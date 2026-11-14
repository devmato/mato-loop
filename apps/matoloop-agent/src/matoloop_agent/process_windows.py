"""PowerShell inspection and isolated execution on the Windows computer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import ntpath
import os
import subprocess
import time
from collections.abc import Awaitable, Callable

from .config import AgentConfig
from .errors import AgentError
from .filesystem_windows import WindowsFilesystem
from .process import _command_categories
from .windows_paths import absolute, native

# Parse with the same installed PowerShell engine that executes the command.
_INSPECTOR = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$source = [Console]::In.ReadToEnd()
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput(
  $source, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) { [Console]::Out.WriteLine('{"syntaxError":true}'); exit 0 }
$commands = @()
$complete = $true
if ($ast.FindAll({ param($n)
  $n -is [System.Management.Automation.Language.AssignmentStatementAst] -or
  $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
  $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst]
}, $true).Count -gt 0) { $complete = $false }
foreach ($node in $ast.FindAll({
  param($n) $n -is [System.Management.Automation.Language.CommandAst]
}, $true)) {
  $name = $node.GetCommandName()
  $words = @()
  foreach ($element in $node.CommandElements) {
    if ($element -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
      $words += $element.Value
    } elseif ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
      $words += '-' + $element.ParameterName
    } else {
      $complete = $false
      $words += $element.Extent.Text
    }
  }
  if (-not $name) { $complete = $false }
  $commands += ,@($words)
}
$redirections = @($ast.FindAll({
  param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst]
}, $true))
$redirectPaths = @()
foreach ($redirection in $redirections) {
  if ($redirection.Location -is
      [System.Management.Automation.Language.StringConstantExpressionAst]) {
    $redirectPaths += $redirection.Location.Value
  } else { $complete = $false }
}
$result = @{commands=$commands; complete=$complete;
  redirect=($redirections.Count -gt 0); redirectPaths=$redirectPaths}
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $result -Compress -Depth 10))
"""

_WRITES = {
    "cp",
    "copy-item",
    "mv",
    "move-item",
    "mkdir",
    "md",
    "new-item",
    "ni",
    "set-content",
    "sc",
    "add-content",
    "ac",
    "out-file",
    "tee-object",
    "rename-item",
    "rni",
    "set-item",
    "start-process",
    "stop-process",
    "invoke-webrequest",
}
_DELETES = {"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "clear-content", "clc"}
_READ_FILES = {
    "get-content",
    "gc",
    "cat",
    "type",
    "get-item",
    "gi",
    "get-childitem",
    "gci",
    "ls",
    "dir",
}


def _encoded(command: str) -> str:
    return base64.b64encode(command.encode("utf-16-le")).decode("ascii")


class WindowsProcess:
    def __init__(self, config: AgentConfig, filesystem: WindowsFilesystem) -> None:
        self.shell = str(
            config.shell_executable
            or os.path.join(
                os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
            )
        )
        if not os.path.isfile(self.shell):
            raise ValueError("Windows PowerShell is unavailable")
        self.filesystem = filesystem
        self.max_output = config.max_output_bytes
        self.default_timeout = config.default_timeout_seconds
        self.max_timeout = config.max_timeout_seconds

    def inspect_shell(self, command: str, current_directory: str) -> dict:
        self.filesystem.resolve_directory(current_directory)
        if len(command.encode("utf-8")) > 256 * 1024:
            raise AgentError(
                "process.inspectionLimitExceeded", "Shell inspection exceeds its limits"
            )
        parsed = subprocess.run(
            [self.shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(_INSPECTOR)],
            input=command,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=15,
            cwd=native(current_directory),
            check=False,
        )
        if parsed.returncode or len(parsed.stdout) > 256 * 1024:
            raise AgentError("process.unavailable", "PowerShell inspection failed")
        try:
            structure = json.loads(parsed.stdout)
        except ValueError as exc:
            raise AgentError("process.unavailable", "PowerShell inspection failed") from exc
        if structure.get("syntaxError"):
            raise AgentError("process.invalidSyntax", "PowerShell command has invalid syntax")
        units = structure.get("commands", [])
        if len(units) > 64 or any(not isinstance(unit, list) or len(unit) > 256 for unit in units):
            raise AgentError(
                "process.inspectionLimitExceeded", "Shell inspection exceeds its limits"
            )
        complete = structure.get("complete") is True
        targets = []
        commands = []
        writes = structure.get("redirect") is True
        required_categories = {"write"} if writes else set()
        for unit in units:
            if not unit or not all(isinstance(token, str) for token in unit):
                complete = False
                continue
            name = ntpath.basename(unit[0]).lower().removesuffix(".exe")
            if name not in _READ_FILES | _WRITES | _DELETES | {
                "git",
                "get-location",
                "pwd",
                "write-output",
                "echo",
            }:
                complete = False
            deletes = name in _DELETES or name.startswith(("remove-", "clear-", "uninstall-"))
            mutates = (
                deletes
                or name in _WRITES
                or name.startswith(
                    (
                        "set-",
                        "new-",
                        "remove-",
                        "clear-",
                        "add-",
                        "copy-",
                        "move-",
                        "rename-",
                        "disable-",
                        "enable-",
                        "install-",
                        "uninstall-",
                        "update-",
                        "restart-",
                        "stop-",
                    )
                )
            )
            categories = (
                _command_categories([name, *unit[1:]])
                if name == "git"
                else {"delete" if deletes else "write" if mutates else "read"}
            )
            mutates = bool(categories & {"write", "delete"})
            required_categories.update(categories)
            writes |= mutates
            commands.append(
                {
                    "tokens": unit,
                    "classification": "write" if mutates else "read",
                    **({"prefix": [unit[0], *unit[1:2]]} if complete and len(unit) > 1 else {}),
                }
            )
            if len(unit[0]) > 2 and unit[0][1:3] in {":/", ":\\"}:
                executable = ntpath.abspath(unit[0]).replace("\\", "/")
                executable = executable[0].upper() + executable[1:]
                absolute(executable)
                self.filesystem._checked(executable)
                targets.append({"path": executable, "kind": "executable", "access": "read"})
            # Static file operands and explicit paths are inspected; dynamic forms are incomplete.
            for token in unit[1:]:
                if token.startswith("-") or "$" in token or "`" in token:
                    if "$" in token or "`" in token:
                        complete = False
                    continue
                if not (
                    token.startswith("./")
                    or token.startswith("../")
                    or (len(token) > 2 and token[1:3] in {":/", ":\\"})
                    or (name in _READ_FILES | _WRITES | _DELETES and not token.startswith("-"))
                ):
                    continue
                candidate = os.path.abspath(os.path.join(native(current_directory), token))
                path = candidate.replace("\\", "/")
                path = path[0].upper() + path[1:]
                try:
                    absolute(path)
                    self.filesystem._checked(path, missing_leaf=True)
                except (ValueError, AgentError):
                    raise AgentError(
                        "path.unsupportedType", "Shell target cannot be inspected"
                    ) from None
                target = {"path": path, "kind": "path", "access": "write" if mutates else "read"}
                try:
                    metadata = self.filesystem.stat(path)
                    target.update(size=metadata["size"], modifiedAt=metadata["modifiedAt"])
                    if metadata["kind"] == "directory":
                        target["kind"] = "directory"
                except AgentError as exc:
                    if exc.code != "path.notFound":
                        raise
                targets.append(target)
        for token in structure.get("redirectPaths", []):
            candidate = os.path.abspath(os.path.join(native(current_directory), token))
            path = candidate.replace("\\", "/")
            path = path[0].upper() + path[1:]
            try:
                absolute(path)
                self.filesystem._checked(path, missing_leaf=True)
            except (ValueError, AgentError) as exc:
                raise AgentError(
                    "path.unsupportedType", "Redirect target cannot be inspected"
                ) from exc
            targets.append({"path": path, "kind": "output", "access": "write"})
        if not complete:
            for item in commands:
                item.pop("prefix", None)
        if writes:
            for item in commands:
                if structure.get("redirect"):
                    item["classification"] = "write"
        result = {
            "targets": targets,
            "analysisComplete": complete,
            "readOnly": not writes,
            "classification": "write" if writes else "read",
            "commands": commands,
            "requiredCategories": [
                category
                for category in ("read", "write", "delete")
                if category in (required_categories or {"read"})
            ],
        }
        result["inspectionRevision"] = hashlib.sha256(
            json.dumps(
                {"command": command, "currentDirectory": current_directory, **result},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return result

    async def exec(
        self,
        *,
        current_directory: str,
        command: str,
        timeout: float | None,
        stream: Callable[[str, str], Awaitable[None]],
        expected_inspection_revision: str | None = None,
        environment: dict[str, str] | None = None,
        temporary_session_id: str | None = None,
    ) -> dict:
        inspection = await asyncio.to_thread(self.inspect_shell, command, current_directory)
        if expected_inspection_revision is not None and (
            inspection["inspectionRevision"] != expected_inspection_revision
        ):
            raise AgentError("process.inspectionChanged", "PowerShell inspection changed")
        timeout = min(timeout or self.default_timeout, self.max_timeout)
        env = {
            name: value
            for name, value in os.environ.items()
            if name.upper()
            in {
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATH",
                "PATHEXT",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "HOMEDRIVE",
                "HOMEPATH",
                "PROGRAMFILES",
                "PROGRAMFILES(X86)",
            }
        }
        env.update(environment or {})
        if temporary_session_id is not None:
            from .temporary import temporary_environment

            env.update(temporary_environment(self.filesystem, temporary_session_id, environment))
        # Stable host prefix sets the pipe encoding; the inspected user command stays intact.
        source = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + command
        if len(_encoded(source)) > 30_000:
            raise AgentError("process.inspectionLimitExceeded", "PowerShell command is too long")
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            self.shell,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            _encoded(source),
            cwd=native(current_directory),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        sent = {"stdout": 0, "stderr": 0}
        truncated = {"stdout": False, "stderr": False}

        async def drain(channel, reader):
            while data := await reader.read(16 * 1024):
                remaining = self.max_output - sum(sent.values())
                if remaining <= 0:
                    truncated[channel] = True
                    continue
                text = data.decode("utf-8", errors="replace")
                encoded = text.encode("utf-8")
                if len(encoded) > remaining:
                    text = encoded[:remaining].decode("utf-8", errors="ignore")
                    truncated[channel] = True
                sent[channel] += len(text.encode("utf-8"))
                if text:
                    await stream(channel, text)

        timed_out = False
        tasks = [
            asyncio.create_task(drain(name, reader))
            for name, reader in (("stdout", process.stdout), ("stderr", process.stderr))
        ]
        try:
            async with asyncio.timeout(timeout):
                await process.wait()
                await asyncio.gather(*tasks)
        except (TimeoutError, asyncio.CancelledError) as exc:
            timed_out = isinstance(exc, TimeoutError)
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            await process.wait()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not timed_out:
                raise
        return {
            "exitCode": process.returncode,
            "timedOut": timed_out,
            "durationMs": round((time.monotonic() - started) * 1000),
            "stdoutBytes": sent["stdout"],
            "stderrBytes": sent["stderr"],
            "stdoutTruncated": truncated["stdout"],
            "stderrTruncated": truncated["stderr"],
        }

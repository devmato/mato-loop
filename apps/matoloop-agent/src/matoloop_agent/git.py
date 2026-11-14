"""Bounded Git inspection and owner-bound session worktree preparation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID

from .config import AgentConfig
from .errors import AgentError
from .platform import FilesystemAdapter
from .protocol import (
    GitBranchArguments,
    GitWorktreeArguments,
    GitWorktreeRemoveArguments,
    _relative_path,
)

MAX_TEXT_BYTES = 256 * 1024
MAX_PREVIEW_BYTES = 768 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_FILES = 500
_worktree_lock = asyncio.Lock()
_preparation_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="git-preparation")


async def _read(reader: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while chunk := await reader.read(min(64 * 1024, limit + 1 - len(output))):
        output.extend(chunk)
        if len(output) > limit:
            raise AgentError("operation.tooLarge", "Git output exceeds the preview limit")
    return bytes(output)


async def _git(
    directory: str,
    *arguments: str,
    limit: int = MAX_OUTPUT_BYTES,
    config: dict[str, str] | None = None,
    timeout_seconds: float = 15,
    protocols: str = "",
) -> tuple[int, bytes, bytes]:
    executable = shutil.which("git")
    if not executable:
        raise AgentError("operation.unsupported", "Git is not installed")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
        GIT_ALLOW_PROTOCOL=protocols,
        GIT_NO_LAZY_FETCH="1",
    )
    if config:
        environment["GIT_CONFIG_COUNT"] = str(len(config))
        for index, (key, value) in enumerate(config.items()):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
    process = await asyncio.create_subprocess_exec(
        executable,
        "--no-optional-locks",
        "--literal-pathspecs",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *arguments,
        cwd=directory,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **(
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        ),
    )
    readers = [
        asyncio.create_task(_read(process.stdout, limit)),
        asyncio.create_task(_read(process.stderr, 8 * 1024)),
    ]
    completed = False
    try:
        async with asyncio.timeout(timeout_seconds):
            output, error = await asyncio.gather(*readers)
            code = await process.wait()
            completed = True
            return code, output, error
    except TimeoutError as exc:
        raise AgentError("path.unavailable", "Git inspection timed out") from exc
    finally:
        for reader in readers:
            reader.cancel()
        if not completed:
            termination = asyncio.create_task(_terminate_git(process))
            try:
                await asyncio.shield(termination)
            except asyncio.CancelledError:
                await termination
                raise
        await asyncio.gather(*readers, return_exceptions=True)


async def _terminate_git(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(killer.wait(), 2)
        except TimeoutError:
            killer.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    # Detached children must not keep cleanup or the preparation lock alive through pipes.
    transport = getattr(process, "_transport", None)
    if transport is not None:
        for descriptor in (0, 1, 2):
            pipe = transport.get_pipe_transport(descriptor)
            if pipe is not None:
                pipe.close()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        if transport is not None:
            transport.close()


async def _checked(directory: str, *arguments: str, config: dict[str, str] | None = None) -> bytes:
    code, output, _ = await _git(directory, *arguments, config=config)
    if code:
        raise AgentError("path.unavailable", "Git inspection failed")
    return output


async def _disabled_filters(directory: str) -> dict[str, str]:
    code, output, _ = await _git(
        directory,
        "config",
        "--null",
        "--name-only",
        "--get-regexp",
        r"^filter\..*\.(clean|smudge|process|required)$",
    )
    if code not in {0, 1}:
        raise AgentError("path.unavailable", "Git filter inspection failed")
    try:
        drivers = {key.decode("utf-8").rsplit(".", 1)[0] for key in output.split(b"\0") if key}
    except UnicodeDecodeError as exc:
        raise AgentError("path.unsupportedType", "Unsupported Git filter name") from exc
    return {
        f"{driver}.{option}": value
        for driver in drivers
        for option, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))
    }


async def _diff(
    filesystem: FilesystemAdapter, root: str, reference: str, paths: tuple[str, ...]
) -> tuple[bytes, bytes]:
    # Keep file-tool writes from introducing a new filter between inspection and comparison.
    while not filesystem._lock.acquire(blocking=False):
        await asyncio.sleep(0.01)
    try:
        config = await _disabled_filters(root)
        flags = ("--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=all")
        names = await _checked(
            root, "diff", *flags, "--name-status", "-z", reference, *paths, config=config
        )
        stats = await _checked(
            root, "diff", *flags, "--numstat", "-z", reference, *paths, config=config
        )
        return names, stats
    finally:
        filesystem._lock.release()


def _path(value: bytes) -> str:
    try:
        path = _relative_path(value.decode("utf-8"))
        if ".git" in PurePosixPath(path).parts:
            raise ValueError("Git metadata is not a file preview")
        return path
    except (UnicodeDecodeError, ValueError) as exc:
        raise AgentError("path.unsupportedType", "Unsupported Git filename") from exc


def _text(data: bytes) -> str:
    if b"\0" in data:
        raise AgentError("file.invalidEncoding", "Binary file cannot be previewed as text")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentError("file.invalidEncoding", "File is not UTF-8 text") from exc


async def _working(
    filesystem: FilesystemAdapter, root: str, path: str, limit: int = MAX_TEXT_BYTES
) -> bytes:
    return await asyncio.to_thread(filesystem._read_bytes, f"{root.rstrip('/')}/{path}", limit)


async def changes(
    filesystem: FilesystemAdapter,
    directory: str,
    file: str | None = None,
    *,
    status_only: bool = False,
) -> dict:
    directory = (await asyncio.to_thread(filesystem.resolve_directory, directory))["path"]
    code, output, error = await _git(directory, "rev-parse", "--show-toplevel")
    if code:
        if error.startswith(b"fatal: not a git repository"):
            return {"repository": None, "files": [], "truncated": False}
        code = "path.accessDenied" if b"dubious ownership" in error else "path.unavailable"
        raise AgentError(code, "Git working tree inspection failed")
    root = output.decode("utf-8").rstrip("\r\n").replace("\\", "/")
    await asyncio.to_thread(filesystem.resolve_directory, root)
    if status_only:
        while not filesystem._lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            status = await _checked(
                root,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=normal",
                "--ignore-submodules=all",
                config=await _disabled_filters(root),
            )
            return {"repository": root, "dirty": bool(status)}
        finally:
            filesystem._lock.release()
    head_code, head, _ = await _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    if head_code not in {0, 1}:
        raise AgentError("path.unavailable", "Git HEAD inspection failed")
    reference = head.decode("ascii").strip()
    paths = ("--", file) if file is not None else ("--",)
    files = {}
    if not head_code:
        names, stats = await _diff(filesystem, root, reference, paths)
        names = names.split(b"\0")
        for index in range(0, len(names) - 1, 2):
            path = _path(names[index + 1])
            files[path] = {
                "path": path,
                "status": {b"A": "added", b"D": "deleted"}.get(names[index], "modified"),
                "additions": None,
                "deletions": None,
            }
        for record in stats.split(b"\0"):
            if not record:
                continue
            added, removed, name = record.split(b"\t", 2)
            item = files.get(_path(name))
            if item is not None and added != b"-":
                item.update(additions=int(added), deletions=int(removed))
        untracked = await _checked(root, "ls-files", "--others", "--exclude-standard", "-z", *paths)
    else:
        untracked = await _checked(
            root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", *paths
        )
    extra = {
        _path(name) for name in untracked.split(b"\0") if name and not name.endswith(b"/")
    }.difference(files)
    for path in extra:
        files[path] = {"path": path, "status": "added", "additions": None, "deletions": None}
    if file is not None:
        if file not in files:
            raise AgentError("file.changed", "File is no longer changed")
        item = files[file]
        result = {"repository": root, "file": item, "before": "", "after": "", "reason": None}
        try:
            if not head_code and item["status"] != "added":
                code, before, _ = await _git(
                    root, "cat-file", "blob", f"{reference}:{file}", limit=MAX_TEXT_BYTES
                )
                if code:
                    raise AgentError("path.unsupportedType", "Git object is not a regular file")
                result["before"] = _text(before)
            if item["status"] != "deleted":
                result["after"] = _text(await _working(filesystem, root, file))
            # Leave room for the response envelope within the 1 MiB transport limit.
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_PREVIEW_BYTES:
                raise AgentError("operation.tooLarge", "Git preview exceeds the transport budget")
        except AgentError as exc:
            reasons = {
                "file.tooLarge": "tooLarge",
                "operation.tooLarge": "tooLarge",
                "file.invalidEncoding": "binary",
                "path.notFile": "unsupported",
                "path.symlinkUnsupported": "unsupported",
                "path.unsupportedType": "unsupported",
            }
            if exc.code not in reasons:
                raise
            result.update(before="", after="", reason=reasons[exc.code])
        return result
    selected = []
    budget = 0
    for path, item in sorted(files.items()):
        size = len(json.dumps(item).encode("utf-8"))
        if len(selected) >= MAX_FILES or budget + size > 256 * 1024:
            break
        if path in extra:
            try:
                text = _text(await _working(filesystem, root, path, limit=64 * 1024))
                item.update(
                    additions=text.count("\n") + int(bool(text) and not text.endswith("\n")),
                    deletions=0,
                )
            except AgentError as exc:
                if exc.code not in {
                    "file.tooLarge",
                    "file.invalidEncoding",
                    "path.notFile",
                    "path.symlinkUnsupported",
                    "path.unsupportedType",
                    "path.notFound",
                }:
                    raise
        selected.append(item)
        budget += size
    return {"repository": root, "files": selected, "truncated": len(selected) < len(files)}


async def repository(
    filesystem: FilesystemAdapter,
    directory: str,
    *,
    executor: Executor | None = None,
    target_branch: str | None = None,
) -> dict:
    loop = asyncio.get_running_loop()
    directory = (await loop.run_in_executor(executor, filesystem.resolve_directory, directory))[
        "path"
    ]
    code, output, error = await _git(directory, "rev-parse", "--show-toplevel")
    if code:
        if error.startswith(b"fatal: not a git repository"):
            return {
                "repository": None,
                "commonDirectory": None,
                "remotes": [],
                "defaultRemote": None,
                "defaultBranch": None,
                "branch": None,
                "localBranches": [],
                "localBranchesTruncated": False,
            }
        raise AgentError("path.unavailable", "Git repository inspection failed")
    root = output.decode("utf-8").rstrip("\r\n").replace("\\", "/")
    await loop.run_in_executor(executor, filesystem.resolve_directory, root)
    common = (
        (await _checked(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        .decode("utf-8")
        .rstrip("\r\n")
        .replace("\\", "/")
    )
    await loop.run_in_executor(executor, filesystem.resolve_directory, common)
    remotes = (await _checked(root, "remote")).decode("utf-8").splitlines()
    if len(remotes) > 100:
        raise AgentError("operation.tooLarge", "Too many Git remotes")
    identities = {}
    urls = {}
    for name in remotes:
        fetch_urls = await _checked(root, "remote", "get-url", "--all", "--", name)
        push_urls = await _checked(root, "remote", "get-url", "--push", "--all", "--", name)
        identities[name] = hashlib.sha256(fetch_urls + b"\0" + push_urls).hexdigest()
        value = fetch_urls.decode("utf-8").splitlines()[0].strip()
        try:
            parsed = urlsplit(value)
            hidden = (
                parsed.password is not None
                or parsed.query
                or parsed.fragment
                or (parsed.scheme in {"http", "https"} and parsed.username is not None)
            )
            urls[name] = (
                None
                if hidden or len(value) > 4096 or any(ord(char) < 32 for char in value)
                else value
            )
        except ValueError:
            urls[name] = None
    remote = "origin" if "origin" in remotes else remotes[0] if len(remotes) == 1 else None
    code, branch, _ = await _git(root, "symbolic-ref", "--quiet", "HEAD")
    branch = branch.decode("utf-8").strip().removeprefix("refs/heads/") if code == 0 else None
    local_branches = (
        (
            await _checked(
                root, "for-each-ref", "--format=%(refname:lstrip=2)", "--count=1001", "refs/heads/"
            )
        )
        .decode("utf-8")
        .splitlines()
    )
    default_branch = None
    if remote:
        code, reference, _ = await _git(
            root, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"
        )
        prefix = f"refs/remotes/{remote}/"
        value = reference.decode("utf-8").strip()
        if code == 0 and value.startswith(prefix):
            default_branch = value[len(prefix) :]
    target = target_branch or default_branch or branch
    target_has_commit = None
    if target:
        code, _, _ = await _git(
            root, "rev-parse", "--verify", "--quiet", f"refs/heads/{target}^{{commit}}"
        )
        target_has_commit = code == 0
    return {
        "repository": root,
        "commonDirectory": common,
        "remotes": remotes,
        "defaultRemote": remote,
        "defaultBranch": default_branch or branch,
        "branch": branch,
        "localBranches": local_branches[:1000],
        "localBranchesTruncated": len(local_branches) > 1000,
        "worktrees": await worktrees(filesystem, root),
        "remoteIdentities": identities,
        "remoteUrls": urls,
        "targetHasCommit": target_has_commit,
        "gitDirectory": (await _checked(root, "rev-parse", "--absolute-git-dir"))
        .decode("utf-8")
        .strip()
        .replace("\\", "/"),
    }


def _check_metadata(common: str) -> None:
    pending = [Path(common)]
    count = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > 100_000:
                    raise AgentError(
                        "operation.tooLarge", "Git metadata exceeds the inspection limit"
                    )
                metadata = entry.stat(follow_symlinks=False)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & 0x400
                ):
                    raise AgentError(
                        "git.worktreeFailed",
                        "Linked Git metadata is not supported for managed preparation",
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
    alternates = Path(common) / "objects" / "info" / "alternates"
    if alternates.exists() and alternates.stat().st_size:
        raise AgentError(
            "git.worktreeFailed",
            "Alternate object directories are not supported for managed preparation",
        )


@asynccontextmanager
async def _preparation_lock(config: AgentConfig, filesystem: FilesystemAdapter):
    directory = config.data_dir / "worktree-state"
    filesystem.resolve_directory(config.data_dir.as_posix())
    lock = config.data_dir / "worktree-preparation.lock"
    if lock.is_symlink() or (
        lock.exists() and getattr(lock.lstat(), "st_file_attributes", 0) & 0x400
    ):
        raise AgentError("git.worktreeFailed", "Preparation lock must not be a link")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AgentError("git.worktreeFailed", "Invalid preparation lock")
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
            except OSError as exc:
                if os.name != "nt" or exc.errno not in {13, 36}:
                    raise
                await asyncio.sleep(0.05)
        filesystem.mkdir(directory.as_posix(), True)
        yield directory
    finally:
        os.close(descriptor)


@asynccontextmanager
async def _locked_worktrees(config: AgentConfig, filesystem: FilesystemAdapter):
    async with _worktree_lock:
        while not filesystem._lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            async with _preparation_lock(config, filesystem) as state_directory:
                try:
                    yield state_directory
                except AgentError as exc:
                    if exc.code == "path.unavailable":
                        raise AgentError(
                            "operation.cancelled", "Git preparation outcome is unknown"
                        ) from exc
                    raise
        finally:
            filesystem._lock.release()


def project_worktree_directory(config, filesystem, project_id: str) -> Path:
    root = config.data_dir / "worktrees"
    project_id = str(UUID(project_id))
    for index in range(1, 1001):
        candidate = root / (project_id[:8] if index == 1 else f"{project_id[:8]}-{index}")
        if not os.path.lexists(candidate):
            return candidate
        try:
            identity = filesystem._read_bytes((candidate / ".matoloop-project").as_posix(), 128)
        except AgentError:
            continue
        if identity == project_id.encode():
            return candidate
    raise AgentError("git.worktreeFailed", "No available project directory name was found")


async def prepare_worktree(
    config: AgentConfig, filesystem: FilesystemAdapter, arguments: GitWorktreeArguments
) -> dict:
    async with _locked_worktrees(config, filesystem) as state_directory:
        return await _prepare_worktree(config, filesystem, arguments, state_directory)


async def worktrees(filesystem: FilesystemAdapter, directory: str) -> list[dict]:
    records = (await _checked(directory, "worktree", "list", "--porcelain", "-z")).split(b"\0\0")
    items = []
    for index, record in enumerate(records):
        fields = dict(field.partition(b" ")[::2] for field in record.split(b"\0") if field)
        if b"worktree" not in fields or b"bare" in fields:
            continue
        path = _text(fields[b"worktree"])
        try:
            resolved = filesystem.resolve_directory(path)["path"]
            if resolved != path:
                raise AgentError("git.worktreeChanged", "The worktree path changed")
        except AgentError:
            continue
        items.append(
            {
                "path": path,
                "branch": _text(fields[b"branch"])[11:] if b"branch" in fields else None,
                "main": index == 0,
                "locked": b"locked" in fields,
            }
        )
    return items


async def branch_action(
    config: AgentConfig, filesystem: FilesystemAdapter, arguments: GitBranchArguments
) -> dict:
    async with _locked_worktrees(config, filesystem):
        info = await repository(filesystem, arguments.path, executor=_preparation_executor)
        if not info["repository"] or info["repository"] != arguments.path:
            raise AgentError("git.repositoryRootRequired", "Select the repository root")
        await asyncio.to_thread(_check_metadata, info["commonDirectory"])
        items = info["worktrees"]
        selected = next((item for item in items if item["path"] == arguments.path), None)
        if selected is None:
            raise AgentError(
                "git.worktreeChanged", "The worktree is not registered in this repository"
            )
        directory = selected["path"]
        if selected["locked"]:
            raise AgentError("git.worktreeLocked", "The worktree is locked")
        name = arguments.name
        if (
            name.startswith("-")
            or (await _git(directory, "check-ref-format", f"refs/heads/{name}"))[0]
        ):
            raise AgentError("git.invalidBranch", "The branch name is invalid")
        if arguments.action in {"switch", "rename"} and await _checked(
            directory,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            config=await _disabled_filters(directory),
        ):
            raise AgentError("git.dirty", "Commit the working changes first")
        if arguments.action == "create":
            code, _, _ = await _git(directory, "branch", "--", name, "HEAD")
        elif arguments.action == "switch":
            if any(item["branch"] == name and item["path"] != directory for item in items):
                raise AgentError("git.branchInUse", "The branch is checked out in another worktree")
            if (await _git(directory, "show-ref", "--verify", "--quiet", f"refs/heads/{name}"))[0]:
                raise AgentError("git.localBranchRequired", "The local branch is unavailable")
            code, _, _ = await _git(
                directory,
                "checkout",
                "--no-guess",
                "--no-overwrite-ignore",
                "--no-recurse-submodules",
                name,
                config=await _disabled_filters(directory),
            )
        elif arguments.action == "rename":
            branch = info["branch"]
            if not branch:
                raise AgentError("git.branchRequired", "Create and select a branch first")
            published = await _checked(
                directory, "for-each-ref", "--format=%(refname)", "refs/remotes/"
            )
            if any(
                _text(ref).removeprefix("refs/remotes/").partition("/")[2] == branch
                for ref in published.splitlines()
            ):
                raise AgentError("git.branchPublished", "Published branches are not renamed")
            if (await _git(directory, "config", "--get", f"branch.{branch}.remote"))[0] == 0:
                raise AgentError("git.branchPublished", "Published branches are not renamed")
            code, _, _ = await _git(directory, "branch", "-m", branch, name)
        else:
            reference = f"refs/heads/{name}"
            occupied = await _checked(
                directory, "branch", "--list", "--format=%(worktreepath)", "--", name
            )
            if occupied.strip():
                raise AgentError("git.branchInUse", "The branch is checked out in a worktree")
            target = arguments.target_branch
            if not target or name == target:
                raise AgentError("git.targetRequired", "Select a different local target branch")
            tip = (await _checked(directory, "rev-parse", "--verify", reference)).decode().strip()
            if (
                await _git(
                    directory,
                    "merge-base",
                    "--is-ancestor",
                    tip,
                    f"refs/heads/{target}",
                )
            )[0]:
                raise AgentError(
                    "git.branchUnmerged", "The branch contains commits not in the target"
                )
            # Delete only the checked tip, regardless of its configured upstream.
            code, _, _ = await _git(directory, "update-ref", "-d", reference, tip)
            if not code:
                await _git(directory, "config", "--remove-section", f"branch.{name}")
        if code:
            raise AgentError(
                "git.actionFailed",
                "The Git action could not be completed; inspect its current state",
            )
        return {}


def _check_worktree_session(filesystem, git_directory: str, owner: str, session: str) -> None:
    try:
        identity = filesystem._read_bytes(f"{git_directory}/matoloop-session", 128)
    except AgentError as exc:
        if exc.code != "path.notFound":
            raise
        raise AgentError("git.worktreeChanged", "The worktree session identity is missing") from exc
    if identity != f"{owner}/{session}".encode():
        raise AgentError("git.worktreeChanged", "The worktree session identity changed")


def _check_removal_permissions(path: str, git_directory: str | None = None) -> None:
    def denied(directory):
        raise AgentError(
            "git.worktreePermissionDenied",
            "Worktree removal requires directory access rights",
            {"path": os.fspath(directory)},
        )

    def on_error(error):
        if isinstance(error, PermissionError):
            denied(error.filename)
        raise error

    access_options = {"effective_ids": True} if os.access in os.supports_effective_ids else {}

    def check_sticky(parent, children):
        if os.name != "posix":
            return
        metadata = Path(parent).stat(follow_symlinks=False)
        uid = os.geteuid()
        if metadata.st_mode & stat.S_ISVTX and uid not in {0, metadata.st_uid}:
            for child in children:
                if Path(child).stat(follow_symlinks=False).st_uid != uid:
                    denied(child)

    for target in (path, git_directory) if git_directory else (path,):
        parent = Path(target).parent
        if not os.access(parent, os.W_OK | os.X_OK, **access_options):
            denied(parent)
        check_sticky(parent, [target])
        for directory, directories, files in os.walk(target, followlinks=False, onerror=on_error):
            required = os.R_OK | os.X_OK | (os.W_OK if directories or files else 0)
            if not os.access(directory, required, **access_options):
                denied(directory)
            check_sticky(directory, (Path(directory) / name for name in directories + files))


async def remove_worktree(config, filesystem, arguments: GitWorktreeRemoveArguments) -> dict:
    async with _locked_worktrees(config, filesystem):
        info = await repository(filesystem, arguments.repository, executor=_preparation_executor)
        if info["commonDirectory"] != arguments.common_directory:
            raise AgentError("git.worktreeChanged", "The repository metadata changed")
        item = next((entry for entry in info["worktrees"] if entry["path"] == arguments.path), None)
        if item is None:
            raise AgentError(
                "git.worktreeChanged", "The worktree is not registered in this repository"
            )
        if item["main"] or arguments.path == arguments.repository:
            raise AgentError("git.mainWorktree", "The original checkout cannot be removed")
        if item["locked"]:
            raise AgentError("git.worktreeLocked", "The worktree is locked")
        await asyncio.to_thread(_check_metadata, info["commonDirectory"])
        current = await repository(filesystem, arguments.path, executor=_preparation_executor)
        if current["commonDirectory"] != info["commonDirectory"]:
            raise AgentError("git.worktreeChanged", "The worktree metadata changed")
        if not arguments.force and await _checked(
            arguments.path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored",
            config=await _disabled_filters(arguments.path),
        ):
            raise AgentError("git.dirty", "The worktree contains changes or additional files")
        await asyncio.to_thread(_check_removal_permissions, arguments.path, current["gitDirectory"])
        code, _, _ = await _git(
            arguments.repository,
            "worktree",
            "remove",
            *(("--force",) if arguments.force else ()),
            "--",
            arguments.path,
            config=await _disabled_filters(arguments.path),
            timeout_seconds=120,
        )
        if code:
            raise AgentError("git.worktreeRemoveFailed", "The worktree could not be removed")
        return {"removed": True}


async def _refresh_worktree_target(root: str, target: str, remote: str) -> None:
    tracking = f"refs/remotes/{remote}/{target.removeprefix('refs/heads/')}"
    if (await _git(root, "check-ref-format", tracking))[0]:
        raise AgentError("git.invalidRemote", "Invalid remote tracking reference")
    try:
        code, _, _ = await _git(
            root,
            "ls-remote",
            "--exit-code",
            "--refs",
            "--",
            remote,
            target,
            protocols="https:http:ssh:git",
            timeout_seconds=120,
        )
        if code == 2:
            return
        if not code:
            code, _, _ = await _git(
                root,
                "fetch",
                "--no-tags",
                "--no-prune",
                "--no-prune-tags",
                "--no-recurse-submodules",
                "--no-auto-maintenance",
                "--no-write-fetch-head",
                "--",
                remote,
                f"+{target}:{tracking}",
                protocols="https:http:ssh:git",
                timeout_seconds=120,
            )
    except AgentError as exc:
        if exc.code != "path.unavailable":
            raise
        raise AgentError("git.fetchFailed", "The target branch could not be fetched") from exc
    if code:
        raise AgentError("git.fetchFailed", "The target branch could not be fetched")
    occupied = _text(
        await _checked(root, "branch", "--list", "--format=%(worktreepath)", "--", target[11:])
    ).strip()
    if occupied and occupied != root:
        raise AgentError("git.branchInUse", "The target branch is checked out in another worktree")
    filters = await _disabled_filters(root)
    if occupied and await _checked(
        root, "status", "--porcelain=v1", "--untracked-files=all", config=filters
    ):
        raise AgentError("git.dirty", "Commit the working changes before updating the target")
    before = _text(await _checked(root, "rev-parse", "--verify", target)).strip()
    after = _text(await _checked(root, "rev-parse", "--verify", tracking)).strip()
    if (await _git(root, "merge-base", "--is-ancestor", after, before))[0] == 0:
        return
    if (await _git(root, "merge-base", "--is-ancestor", before, after))[0]:
        raise AgentError("git.actionFailed", "The target branch cannot be fast-forwarded")
    if occupied:
        code, _, _ = await _git(
            root,
            "-c",
            "merge.autostash=false",
            "-c",
            "submodule.recurse=false",
            "merge",
            "--ff-only",
            "--no-autostash",
            "--no-edit",
            "--no-overwrite-ignore",
            after,
            config=filters,
            timeout_seconds=120,
        )
    else:
        code, _, _ = await _git(root, "update-ref", target, after, before)
    if code:
        raise AgentError("git.actionFailed", "The target branch could not be fast-forwarded")


async def _prepare_worktree(config, filesystem, arguments, state_directory) -> dict:
    loop = asyncio.get_running_loop()
    info = await repository(filesystem, arguments.path, executor=_preparation_executor)
    root = info["repository"]
    if root is None:
        raise AgentError("git.notRepository", "The selected directory is not a Git repository")
    selected = (
        await loop.run_in_executor(
            _preparation_executor, filesystem.resolve_directory, arguments.path
        )
    )["path"]
    if selected != root:
        raise AgentError("git.repositoryRootRequired", "Select the repository root")
    if info["commonDirectory"] != arguments.common_directory:
        raise AgentError("git.worktreeChanged", "The repository metadata location changed")
    await loop.run_in_executor(_preparation_executor, _check_metadata, info["commonDirectory"])
    target = f"refs/heads/{arguments.target_branch}"
    code, _, _ = await _git(root, "check-ref-format", target)
    if code:
        raise AgentError("git.invalidBranch", "Invalid target branch")
    owner = str(UUID(arguments.owner_id))
    project = str(UUID(arguments.project_id))
    session = str(UUID(arguments.session_id))
    project_directory = project_worktree_directory(config, filesystem, project)
    candidates = [
        destination
        for destination in arguments.destinations
        if PurePosixPath(destination).parent == PurePosixPath(project_directory.as_posix())
        and re.fullmatch(r"[0-9a-f]{8}", PurePosixPath(destination).name)
    ]
    if not candidates:
        raise AgentError("git.worktreeChanged", "No authorized worktree destination was supplied")
    state_path = (state_directory / f"{owner}-{session}.json").as_posix()
    try:
        state = json.loads(filesystem._read_bytes(state_path, 256 * 1024))
    except AgentError as exc:
        if exc.code != "path.notFound":
            raise
        state = None
    except (ValueError, UnicodeDecodeError) as exc:
        raise AgentError("git.worktreeChanged", "Invalid preparation record") from exc
    registered = (await _checked(root, "worktree", "list", "--porcelain", "-z")).split(b"\0")
    if state is not None:
        if (
            not isinstance(state, dict)
            or state.get("path") not in candidates
            or "branch" not in state
            or state["branch"] not in (None, f"worktree-{PurePosixPath(state['path']).name}")
        ):
            raise AgentError("git.worktreeChanged", "Invalid preparation binding")
        path, branch = state["path"], state["branch"]
    else:
        for candidate in candidates:
            if (
                not os.path.lexists(candidate)
                and f"worktree {candidate}".encode() not in registered
            ):
                path, branch = candidate, None
                break
        else:
            raise AgentError("git.worktreeFailed", "No available worktree name was found")
    if path == root or path.startswith(root.rstrip("/") + "/"):
        raise AgentError(
            "git.worktreeLocation", "Managed worktrees must be outside the source repository"
        )
    base_ref = f"refs/matoloop/bases/{owner}/{session}"
    binding = {
        "repository": root,
        "commonDirectory": info["commonDirectory"],
        "path": path,
        "branch": branch,
        "targetBranch": arguments.target_branch,
        "projectId": project,
    }
    if state is not None and (
        not isinstance(state, dict)
        or any(state.get(key) != value for key, value in binding.items())
    ):
        raise AgentError("git.worktreeChanged", "Preparation inputs changed")
    parent = path.rsplit("/", 1)[0]
    filesystem.mkdir(parent, True)
    filesystem.upsert((project_directory / ".matoloop-project").as_posix(), project)
    # Recover only the exact registered worktree; never adopt or reset an unrelated directory.
    exists = f"worktree {path}".encode() in registered
    if state is None:
        if exists or os.path.lexists(path):
            raise AgentError(
                "git.worktreeChanged", "The destination has no matching preparation record"
            )
        state = {**binding, "ready": False, "baseCommit": None}
        filesystem.upsert(state_path, json.dumps(state))
    if exists:
        current = await repository(filesystem, path, executor=_preparation_executor)
        git_dir = current["gitDirectory"]
        if (
            not state.get("ready")
            or current["repository"] != path
            or current["branch"] != branch
            or current["commonDirectory"] != info["commonDirectory"]
            or git_dir != state.get("gitDirectory")
        ):
            raise AgentError("git.worktreeChanged", "The session worktree binding changed")
        _check_worktree_session(filesystem, git_dir, owner, session)
    elif state.get("ready"):
        raise AgentError("git.worktreeChanged", "The completed worktree is no longer registered")
    if state.get("baseCommit") is None:
        code, commit, _ = await _git(root, "rev-parse", "--verify", f"{target}^{{commit}}")
        if code:
            raise AgentError(
                "git.localBranchRequired", "The local target branch must contain a commit"
            )
        if arguments.remote is not None:
            if arguments.remote not in info["remotes"]:
                raise AgentError("git.remoteRequired", "The selected remote is unavailable")
            await _refresh_worktree_target(root, target, arguments.remote)
            commit = await _checked(root, "rev-parse", "--verify", f"{target}^{{commit}}")
        await _checked(root, "update-ref", base_ref, commit.decode().strip())
    base = (
        (await _checked(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")).decode().strip()
    )
    if state.get("baseCommit") not in {None, base}:
        raise AgentError("git.worktreeChanged", "The preparation base changed")
    state["baseCommit"] = base
    if not exists:
        if branch is not None:
            code, _, _ = await _git(
                root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
            )
            if code == 0:
                raise AgentError(
                    "git.worktreeChanged", "The preparation branch changed or is in use"
                )
            if code not in {0, 1}:
                raise AgentError(
                    "git.worktreeFailed", "The preparation branch could not be inspected"
                )
        if os.path.lexists(path):
            filesystem.resolve_directory(path)
            if any(Path(path).iterdir()):
                raise AgentError("git.worktreeChanged", "The incomplete destination contains files")
        else:
            # Git checks out as the Agent process, not the data directory's owner.
            filesystem.mkdir(path, False, inherit_ownership=False)
        filesystem.resolve_directory(path)
        filesystem.upsert(state_path, json.dumps(state))
        code, _, _ = await _git(
            root,
            "worktree",
            "add",
            "--no-checkout",
            *(("-b", branch) if branch is not None else ("--detach",)),
            "--",
            path,
            base,
            timeout_seconds=120,
        )
        if not code:
            code, _, _ = await _git(
                path,
                "read-tree",
                "--reset",
                "-u",
                "HEAD",
                config=await _disabled_filters(path),
                timeout_seconds=120,
            )
        if code:
            raise AgentError("git.worktreeFailed", "The worktree could not be prepared")
        state["gitDirectory"] = (
            (await _checked(path, "rev-parse", "--absolute-git-dir"))
            .decode()
            .strip()
            .replace("\\", "/")
        )
        filesystem.upsert(f"{state['gitDirectory']}/matoloop-session", f"{owner}/{session}")
        state["ready"] = True
        filesystem.upsert(state_path, json.dumps(state))
    await loop.run_in_executor(_preparation_executor, filesystem.resolve_directory, path)
    return {
        "repository": root,
        "path": path,
        "branch": branch,
        "baseCommit": base,
        "targetBranch": arguments.target_branch,
        "commonDirectory": info["commonDirectory"],
        "gitDirectory": state["gitDirectory"],
    }

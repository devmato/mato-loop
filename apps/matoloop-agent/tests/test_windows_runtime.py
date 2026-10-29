import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch


@unittest.skipUnless(sys.platform == "win32", "native Windows test")
class WindowsRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        from matoloop_agent.config import AgentConfig
        from matoloop_agent.filesystem_windows import WindowsFilesystem
        from matoloop_agent.process_windows import WindowsProcess

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "workspace"
        self.home.mkdir()
        config_path = Path(self.temporary.name) / "config.json"
        config_path.write_text(json.dumps({"homeDirectory": str(self.home)}), encoding="utf-8")
        config = AgentConfig.load(config_path)
        self.config = config
        self.filesystem = WindowsFilesystem(config)
        self.process = WindowsProcess(config, self.filesystem)
        self.wire = self.home.as_posix()

    def test_file_operations_and_no_follow(self) -> None:
        from matoloop_agent.errors import AgentError

        nested = self.wire + "/nested/inside"
        self.filesystem.mkdir(nested, True)
        self.assertEqual(self.filesystem.stat(nested)["kind"], "directory")
        self.assertIn(
            "nested", [entry["name"] for entry in self.filesystem.list(self.wire, 0, 10)["entries"]]
        )
        path = self.wire + "/note.txt"
        written = self.filesystem.write(path, "one\n", None)
        self.assertEqual(self.filesystem.read(path, 1, 10, 2000)["sha256"], written["sha256"])
        self.filesystem.edit(path, "one", "two", False, written["sha256"])
        self.assertIn("two", self.filesystem.read(path, 1, 10, 2000)["content"])
        revision = self.filesystem.read(path, 1, 10, 2000)["sha256"]
        self.filesystem.patch(
            f"*** Begin Patch\n*** Update File: {path}\n@@\n-two\n+three\n*** End Patch",
            {path: revision},
        )
        self.assertIn("three", self.filesystem.read(path, 1, 10, 2000)["content"])
        with self.assertRaises(AgentError):
            self.filesystem.write(path, "stale", written["sha256"])
        link = self.home / "alias.txt"
        try:
            link.symlink_to(self.home / "note.txt")
        except OSError:
            self.skipTest("Symlink creation is not permitted on this Windows account")
        self.assertEqual(self.filesystem.stat(link.as_posix())["kind"], "symlink")
        with self.assertRaises(AgentError):
            self.filesystem.read(link.as_posix(), 1, 10, 2000)

    def test_patch_move_with_changes_checks_destination(self) -> None:
        from matoloop_agent.errors import AgentError

        source, destination = self.wire + "/source.txt", self.wire + "/destination.txt"
        revision = self.filesystem.write(source, "old\r\n", None)["sha256"]
        self.filesystem.write(destination, "existing\r\n", None)
        text = (
            f"*** Begin Patch\n*** Update File: {source}\n*** Move to: {destination}\n"
            "-old\n+new\n*** End of File\n*** End Patch"
        )
        with self.assertRaises(AgentError) as caught:
            self.filesystem.patch(text, {source: revision})
        self.assertEqual(caught.exception.params["destination"], destination)
        self.assertEqual((self.home / "source.txt").read_bytes(), b"old\r\n")
        self.assertEqual((self.home / "destination.txt").read_bytes(), b"existing\r\n")
        self.filesystem.delete(destination, False)
        result = self.filesystem.patch(text, {source: revision})
        self.assertEqual((self.home / "destination.txt").read_bytes(), b"new\r\n")
        self.assertFalse((self.home / "source.txt").exists())
        self.assertEqual(
            result["files"][0]["sha256"],
            self.filesystem.read(destination, 1, 10, 2000)["sha256"],
        )

        self.filesystem.delete(destination, False)
        self.filesystem.write(source, "old\r\n", None)
        write = self.filesystem.write

        def replace_after_write(*args):
            result = write(*args)
            replacement = self.home / "replacement.txt"
            replacement.write_bytes(b"external\r\n")
            replacement.replace(self.home / "source.txt")
            return result

        with patch.object(self.filesystem, "write", side_effect=replace_after_write):
            with self.assertRaises(AgentError):
                self.filesystem.patch(text, {source: revision})
        self.assertEqual(
            ((self.home / "source.txt").read_bytes(), (self.home / "destination.txt").exists()),
            (b"external\r\n", False),
        )

    def test_installer_mutex_blocks_agent_admission_and_script_parses(self) -> None:
        import hashlib

        from matoloop_agent.platform_windows import WindowsPlatform

        root = self.config.data_dir.parent
        name = (
            "Global\\MatoLoopAgentInstaller-"
            + hashlib.sha256(str(root).lower().rstrip("\\/").encode()).hexdigest()
        )
        installer = Path(__file__).parents[3] / "scripts/install-matoloop-agent.ps1"
        installer_path = str(installer).replace("'", "''")
        script = (
            "$tokens=$null; $errors=$null; "
            f"[Management.Automation.Language.Parser]::ParseFile('{installer_path}', "
            "[ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count) {throw ($errors | Out-String)}; "
            f"$mutex=[Threading.Mutex]::new($false, '{name}'); "
            "$mutex.WaitOne() | Out-Null; [Console]::WriteLine('locked'); "
            "[Console]::In.ReadLine() | Out-Null; $mutex.ReleaseMutex(); $mutex.Dispose()"
        )
        process = subprocess.Popen(
            [self.process.shell, "-NoProfile", "-Command", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with patch.object(WindowsPlatform, "_maintenance_available", return_value=True):
                self.assertTrue(WindowsPlatform().maintenance_running(self.config))
        finally:
            process.communicate("release\n", timeout=10)
        self.assertEqual(process.returncode, 0)

    def test_powershell_inspection_and_revision(self) -> None:
        read = self.process.inspect_shell("Get-Content ./note.txt", self.wire)
        self.assertEqual(read["classification"], "read")
        self.assertIn(self.wire + "/note.txt", [target["path"] for target in read["targets"]])
        write = self.process.inspect_shell("Set-Content ./note.txt two", self.wire)
        self.assertEqual(write["classification"], "write")
        for command, categories in (
            ("Get-Content ./note.txt", ["read"]),
            ("Set-Content ./note.txt two", ["write"]),
            ("Remove-Item ./note.txt", ["delete"]),
            ("Clear-Content ./note.txt", ["delete"]),
            ("rmdir ./old", ["delete"]),
            ("Set-Content ./new.txt two; Remove-Item ./note.txt", ["write", "delete"]),
            ("git clean -n", ["read"]),
            ("git clean -- -n", ["delete"]),
            ("git branch -qD old", ["delete"]),
            ("git push --delete origin old", ["write", "delete"]),
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.process.inspect_shell(command, self.wire)["requiredCategories"], categories
                )

        async def execute():
            chunks = []

            async def stream(channel, text):
                chunks.append((channel, text))

            result = await self.process.exec(
                current_directory=self.wire,
                command="Write-Output 'Windows ready'",
                timeout=10,
                stream=stream,
            )
            self.assertEqual(result["exitCode"], 0)
            self.assertIn("Windows ready", "".join(text for _, text in chunks))

        asyncio.run(execute())

    def test_pairing_state_is_protected_by_windows_dpapi(self) -> None:
        from pydantic import SecretStr

        from matoloop_agent.credentials import CredentialState
        from matoloop_agent.credentials_windows import WindowsCredentialStore

        state = CredentialState(
            version=1,
            backendUrl="wss://example.test/api/execution-agents/connect",
            agentId="2ea504cc-e3c6-4e0d-aa05-080328100fe8",
            accessKey=SecretStr("private-access-key"),
            lastPairingTokenHash="a" * 64,
        )
        with WindowsCredentialStore(Path(self.temporary.name)) as store:
            store.save(state)
            self.assertEqual(store.load().access_key.get_secret_value(), "private-access-key")
        self.assertNotIn(
            b"private-access-key",
            (Path(self.temporary.name) / "agent-state" / "credential.dat").read_bytes(),
        )

    def test_mutation_stays_in_original_parent_after_path_swap(self) -> None:
        from matoloop_agent import windows_handles

        original = self.home / "project"
        original.mkdir()
        moved = self.home / "original-project"
        staged_file = windows_handles.staged_file

        @contextmanager
        def swap_then_publish(parent, name, *, replace):
            original.rename(moved)
            original.mkdir()
            with staged_file(parent, name, replace=replace) as handle:
                yield handle

        with patch.object(windows_handles, "staged_file", side_effect=swap_then_publish):
            self.filesystem.write((original / "result.txt").as_posix(), "bounded", None)
        self.assertEqual((moved / "result.txt").read_text(), "bounded")
        self.assertFalse((original / "result.txt").exists())

    def test_credential_reader_waits_for_pairing_lock(self) -> None:
        from matoloop_agent.credentials_windows import WindowsCredentialStore

        script = (
            "import sys; from pathlib import Path; "
            "from matoloop_agent.credentials_windows import WindowsCredentialStore; "
            "print('ready', flush=True)\n"
            "with WindowsCredentialStore(Path(sys.argv[1])): pass\n"
        )
        with WindowsCredentialStore(self.config.data_dir):
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(self.config.data_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                process.stdout.readline()
                time.sleep(11)
            except BaseException:
                process.kill()
                process.communicate()
                raise
        try:
            _, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_delete_uses_the_opened_file_after_parent_swap(self) -> None:
        from matoloop_agent import windows_handles

        original = self.home / "project"
        original.mkdir()
        (original / "remove.txt").write_text("original", encoding="utf-8")
        moved = self.home / "original-project"
        child = windows_handles.child

        @contextmanager
        def swap_then_open(parent, name, **kwargs):
            if name == "remove.txt":
                original.rename(moved)
                original.mkdir()
                (original / "remove.txt").write_text("replacement", encoding="utf-8")
            with child(parent, name, **kwargs) as handle:
                yield handle

        with patch.object(windows_handles, "child", side_effect=swap_then_open):
            self.filesystem.delete((original / "remove.txt").as_posix(), False)
        self.assertFalse((moved / "remove.txt").exists())
        self.assertEqual((original / "remove.txt").read_text(), "replacement")

    def test_read_uses_the_opened_parent_after_path_swap(self) -> None:
        from matoloop_agent import windows_handles

        original = self.home / "project"
        original.mkdir()
        (original / "note.txt").write_text("approved", encoding="utf-8")
        moved = self.home / "original-project"
        child = windows_handles.child

        @contextmanager
        def swap_before_leaf(parent, name, **kwargs):
            if name == "note.txt":
                original.rename(moved)
                original.mkdir()
                (original / "note.txt").write_text("wrong target", encoding="utf-8")
            with child(parent, name, **kwargs) as handle:
                yield handle

        with patch.object(windows_handles, "child", side_effect=swap_before_leaf):
            content = self.filesystem.read((original / "note.txt").as_posix(), 1, 10, 2000)
        self.assertIn("approved", content["content"])
        self.assertNotIn("wrong target", content["content"])

    def test_installer_home_alias_does_not_overwrite_powershell_home(self) -> None:
        root = Path(self.temporary.name) / "matoloop-agent"
        root.mkdir()
        (root / "install.json").write_text(
            json.dumps({"source": "source", "homeDirectory": "workspace"}), encoding="utf-8"
        )
        installer = Path(__file__).parents[3] / "scripts/install-matoloop-agent.ps1"
        result = subprocess.run(
            [
                self.process.shell,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(installer),
                "status",
                "-Home",
                "workspace",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "LOCALAPPDATA": self.temporary.name},
        )
        self.assertIn("Home: workspace", result.stdout)

    def test_installer_elevation_rejects_another_account(self) -> None:
        installer = Path(__file__).parents[3] / "scripts/install-matoloop-agent.ps1"
        result = subprocess.run(
            [
                self.process.shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$tokens=$null; $errors=$null; "
                "$ast=[Management.Automation.Language.Parser]::ParseFile("
                "$env:TEST_INSTALLER, [ref]$tokens, [ref]$errors); "
                "$definition=$ast.Find({ param($node) "
                "$node -is [Management.Automation.Language.FunctionDefinitionAst] "
                "-and $node.Name -eq 'Register-AgentTasks' }, $true); "
                ". ([ScriptBlock]::Create($definition.Extent.Text)); "
                "Register-AgentTasks -InstallationRoot $env:TEST_ROOT -ExpectedUserSid 'S-1-0-0'",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env={
                **os.environ,
                "TEST_INSTALLER": str(installer),
                "TEST_ROOT": self.temporary.name,
            },
        )
        self.assertIn("Elevation must use the installing Windows account", result.stderr)

    def test_installer_preserves_unexpanded_user_path_and_registry_type(self) -> None:
        installer = Path(__file__).parents[3] / "scripts/install-matoloop-agent.ps1"
        # Redirect HKCU only inside the child process; never edit the user's real PATH.
        result = subprocess.run(
            [
                self.process.shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; $tokens=$null; $errors=$null; "
                "$ast=[Management.Automation.Language.Parser]::ParseFile("
                "$env:TEST_INSTALLER, [ref]$tokens, [ref]$errors); "
                "$ast.FindAll({ param($node) "
                "$node -is [Management.Automation.Language.FunctionDefinitionAst] "
                "-and $node.Name -eq 'Set-CommandPath' "
                "}, $true) | ForEach-Object { . ([ScriptBlock]::Create($_.Extent.Text)) }; "
                "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
                'public static class TestRegistry { [DllImport("advapi32.dll")] '
                "public static extern int RegOverridePredefKey(IntPtr key, IntPtr target); }'; "
                "$name='Software\\MatoLoop-Tests\\'+[Guid]::NewGuid().ToString('N'); "
                "$key=[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($name); "
                "$environment=$key.CreateSubKey('Environment'); "
                "$environment.SetValue('Path','%MATOLOOP_PATH_TEST_HOME%\\bin;C:\\existing', "
                "[Microsoft.Win32.RegistryValueKind]::ExpandString); $environment.Dispose(); "
                "try { "
                "if ([TestRegistry]::RegOverridePredefKey([IntPtr](-2147483647), "
                "$key.Handle.DangerousGetHandle()) -ne 0) { throw 'Registry isolation failed' }; "
                "$commandDirectory='C:\\agent\\bin'; Set-CommandPath $true; "
                "$environment=$key.OpenSubKey('Environment'); "
                "@{value=$environment.GetValue('Path',$null, "
                "[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames); "
                "kind=$environment.GetValueKind('Path')} | ConvertTo-Json -Compress; "
                "$environment.Dispose() "
                "} finally { "
                "[void][TestRegistry]::RegOverridePredefKey("
                "[IntPtr](-2147483647), [IntPtr]::Zero); "
                "$key.Dispose(); [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($name) }",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "TEST_INSTALLER": str(installer),
                "MATOLOOP_PATH_TEST_HOME": "C:\\old-tool",
            },
        )
        self.assertEqual(
            json.loads(result.stdout),
            {"value": "%MATOLOOP_PATH_TEST_HOME%\\bin;C:\\existing;C:\\agent\\bin", "kind": 2},
        )

    def test_installer_rollback_preserves_error_when_the_task_is_missing(self) -> None:
        installer = Path(__file__).parents[3] / "scripts/install-matoloop-agent.ps1"
        result = subprocess.run(
            [
                self.process.shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; $tokens=$null; $errors=$null; "
                "$ast=[Management.Automation.Language.Parser]::ParseFile("
                "$env:TEST_INSTALLER, [ref]$tokens, [ref]$errors); "
                "$activation=$ast.Find({ param($node) "
                "$node -is [Management.Automation.Language.TryStatementAst] "
                "-and $node.Body.Statements[0].Extent.Text -like "
                "'[[]IO.File]::WriteAllText($configFile,*' }, $true); "
                "function Stop-Agent {}; function Save-State {}; function Task { $null }; "
                "function Start-ScheduledTask { throw 'Task not found' }; "
                "$existing=$true; $previousConfig=$null; $previousInstaller=$null; "
                "$configFile=Join-Path $env:TEST_ROOT 'config.json'; "
                "try { & ([ScriptBlock]::Create(\"try { throw 'Startup denied' } catch \" "
                "+ $activation.CatchClauses[0].Body.Extent.Text)) } "
                "catch { Write-Output $_.Exception.Message }",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env={
                **os.environ,
                "TEST_INSTALLER": str(installer),
                "TEST_ROOT": self.temporary.name,
            },
        )
        self.assertEqual(result.stdout.strip(), "Startup denied")

    def test_installer_early_failure_preserves_a_caller_snapshot_directory(self) -> None:
        sentinel = self.home / "keep.txt"
        sentinel.write_text("User data", encoding="utf-8")
        root = Path(self.temporary.name) / "installation"
        root.mkdir()
        installer = Path(__file__).parents[3] / "scripts/install-matoloop-agent.ps1"
        # Run the real preparation block without installing tools or touching Scheduled Tasks.
        subprocess.run(
            [
                self.process.shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; $tokens=$null; $errors=$null; "
                "$ast=[Management.Automation.Language.Parser]::ParseFile("
                "$env:TEST_INSTALLER, [ref]$tokens, [ref]$errors); "
                "if ($errors) { throw 'Invalid installer syntax' }; "
                "$start=$ast.Find({ param($node) "
                "$node -is [Management.Automation.Language.AssignmentStatementAst] "
                "-and $node.Left.Extent.Text -eq '$env:VIRTUAL_ENV' }, $true).Extent.StartOffset; "
                "$end=$ast.Find({ param($node) "
                "$node -is [Management.Automation.Language.TryStatementAst] "
                "-and $node.Body.Statements[0].Extent.Text -like '$python = & $uv python find*' "
                "}, $true).Extent.EndOffset; "
                "$root=$env:TEST_ROOT; $newVenv=Join-Path $root 'venv'; "
                "$installer=Join-Path $root 'installer.ps1'; $previousInstaller=$null; "
                "$managedSource=$false; $checkout=$null; $snapshot=$env:TEST_KEEP; "
                "function Fail($message) { throw $message }; "
                "$uv={ $global:LASTEXITCODE=1 }; "
                "try { & ([ScriptBlock]::Create("
                "$ast.Extent.Text.Substring($start, $end-$start))) } catch {}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env={
                **os.environ,
                "TEST_INSTALLER": str(installer),
                "TEST_ROOT": str(root),
                "TEST_KEEP": str(self.home),
            },
        )
        self.assertTrue(sentinel.exists())

    def test_installer_finds_private_git_without_git_on_path(self) -> None:
        root = Path(self.temporary.name) / "agent root"
        executable = root / "tools/git/cmd/git.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        installer = Path(__file__).parents[3] / "scripts/install-matoloop-agent.ps1"
        result = subprocess.run(
            [
                self.process.shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; $tokens=$null; $errors=$null; "
                "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
                "$env:TEST_INSTALLER, [ref]$tokens, [ref]$errors); "
                "if ($errors) { throw 'Invalid installer syntax' }; "
                "$definition=$ast.Find({ param($node) "
                "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] "
                "-and $node.Name -eq 'Resolve-Git' }, $true); "
                ". ([ScriptBlock]::Create($definition.Extent.Text)); "
                "$root=$env:TEST_ROOT; function Fail($message) { throw $message }; Resolve-Git",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env={
                **os.environ,
                "PATH": "",
                "TEST_ROOT": str(root),
                "TEST_INSTALLER": str(installer),
            },
        )
        self.assertEqual(result.stdout.strip(), str(executable))

    def test_maintenance_handoff_uses_only_a_fixed_task_and_bounded_job(self) -> None:
        from matoloop_agent.platform_windows import WindowsPlatform

        platform = WindowsPlatform()
        process = AsyncMock()
        process.returncode = 0
        with (
            patch.object(platform, "_maintenance_available", return_value=True),
            patch(
                "matoloop_agent.platform_windows.asyncio.create_subprocess_exec",
                return_value=process,
            ) as start,
        ):
            asyncio.run(
                platform.start_maintenance(
                    self.config,
                    "update",
                    "2ea504cc-e3c6-4e0d-aa05-080328100fe8",
                    None,
                    "a" * 40,
                )
            )
        self.assertEqual(
            json.loads((self.config.data_dir / "maintenance-request.json").read_text()),
            {
                "action": "update",
                "jobId": "2ea504cc-e3c6-4e0d-aa05-080328100fe8",
                "revision": "a" * 40,
            },
        )
        self.assertIn(
            "Start-ScheduledTask -TaskName 'MatoLoop Agent Maintenance'", start.call_args.args[-1]
        )

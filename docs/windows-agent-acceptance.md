# Native Windows Agent acceptance

The backend with Windows path support must run before pairing the Windows Agent.
Test a published backend containing commit-based Agent setup. The downloaded
installer selects that backend's exact commit; no branch option is needed. For
local development, run `scripts\install-matoloop-agent.ps1` from the intended checkout.
Run the installer as a regular user under the intended task account. Approve the
UAC request for task registration using that same Windows account; switching to
another administrator account is rejected. Internet access and access to the repository are needed for a fresh
standalone installation. If Git is missing, the installer downloads checksum-verified
MinGit for x64 or ARM64 Windows into its private `tools\git` directory.

The installer checks the new Agent executable before activating it. Shared
filesystem helpers must remain importable on Windows; the Linux `renameat2`
binding is loaded only on Linux.

1. Install with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File
   C:\path\to\checkout\scripts\install-matoloop-agent.ps1 -Source
   C:\path\to\checkout -InstanceUrl https://your-matoloop-host`.
   Enter the installing account's password (not its Windows Hello PIN) at the
   masked prompt in the task registration window. Cancelling UAC or an incorrect
   password must report the original error. Interactive pairing supplies the backend
   URL; failure must leave the installation intact for `matoloop-agent pair` to retry.
   Also test the downloaded installer on a machine without
   Git in `PATH`. Confirm `tools\git\cmd\git.exe` runs and the checkout matches the backend SHA.
   The private Git must remain available for local and portal updates without
   changing the system `PATH`. Installing an existing checkout also needs Git for version stamping.
2. Restart the terminal and run `matoloop-agent status`. Verify
   `Get-ScheduledTask -TaskName 'MatoLoop Agent'` is running and the home is
   `%USERPROFILE%\mato-loop-agent\workspace` (or the chosen external directory).
   Only the stable `bin` directory enters the user PATH; existing `%JAVA_HOME%`
   references and the registry type must remain unchanged.
3. Run the native tests with the active environment from `install.json`:
   `$state = Get-Content "$env:LOCALAPPDATA\matoloop-agent\install.json" | ConvertFrom-Json;`
   `& (Join-Path $state.activeVenv 'Scripts\python.exe') -m unittest discover
   -s C:\path\to\checkout\apps\matoloop-agent\tests -p test_windows_runtime.py -v`.
4. If pairing was skipped during installation, create a computer in
   **Einstellungen > Computer** and run `matoloop-agent pair`. Enter the
   displayed computer ID and pairing token; the backend URL must not need to be
   entered again. Confirm the platform is `win32` and system info says Windows.
   Keep the startup task running during pairing: it must wait for the credential
   lock and connect after pairing completes, without requiring a manual restart.
5. Select the Windows workspace. Browse and create a folder, create/edit/read a
   UTF-8 file, search it, upload/download it, and use an approved PowerShell
   `Get-Content` and `Set-Content` command. Confirm `C:/` breadcrumbs and
   outside-workspace approvals. The native tests also reject file symlinks.
6. Restart Windows **without signing in** and check that the task becomes
   running and the computer reconnects. Change the backend image to a newer supported
   commit and use **Einstellungen > Computer > Agent aktualisieren**; also test a backward transition.
   Verify the maintenance result becomes completed, the Agent reconnects, and the
   pairing and workspace stay intact. `MatoLoop Agent Maintenance` must run independently
   of the boot-time Agent task and return to Ready when the update finishes.
   On the installation without system Git, also run the local `update` command
   and confirm both update paths reuse private Git without downloading it again.
   Verify `matoloop-agent --help` after updates. The current bootstrap must repair
   missing tasks even when targeting an older installer. A blocked launcher write
   must fail before stopping the existing Agent. Local checkout edits must remain
   untouched. An unavailable target commit must report failure, not success.
7. Run `uninstall`; confirm the workspace and pairing data remain. Repeat an
   installation and run `uninstall -Purge`; confirm the managed directory is
   removed while the workspace remains. Both modes remove private Git, the CLI
   launcher, and its own user PATH entry.
8. For an installation made by the previous published installer, update through the
   WebUI and rerun downloaded setup once. After restarting the terminal, verify
   `matoloop-agent status` without losing pairing or the workspace.

Do not treat the native Windows behavior as verified until these steps pass on
the target Windows installation. Linux Docker tests exercise protocol and
backend compatibility but cannot run Windows APIs or Scheduled Tasks.

Filesystem RPCs traverse directories through no-reparse handles and perform
file mutations relative to those handles. Relative renames use
`NtSetInformationFile` with `FILE_RENAME_INFORMATION` and the open destination
directory handle. Recursive directory creation reopens an existing directory
handle with an empty relative name instead of `"."`. Native tests swap a parent
directory after opening its parent handle but before opening the leaf file, and
check that only the originally opened directory is used for reads, writes and
deletes. Windows rejects parent-directory renames while a leaf file is open.
This Windows-specific behavior still needs to pass
on the target VM before the no-follow contract can be considered verified.

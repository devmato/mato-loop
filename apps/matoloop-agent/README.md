# MatoLoop Agent

Agent commands run in fresh processes with explicit directories and environments.
The Control Center displays their live output and saved results separately from
the personal interactive terminal. Shell inspection classifies commands without a
recognized write as `shell.read`, even when analysis is incomplete; this is not a
sandbox guarantee.

Windows shell commands inherit only an allowlisted environment, including
`ProgramFiles` and `ProgramFiles(x86)` for tools such as NuGet. Other host variables,
including Agent credentials, are not inherited automatically.

On Windows, shell completion follows the shell process exit, not the lifetime of
detached children. Output is drained for up to one second after exit; channels
still held open are closed and reported as truncated. Detached children are not
managed background jobs, and a successful launch does not prove service readiness.
Redirect detached service output to files and check readiness separately.

Standalone agent with a portable connection and dispatch core. Linux provides bounded filesystem, process, POSIX terminal, transfer, OpenSCAD, browser, credential-storage, and session-cache operations. Native macOS on Apple Silicon provides POSIX files, Bash shell, PTYs, search, transfer, optional browser, and cache operations without administrator execution. The Windows adapter advertises its native filesystem, PowerShell, search, transfer, browser, and cache operations. It connects outbound to the MatoLoop backend and has no backend package or database dependency.

Platform adapters compose host operations behind the common core. Linux and macOS use normalized absolute POSIX paths; Windows uses forward-slash drive-letter paths such as `C:/Users/Ada/workspace` on the wire. Native Windows paths are converted at the Windows adapter boundary. macOS reports `platform: darwin`; the protocol, database, and existing Computer UI are shared. Unsupported capabilities are not advertised.

The [browser runtime](BROWSER.md) provides disposable per-chat Chromium profiles,
isolated per-chat headless automation and graphical viewing on native/container agents, explicit prerequisite setup, and bounded browser actions.

## Computer-bound MCP over stdio

Configured stdio MCP servers run with the Agent account's operating-system access,
not in a sandbox or under a RAM quota. Saving a connection does not launch it;
catalog refresh uses a separate short-lived discovery process. Authorized tool
calls lazily start or reuse one process per chat and connection on the selected
computer. The default idle timeout is 30 minutes and does not interrupt active
calls; separate call deadlines still apply. At most four MCP processes run at
once, including discovery. Users can close idle processes under **Aktive Sitzungen**.
Process state is volatile: explicitly save or export important work rather than
assuming autosave. See [computer-bound stdio](../../docs/integrations.md#computer-bound-stdio)
for configuration and lifecycle details.

## Configuration

```json
{
  "homeDirectory": "/home/mato/workspace",
  "shellExecutable": "/bin/sh"
}
```

`homeDirectory` is mandatory and canonicalized at startup. The configuration file's parent directory is the agent data directory for credentials, transfers, and cache data. Native Linux lifecycle installations read `${XDG_DATA_HOME:-~/.local/share}/matoloop-agent/data/config.json` by default; macOS uses `~/Library/Application Support/MatoLoop Agent/data/config.json`. `MATOLOOP_AGENT_CONFIG` or `--config` selects another file, including `/data/config.json`.

Personal terminal.open and terminal.run require POSIX PTYs and a configured
`shellExecutable`. Unsupported operations are omitted from the advertisement.

Run `matoloop-agent pair` to enter the backend WebSocket URL, agent ID, and one-time pairing token. Non-interactive pairing uses `MATOLOOP_AGENT_BACKEND_URL`, `MATOLOOP_AGENT_ID`, and `MATOLOOP_AGENT_PAIRING_TOKEN`. `ws://` requires `allowInsecure: true`.

Run locally:

```bash
uv sync
uv run matoloop-agent --config config.example.json pair
uv run matoloop-agent --config config.example.json run
uv run matoloop-agent --config config.example.json cache-clear
```

## Native Linux installation

From a MatoLoop checkout, run the interactive installer as a normal user:

```bash
./scripts/install-matoloop-agent.sh
```

Or copy the instance-specific command from **Einstellungen > Computer**:

```bash
curl -fsSL https://mato.example/api/execution-agents/install.sh | sh -s -- --instance-url https://mato.example
```

The bootstrap installs pinned, private `uv` and Python runtimes below
`~/.local/share/matoloop-agent`; it offers to install only the Agent's missing Debian/Ubuntu system
packages and never changes shell profiles. Browser Python, Chromium, and browser host dependencies
are installed later through the browser integration setup. From a
checkout it uses the current source. A standalone script requires `git` to obtain the source
repository because there is no native release package. The setup asks for the Agent home directory
and normal MatoLoop instance URL, derives the WebSocket endpoint, and reuses the Agent's secure
pairing prompt. An `http://` instance enables insecure transport; use `https://` outside a trusted
local network.

The systemd system service starts at boot and runs with the installing user's permissions. The
installer uses `sudo` only to manage that service and optional system packages. Repeating `install`
deploys the checkout as it currently exists and repairs an interrupted installation. The same script is
installed as the `matoloop-agent` command; there is no second wrapper implementation:

```bash
matoloop-agent update
matoloop-agent install
matoloop-agent status
matoloop-agent uninstall
matoloop-agent uninstall --purge
```

Standalone setup and `update` retrieve the running backend's full commit SHA from
`/api/execution-agents/installation-target`. Source and dependencies are prepared in a new
managed checkout and the fetched SHA is verified before replacing the running version.
Updates can move forward or backward; they do not change a user's original checkout.
There is no branch prompt. Use `install` from a checkout to deploy local development changes.
Dirty or unknown backend builds cannot supply a reproducible managed installation target.
`uninstall` preserves credentials, while `uninstall --purge` removes all managed Agent files. Both
offer to remove only the system packages recorded as newly installed by MatoLoop. Agent
home directories and workspaces are always preserved. The first platform implementation targets
Linux system services with systemd. Browser and personal terminal capabilities remain unavailable
when their host prerequisites are declined or unsupported.

## Native macOS installation (Apple Silicon)

In **Einstellungen > Computer**, select **macOS (Apple Silicon)** and run the displayed
command in Terminal as the user who will own the Agent. The existing `install.sh`
endpoint selects the OS automatically. Intel Macs and Rosetta are not supported.
Install Apple's Command Line Tools first if Git is missing (`xcode-select --install`).
No Homebrew, root account, or sudo is required. The installer keeps its pinned private
`uv` and Python under `~/Library/Application Support/MatoLoop Agent`; it does not
modify shell profiles. Use `~/.local/bin/matoloop-agent` if that directory is not on
your `PATH`. Pair with the Computer ID and one-time token from the UI.

The user LaunchAgent at `~/Library/LaunchAgents/com.matoloop.agent.plist` starts at
login, not before login, and stops at logout. There is no tray app or system daemon.
The existing CLI provides `pair`, `install`, `update`, `status`, `uninstall`, and
`uninstall --purge`. CLI and WebUI updates use the running backend's exact commit,
not a branch or Homebrew package. Uninstall preserves credentials unless purged;
workspaces are never removed.

The configured shell is `/bin/bash`, using the existing shell inspector. POSIX files
and interactive PTYs are available. The installer supplies pinned, checksum-verified
arm64 ripgrep privately at `tools/bin/rg` for file search; no Homebrew setup is needed.
OpenSCAD and administrator execution are not advertised. The Linux administrator
implementation relies on `/proc` and is not a macOS elevation mechanism.

macOS privacy controls (TCC) still govern Desktop, Documents, Downloads, and other
protected folders. A shell or MatoLoop approval does not grant OS access. If access
is denied, use an accessible workspace or review the relevant permissions in macOS
System Settings; do not bypass TCC. A LaunchAgent may have a different access context
from Terminal.

Browser support is optional. Use **Browser einrichten** in the existing Browser
integration to install pinned packages and matching Chromium privately without
administrator rights or interactive input. Setup output is read-only and replayable,
as on Windows; opening a browser never installs it automatically.

### Native macOS acceptance

Linux/container checks do not verify launchd, TCC, or native Chromium on Apple Silicon.
Before declaring native acceptance, check on an arm64 Mac: fresh install and pairing,
login/logout autostart, CLI status and backend-pinned update/rollback, WebUI maintenance,
POSIX file mutations/search, Bash and PTY interaction, protected-folder denial, optional
Browser setup/replay and sandboxed readiness, automation/viewer/file staging and chat
isolation, and uninstall with workspace preservation. Native acceptance is not implied
by the implementation or by passing Linux suites.

## Native Windows installation

Open **Windows PowerShell as a regular user under the account that should own the Agent**.
Copy the Windows installation command from **Einstellungen > Computer** and approve
UAC for task registration with that same account, not another administrator account.
The script installs its own `uv` and Python under `%LOCALAPPDATA%\matoloop-agent` and creates
`%USERPROFILE%\mato-loop-agent\workspace` as the default Agent home. A startup
Scheduled Task runs under the installing account, using a password supplied only
to Windows Task Scheduler at installation. For cloning and updates, the installer
uses Git from `PATH`, reuses its private Git, or downloads pinned MinGit 2.56.0.2
for x64 or ARM64 Windows into `tools\git` after checking the archive's SHA-256.
Network access to the MatoLoop source repository is required; an existing checkout
can be provided with `-Source`; Git is still used to stamp its source version. No system Git installation
or machine-wide `PATH` change is made. Only the stable launcher directory is added
to the user `PATH`, preserving existing variable references and the registry value
type. Windows PowerShell is the shell for `shell_run`.

Interactive setup starts pairing with the backend URL supplied; enter the computer
ID and token from the UI. Restart the terminal to use `matoloop-agent pair`, `update`,
`status`, `uninstall`, or `uninstall -Purge`. CLI setup finishes before stopping an
existing Agent; a failed version switch may keep the compatible launcher. Pairing
failure leaves the installed Agent intact.

**Existing Windows installations need one rerun of the downloaded installation
command to acquire the CLI launcher.** Do not uninstall or purge first; pairing and
the workspace are preserved.

The setup also registers an on-demand maintenance task. **Agent aktualisieren**
in the Computer settings uses that task and the same installer update function;
both tasks run under the installing account without elevation. Task Scheduler
stores the account password, while the Agent's own pairing state uses DPAPI.
Setup and updates follow the running backend's exact commit, including backward transitions. Deinstallation
removes the private tools, including MinGit, and never removes the workspace.
Browser support is optional: use **Browser einrichten** in the browser integration
to install Playwright, Pillow, and matching Chromium in the private Agent directories.
Setup is non-interactive and needs no administrator privileges. Updates retain the
browser packages and probe matching Chromium before activation.
OpenSCAD and interactive terminals are not advertised by the Windows adapter.

## Idle sleep during local work

The native Agent prevents automatic idle sleep while handling local operations, including browser
setup, and for five minutes after the last one finishes. Idle connections and background polling
do not extend that period. Commands typed into the personal terminal are not tracked through
completion. Stopping the Agent releases the assertion without the grace period, as does a backend
disconnect unless browser setup is still running locally. The Agent does not keep the display on
or prevent screen locking.
Do not rely on it to prevent sleep when closing the lid or explicitly putting the computer to sleep.

macOS and Windows use native power management APIs. On Linux the Agent uses logind and falls back
to an idle-only lock if the combined idle and sleep lock is denied. Support depends on the desktop
and its power policy. Container Agents do not inhibit host sleep. If the OS does not grant an
assertion, local work proceeds normally.

To verify on macOS, run a shell command longer than the configured idle sleep time. Check
`pmset -g assertions` for `MatoLoop Agent work` while it runs, and confirm that the assertion
disappears five minutes after the work finishes or immediately when the Agent disconnects.

## Container updates and development checks

Managed Docker Agents can update to the running backend's exact commit through **Agent aktualisieren** or `matoloop-agent update`. Updates survive restart; recreation restores the selected image version. Data and workspaces remain in their mounts. See [computer maintenance](../../docs/computer-maintenance.md) for lifecycle and recovery details and [Windows acceptance](../../docs/windows-agent-acceptance.md) for native validation.

Run repository checks from the root through the development stack:

```bash
./scripts/dev.sh check matoloop-agent
./scripts/dev.sh test matoloop-agent
```

## Protocol v1

The authenticated WebSocket sends strict `hello`, `request`, `cancel`, `response`, and `event` messages with `protocolVersion: 1`. Hello advertises `agentId`, `agentVersion`, `computerName`, `platform`, canonical `homeDirectory`, and operations.

Linux filesystem operations use normalized absolute POSIX paths and traverse from an open `/` descriptor with `O_NOFOLLOW` on every component. Windows uses normalized forward-slash drive paths and rejects reparse-point traversal. Copy and move use absolute source and destination paths. Process and shell working directories are absolute. `shell.inspect` returns absolute targets and an inspection revision without root metadata.

The fixed terminal operations are `terminal.open`, `terminal.read`, `terminal.write`, `terminal.resize`, `terminal.close`, and `terminal.run`. Interactive shells and one-shot terminal runs execute with the agent operating-system user's rights. PTYs are live process state and are not persistent terminal sessions.

File size, transfer size, response size, entry count, timeout, cache, and atomic mutation protections remain enforced. The agent is not an operating-system sandbox; deploy it under an appropriate container, VM, operating-system user, and network policy.

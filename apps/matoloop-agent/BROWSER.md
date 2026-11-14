# Browser Runtime

Native Linux, native Windows, and Linux container Agents expose the same
`integration.browser.*` operations. Chromium
runs headless with its renderer sandbox enabled. There is no X11 desktop, VNC process or
public debugging listener. The backend owns authorization and chat bindings; the Agent
owns browser processes, website traffic, temporary profiles and page input.
On Linux, the user agent uses the installed Chromium major version with the regular `Chrome`
token, avoiding blanket `HeadlessChrome` blocks. Headless mode, automation signals
and the renderer sandbox remain unchanged; this does not guarantee website access.

## Runtime and Storage

`BrowserManager` routes opaque browser IDs to `BrowserRuntime` instances. Each runtime
reuses the existing worker subprocess, typed private JSON pipes, execution lock, file
staging and shutdown path. It launches one disposable Playwright persistent context in
its private temporary directory. The context is persistent only for that runtime's
lifetime; no profile is loaded from disk on the next open. The backend binds each runtime
to a single owner/chat/computer. Eight concurrent workers are allowed per Agent.

The protected backend/Agent/owner namespace remains under `<data>/tools/browser`.
Root Linux Agents launch workers as `matoloop-browser` without supplementary groups;
non-root Linux and Windows Agents use their own account. Windows profiles and staging
stay beneath that namespace, inheriting the managed installation's account-restricted
ACL. Storage traversal refuses reparse points through the existing Windows file handles;
Linux also verifies numeric ownership. Worker environments are allowlisted and contain no Agent
credentials. Existing
old `browser/profile` directories are not loaded or deleted automatically.

Worker shutdown has a 10-second cleanup budget, runtime termination 12 seconds, and
Agent shutdown retains its existing overall deadline. Browser workers close concurrently.
Windows browser workers and setup processes own non-inheritable, kill-on-close Job Objects
before starting children; process exit also terminates surviving descendants.
Temporary profiles and staging are removed on orderly close; abrupt host failure cannot
guarantee cleanup. Completed downloads stream to backend session files with SHA-256
verification; worker staging is deleted only after successful backend storage. Existing
computer downloads are not moved or removed. Transfers support 1 GiB per file and 2 GiB
aggregate staging, while observations retain their separate 64 MiB limit.
Linux additionally caps in-progress download files through `RLIMIT_FSIZE`; Windows
checks completed downloads before retaining them but has no corresponding hard
in-progress file-size limit. Allow disk headroom for unfinished downloads on Windows.

## Viewer Protocol

`integration.browser.view` opens the existing outbound authenticated binary relay from
the runtime's private Unix `view` socket on Linux. Windows uses an ephemeral
`127.0.0.1` listener authenticated with a per-worker 256-bit secret passed only through
the private worker pipe. Authentication must finish within five seconds before any
frames or input are accepted. This is the typed viewer stream, not a CDP endpoint.
The backend supplies an opaque relay ID and
one-use token, never an arbitrary URL or port. Closing a viewer does not close Chromium.

Both directions use a four-byte big-endian payload length followed by UTF-8 JSON:

- Worker output: at most 4 MiB; `type=frame` contains the CDP JPEG as base64, or `type=state`
  publishes metadata only. Metadata includes selected `tabId`, `inputEpoch`, manual-control
  state, viewport width/height and an optional website JavaScript dialog.
- Viewer input: at most 32 KiB, validated as `BrowserInput`. Supported kinds are mouse,
  key, text, release and dialog. No raw CDP request is accepted. Coordinate, key, text,
  numeric and UTF-8 limits apply before dispatch.

Only the latest pending screencast frame is retained. Acknowledgments follow bounded
socket writes; slow consumers cannot grow an unbounded frame queue. Images are drawn
only if their tab/epoch still matches; input is enabled only after a matching image has
been drawn. Viewer frames are never persisted or inserted into model context.

The worker checks membership of the live viewers, selected tab, input epoch and manual
flag under the input lock. Each model action disables input, releases held keys/buttons
and rotates the epoch before acknowledgment. Returning to manual control rotates the epoch
and restarts screencasts without replacing viewer streams. Multiple viewers may subscribe;
joining or leaving does not close another stream. Changing input sources releases held input.
Backend runtime-ID and snapshot guards protect model operations and post-approval execution.

## Controls and Tools

Host navigation retains typed state/new/select/close/navigate/back/forward/reload/stop
operations. Native chrome controls are removed. Navigation acknowledgment means accepted,
not page-load completion; bounded CDP history/loading observations keep Stop usable.
Resize changes all runtime page viewports without restarting Chromium. Website popups
become tabs within the same runtime. Website alerts/confirmations/prompts are rendered
by the WebUI during manual control.

Clipboard endpoints never access the host clipboard. Writing inserts bounded literal
text in the focused page. Reading/copying returns the focused frame's selection, excluding
password inputs. The frontend owns explicit local clipboard access. Copy uses the Clipboard
API or Quasar's HTTP-compatible fallback without a dialog; paste retains its text dialog.
This is text selection/insertion, not native copy/paste menu or custom copy-handler emulation.

Model tools retain the existing execute envelope, AI accessibility snapshots and ref
resolver. Reading includes navigation, new/select tabs and scrolling. Interaction is write.
Website JavaScript requires browser use; Python additionally requires a distinct code grant resource. Free Python retains
worker-account OS authority; this is not a language sandbox or protection against code
reaching other runtime data. No browser-specific permission engine is introduced.

Existing file transfer, managed download origin, observation guards, snapshot-file
staging, screenshot scaling, code-result and diagnostic bounds remain unchanged. Actual
approval waits capture a guard and revalidate before effects; automatic operations do
not take whole-page approval snapshots. Unknown effects are not automatically replayed.

## Setup and Verification

Both images and managed container updates call `container-browser-setup.sh` for the
pinned Playwright Chromium and host libraries. Native installation remains explicit,
with interactive consent for host package installation. Readiness performs a real
sandboxed headless launch and screenshot. Missing sandbox support fails closed.

Windows setup runs non-interactively under the Agent account and uses the managed
`uv.exe` to install the pinned Python packages and matching Chromium without elevation.
Its setup output uses the same bounded replay API; there is no interactive setup terminal.
Setup inherits only explicit proxy, certificate and Playwright download-host settings
from the Agent environment; browser workers do not inherit those installation settings.
Managed Windows updates include the browser dependency group and probe matching Chromium
before activation when browser support was installed. Targets predating Windows browser
support remain installable without advertising browser operations.

Run checks with `./scripts/dev.sh`. The Agent suite verifies real independent browser
profiles, page streaming, Unicode input and server-side input fencing in the development
container. Backend tests retain owner/origin/runtime/file-policy and transport checks;
frontend tests cover framing, stale images, input gating and Quasar website dialogs.
The Windows CI job runs `test_windows_browser.py` with native sandboxed Chromium,
including worker pipes, authenticated streaming, Python/JavaScript automation, file
staging and profile isolation. Scheduled Task acceptance remains a separate native check.

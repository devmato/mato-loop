# Browser Architecture and Operations

The optional personal Browser integration runs Playwright-managed Chromium on the
selected owner-bound MatoLoop Agent. Native Linux, native Windows, and Linux container agents use the same
headless runtime, private viewer relay, and manual controls. No VNC or desktop server
is used. Website traffic originates on the Agent, not the backend or the user's browser.

## Availability and Setup

- Configure one personal Browser integration per owned computer under **Einstellungen > Integrationen**.
  Existing adapter availability, integration selection and agent-profile restrictions apply.
- Setup is explicit. Native setup installs the pinned Playwright/Pillow packages and matching
  Chromium; missing Debian/Ubuntu host packages require interactive consent. Container images
  and managed updates share `container-browser-setup.sh`. Opening a browser never installs software.
  Windows setup is non-interactive, installs into the private Agent directories, and needs no elevation.
- Readiness performs a disposable sandboxed launch and renderer screenshot. Chromium's renderer
  sandbox remains mandatory; there is no `--no-sandbox` fallback. Prepared Compose stacks retain
  their existing user-namespace/security settings. No debugging or viewer TCP port is published.
- The independent setup terminal retains its bounded replay buffer across viewer reconnects,
  but not an Agent process restart. Do not restart an Agent during package installation.
  Windows exposes the same replay buffer as read-only setup output rather than a terminal.
- Native updates retain browser support when the managed Chromium installation exists, including
  installations whose Python packages were lost by an earlier update. The staged environment includes
  the release's browser dependency group and installs and probes matching Chromium before activation.
  Preparation failure leaves the running release unchanged. Playwright retains Chromium versions
  referenced by the active and staged environments and removes unused versions on later installs;
  additional host packages still require explicit setup consent.

## Isolation and Lifetime

Each chat/computer binding has a separate disposable worker and Chromium profile. Tabs in one
chat share that profile; other chats receive neither its tabs, cookies, logins, local storage
nor service workers. Up to eight browser workers may be active on one Agent. This reuses the
existing process cleanup and IPC boundaries rather than adding shared-browser context management.

The runtime keeps its profile in a private temporary directory, not a workspace or backend
session directory. Cookies are not restored after runtime close or Agent restart.
Existing backend/Agent/owner binding checks remain. Old persistent profiles are neither loaded,
migrated, nor deleted automatically. They may still contain sensitive credentials on disk and
require explicit operator cleanup. There is no persistent-profile reset API or UI.

The model and human share their chat's runtime independently of the Control Center app.
Switching chats opens a distinct binding. Multiple views of the same chat subscribe without
takeover; each view retains its own authenticated login binding. Closing a view or the app does
not end the runtime. The WebUI confirms explicit browser close because it removes logins and
unsaved website input for every view. Explicit close, invalidated session/integration access, or Agent
disconnect ends it. The existing worker limits bound retained runtimes. Worker cleanup remains
bounded and runs before Agent shutdown completes.
On Windows, workers use the Agent account and the managed installation's restricted ACLs;
storage and staging reject reparse-point traversal. The local viewer transport uses a
per-worker secret on an ephemeral loopback listener instead of Linux's private Unix socket.
Neither exposes raw CDP. See the [runtime contract](../apps/matoloop-agent/BROWSER.md)
for platform boundaries, including Windows's lack of a hard in-progress download-size cap.

## Viewer and Model Access

Visible activation of **Control Center > Browser** subscribes a viewer. Browser tools are offered
when the selected computer, integration and profile allow them. The first tool action starts the
chat's browser if necessary; no app entry, attachment, control request or viewer is required.
Ordinary action permissions remain authoritative. Concurrent starts reuse one runtime per chat
and computer. All views share one viewport; an explicit resize affects every view.

With on-demand tool loading, the integration catalog explains browser access before its schemas
are loaded and directs the model to discover `browser_tabs`. This hint follows effective tool
availability without eagerly loading schemas or probing the runtime.

For interactive and visual UI checks, the model must use the chat's Browser integration when
available and suitable before installing or launching a separate browser. Python/Playwright checks
use `browser_run_code` with its independent permission. Existing project browser test suites still
run normally; denied or disabled browser access must not be bypassed through scripts or another browser.
Regardless of tool-loading mode, the model must verify that the target app serves the intended
working copy before validating a code change.

The Agent streams the selected page through CDP `Page.startScreencast`; the WebUI draws JPEG
frames and sends typed mouse, keyboard, text and dialog input. Tabs, address, history, reload
and stop remain Quasar controls. Resize changes the actual viewport (320x240 to 3840x2160),
coalesced through the existing control socket. Chromium's native chrome, Settings and native
DevTools are not part of the viewer. Website JavaScript dialogs use an explicit Quasar dialog.

Mouse movement drives Chromium's own hover effects; leaving the image clears remote hover.
The existing state stream reports the standard CSS cursor at most ten times per second during
manual interaction, including same-origin/cross-origin frames and open shadow roots. Playwright
resolves each iframe's own execution context with frame-local coordinates; website security
settings remain unchanged. The viewer accepts only cursor keywords, never cursor image URLs.
For `cursor: auto`, selectable text is detected at the pointer rather than treating the whole
element as text. Cursor samples remain usable while the pointer moves within the same live page;
leaving, control changes and tab changes still clear them.
Tab/Shift+Tab navigate website fields. Ctrl/Cmd+L/T/W, Ctrl/Cmd+R and F5 use the existing navigation
controls; Ctrl+Tab/Shift+Tab and Ctrl+PageUp/PageDown cycle website tabs when the local browser
delivers those keys. Browser-reserved shortcuts cannot be overridden reliably by a web app.

Manual input and clipboard/file UI operations are blocked only while a model browser action is
running, on the server and worker rather than only in the frontend. A muted **Agent aktiv** label
appears in the existing tab strip during the action without changing its layout. The worker releases
held keys/buttons and rotates its input epoch at control changes; the same stream remains open.
Inputs must name the current view epoch and selected tab, so old queued input cannot replay after
unlocking. Manual input is restored after success, failure, or cancellation. Approval snapshots also
briefly lock input; the final approved action rechecks the snapshot while holding control.
Website dialogs can be answered after reconnecting, before the first image; ordinary input still
requires the current displayed image. Stream changes and resizing do not wait for an open dialog.
For human interaction the model asks and waits, without a handoff tool or polling.
Ctrl+Alt+Escape releases local viewer focus. Switching input sources releases held keys/buttons;
joining or leaving another viewer does not replace the runtime or grant model access.

## Permissions

The integration uses the shared resolver, categories, presets and persistent approval requests:

| Capability | Category / scope |
| --- | --- |
| `browser.use` | `write`: all ordinary browser actions, observations, and JavaScript in the website context. |
| `browser.code` | `write`: unrestricted Python/Playwright, with a separate execution grant resource. |

Returning control is automatic and has no configurable permission capability.

Browser use defaults to AUTO for Build and profiles based on Build, except in `ask_all`.
Allgemein, Plan, Sysadmin, and other profiles default to ASK, independently of model trust and
the other preset categories. Browser Python defaults to ASK in every profile and preset.
Explicit connection/profile rules and matching grants retain their existing precedence.
Browser use deliberately includes website JavaScript, authenticated content, and private-network
targets made accessible in the chat; it is not the public-network-only contract of `webfetch`.
File access retains its independent permissions. Ordinary browser grants never authorize Python.
Reusable grants bind the browser context and
integration revision, not a transient viewer or attachment revision. A fresh context cannot
reuse the previous context's grants. Invocation checks bind the runtime ID and live access, so
old queued or approved calls cannot act in a replacement runtime. Disabled actions remain binding.

Uploads retain their independent source-read check. Saving downloads retains the destination-write
check. Secret access uses the ordinary secret permissions. Website clicks cannot be reliably
classified by business effect: a write-authorized click may delete data or make a purchase.
**Automatisch außer Löschen** does not prevent destructive website actions.

The existing session Stop action cancels an active agent browser command through the Agent
cancellation channel, independently of the browser action lock, and remains visible beside draft sending.
Mobile Control Center panels
expose that same session action because they cover the chat composer. Browser Stop only stops
manual page loading. Cancellation cannot undo website effects; an interrupted worker may be
terminated to avoid reusing an uncertain RPC stream, losing its temporary logins and tabs.

### Code Authority

`browser_run_code` executes unrestricted Python with the worker account's OS rights. It is
not a browser-only sandbox: accessible files, processes, network services and other browser
data may be reachable. Separate chat profiles are website-state isolation, not OS isolation
against explicitly authorized code or ordinary computer tools. Root Agents use the dedicated
browser account and a credential-free environment; native non-root Agents use their own account.
Use external OS isolation or disable code where this authority is unacceptable.

`browser_evaluate` runs JavaScript in the selected page and can mutate it or send authenticated
requests. It requires only `browser.use`; Python requires both `browser.use` and `browser.code`,
even with `observe=false`. The code grant resource remains distinct from ordinary browser use.
Source and results remain bounded, errors controlled, and unknown outcomes are never retried
automatically. See [Browser Performance](browser-performance.md) for observation behavior.

Migration `6b6ce77be67f` discards saved browser read/write/use/Python policies at every settings
level and removes browser-specific grants. The new profile defaults apply afterward.
Pending browser approvals expire. Integration availability and all non-browser rules and grants,
including independent file permissions, remain unchanged. Discarded consent cannot be restored by downgrade.

## Clipboard and Files

Manual text insertion uses the focused website field and bounded UTF-8 text, never the host
system clipboard. Copy reads the selection in the focused website frame, including text fields
but not password fields. Custom website copy handlers and native clipboard menus are not
emulated. Select text with the mouse or keyboard and use Ctrl/Cmd+C or Ctrl/Cmd+V directly in
the viewer. Command shortcuts are translated to Control for the remote Linux or Windows browser.
Local clipboard access remains explicit and subject to browser permissions. Copy writes directly,
using Quasar's `execCommand('copy')` fallback when the Clipboard API is absent, including HTTP.
Copy failures show a toast, never a copy dialog. Paste retains its manual text fallback.
No clipboard toolbar buttons or right-click
menu are added. Text is not persisted or logged and is limited to 16 KiB.

Manual upload selects an ordinary **Ablage** or **Computer-Dateien** file after clicking the
website's file input. Playwright intercepts the chooser; a staged file can be retried without
transferring it again. Completed downloads stream into the existing backend session files
with a generated filename, under the owner's upload limit and storage quota. Their SHA-256
is verified before storage; worker staging is deleted only after successful storage and
the saved result is remembered before cancellation can propagate. Failed transfers retain
staging for an explicit retry, not repeated automatic large transfers. Download events retain controller-origin attribution.
Unlocking input does not clear model attribution: subsequent downloads in that runtime conservatively
retain the latest model origin and its browser-use checks, including downloads clicked manually.
Saving an authorized browser result needs no arbitrary computer-file write permission.
Published downloads survive browser close; temporary transfers do not. Existing computer
downloads are neither moved nor deleted. No workspace synchronization is performed.

Limits: 32 tabs, 1 GiB per transferred file, 32 staging entries, 2 GiB aggregate staging,
64 KiB transfer chunks, bounded diagnostics and code. Screenshots for the model remain scaled,
ephemeral observations with a separate 64 MiB ceiling, separate from viewer frames;
neither is a persistent session image. Uploads use private local file paths rather than
whole-file Python buffers and preserve the website filename.

## API and Troubleshooting

`/api/browsers/{integrationId}` carries authenticated JSON control; `/view` carries the private
binary page stream. The Agent connects outbound to `/api/browser-relay/{relayId}` using the
existing one-use, connection-bound token. Login ownership, allowed origins, live access checks,
queue limits and transport cleanup remain enforced. No arbitrary CDP method or upstream URL
is exposed. See [the Agent runtime contract](../apps/matoloop-agent/BROWSER.md).

The WebUI retries interrupted viewers after 1, 2 and 3 seconds while the Browser app is visible
and eligible. A failed page stream replaces only that stream. Control reconnects send
`expectedBrowserId` and may subscribe only to that live runtime; they never create a replacement.
Each new control subscription receives a fresh login-bound view ID. Inputs and mutations are not
replayed. The retry count resets only after the first matching page image is drawn, not on metadata
alone. Hidden views pause retries. Exhausted retries or terminal access/runtime errors require
**Browser erneut verbinden**; closing a browser in another view cannot automatically reopen it.

API errors use the central error catalog and German frontend formatter. No raw Chromium,
Playwright or installer exception is exposed. Technical latency logs and durable browser
open/close diagnostics remain content-free. For an unknown operation outcome,
inspect state instead of blindly repeating a mutation.

Clipboard copy uses `POST /clipboard/copy`; text insertion uses `PUT /clipboard`.
Manual uploads use `/upload-source`: local files first enter the ordinary `/api/workspace/files`
flow, then the browser picker selects the session or computer file. Session-file uploads share
one bounded staging implementation with model uploads; their authorization remains separate.

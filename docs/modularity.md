# Module ownership and dependencies

MatoLoop is a modular monolith. The backend, frontend, and standalone MatoLoop
Agent keep their existing deployment and protocol boundaries. Internal modules
are organized by responsibility; small cohesive domains may remain single files.

## Backend

| Module | Responsibility |
| --- | --- |
| `api/` | HTTP schemas, authentication dependencies, status mapping, SSE and WebSockets. Shared HTTP access helpers live outside `api/routes/`. |
| `sessions/` | Owned sessions, persisted messages, history and submission workflows. |
| `agent/` | Run lifecycle and the provider-neutral execution loop. `runtime.py` owns scheduled work and cancellation; `loop.py` owns a turn; prompts, projections, summarization and titles have separate modules. |
| `tools/` | Model-facing arguments, contracts, execution context, registry and executor. Each capability module contains its handlers and tool definitions. |
| `permissions/` | Policy evaluation, scoped grants, approval coordination, decisions and the existing approval-request workflow. |
| `files/` | Managed session storage, revisions, quotas, archives, search and file-access coordination. Computer operations continue to use `execution/`. |
| `media/` | Attachments, input validation, document extraction and video processing. |
| `web/` | Bounded HTTP retrieval, downloads, URL authorization and source identities. |
| `integrations/` | Product adapters and generic connectors each live in their own package, even when the package contains only `adapter.py`. Product clients and services are colocated; shared contracts, registry, execution, settings helpers, and cross-integration coordination remain at the root. |
| `providers/` | LLM and media transports plus their application services. |
| `execution/` | Owner-bound connections, protocol validation and transfers to MatoLoop Agents. |
| `projects/` | Project coding workflows and durable execution queue. |
| `diagnostics/` | Durable diagnostic recording and the read-only diagnostic MCP. |

Within `agent/`, `loop.py` coordinates model steps and retry decisions. Each
step gets revalidated memory context from `memory_context.py`, effective tools
from `turn_tools.py`, and a projection from `turn_context.py`. `model_turn.py`
handles provider streaming and assistant persistence; `tool_round.py` executes
and persists each tool call. Run-bound skill drafts, media model calls, and
OpenSCAD preview verification live in their corresponding agent modules.
`runtime.py` continues to own cancellation, continuation, and run lifetime.

Memory, settings, authentication, messaging, automations and notification modules
retain their existing ownership. SQLAlchemy models remain grouped by aggregate
under `models/`, with one metadata graph and the existing migrations.

### Tool assembly

`tools/catalog.py` explicitly assembles the existing tool definitions in their
canonical order and supplies policy callbacks. `tools/registry.py` selects and
describes tools; `tools/executor.py` validates and executes a bound call. Tool
contracts and arguments can be imported without initializing the runtime or the
complete integration catalog. Package initializers do not eagerly assemble the
application.

Common authorization helpers are public functions in `tools/access.py`,
`files/access.py` and `web/access.py`. Product adapters use these contracts rather
than importing private handlers. Client modules do not depend on model tools or
HTTP routes.

For example, `tools/web.py` exposes `webfetch` and `download_file` to the model.
Both reuse `web/fetch.py`. Downloads create managed files through `files/service.py`;
BookStack can reuse the same downloader without invoking an agent tool. Session
files and computer workspaces remain distinct storage and authorization domains.

### Import and workflow rules

- Application modules do not import API routes. Routes also do not import other
  routes; shared HTTP helpers belong directly under `api/`.
- Integration adapters import public tool contracts and access functions, not
  concrete general-purpose tool handlers.
- Product clients do not import the agent loop, tool handlers or FastAPI.
- Keep the eager module-import graph acyclic. Deferred execution-time imports at
  an explicit composition boundary must not trigger work during module loading.
- Keep transaction, cancellation, approval revalidation and filesystem
  compensation boundaries intact when extracting an application workflow.
- Model contracts are separate from Python module paths. Moving code does not
  change tool names, schema order, prompts, permission semantics or persisted data.

`tests/test_module_boundaries.py` checks these import boundaries and isolated leaf
imports. `tests/test_refactoring_contract.py` freezes representative public/private
tool schemas and prompts with and without Webfetch. Existing behavior tests remain
the authority for runtime effects, error contracts, storage and protocols.

Each registered adapter has a package named for its product (`mail` for IMAP/SMTP,
`mcp` for the generic connector, `searxng` for search). The registry imports
its `adapter.py` explicitly; adding an adapter means adding its package, not a
root-level adapter file. `client.py`, `service.py`, and other modules belong in
that same package when needed. Transport reuse across products is explicit:
SonoBin uses the bounded MCP client and service without presenting itself as
an MCP connector. The root `search.py` coordinates no-key search, SearXNG and
provider selection; SearXNG's configuration and HTTP client stay in `searxng/`.
The unregistered YouTube transcript helper has its own package too. API routes,
frontend components, tool names, permissions, and persisted adapter IDs do not
move with Python modules.

## Frontend

### Menus and selections

Use `shared/ui/AppMenu.vue` for application action and context menus and
`shared/ui/AppSelect.vue` for selection fields. Both reuse Quasar and the surface,
selection, spacing, and responsive rules in `shared/styles/_menus.scss`. Feature
components control their content, placement, and width; the composer anchors its
menus above the field. Mobile selections use Quasar dialogs by default.

Both components forward Quasar attributes, events, and slots. `AppSelect` supplies
the default option row: `label`, optional `primaryLabel`, `icon` or `trustIcon`,
`caption`, `description`, and a selected checkmark. Keep `label` as the complete
selected-field and accessible label. Custom `option`, `selected`, and `no-option`
slots remain available for specialized content. Multi-selection, chips, filtering,
disabled options, and form validation remain Quasar behavior. Informational
popovers and tooltips use their own Quasar components rather than action menus.

`features/<domain>/` owns each domain's components, pages, Pinia store
implementation, and domain-specific helpers. Chat owns its draft, session,
outbox, and stream actions; files owns the explorer, viewer, editor, and file
helpers. Other domains include browser, terminal, mail, automations, providers,
integrations, notifications, memory, skills, and coding. Settings keeps the
shared settings navigation and scope control; domain settings pages live with
their domains.

`app/` owns application-level layout, navigation composition, global shell
synchronization, the not-found page, PWA update handling, and Control Center
composition. Navigation delegates session and project workflows to their
respective feature modules; dialogs remain mounted alongside the drawer.
`shared/ui/` contains components used across
domains. Common utilities such as API error formatting, date formatting, and
input encoding remain under `utils/` until they have a single domain owner.

Existing `stores/*` and moved `utils/*` imports remain stable re-export facades.
The facades contain no state or duplicate implementation. Cross-feature
coordination uses the existing Pinia actions and the explicit Control Center
manifest. Store IDs, browser-storage keys, route URLs, component templates,
and styles retain their behavior through this extraction.

## Standalone Agent

The standalone `matoloop_agent` package remains independent of backend Python
implementation modules. Backend/Agent communication is an explicit wire contract;
module refactoring is not a protocol-version change. Cross-application integration
tests may import the backend in a combined test environment.

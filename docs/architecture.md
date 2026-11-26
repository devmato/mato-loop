# Architecture

MatoLoop is a self-hosted, provider-neutral chat and agent harness. It owns persistent conversations, execution, permissions, files, and memory. Model providers transport requests and responses; they never execute tools.

This page explains system boundaries and deliberate tradeoffs. Use the [documentation index](index.md) for detailed contracts and the German [handbook](user/index.md) for usage and operations. Implementation changes belong in the relevant topic, not in an expanding feature history here.

## Layers

MatoLoop is a modular monolith with a separately installed execution Agent, not a collection of internal microservices.

| Layer | Responsibility |
| --- | --- |
| FastAPI API | Authentication, HTTP schemas, dependencies, controlled errors, REST and SSE |
| Application services | Workflows, transactions, filesystem compensation |
| Agent runtime | Provider-neutral model/tool loop, context, authorization and human interaction |
| Providers | Replaceable LLM and search transports |
| Integrations | Host-installed product adapters and bounded generic connectors |
| MatoLoop Agent | Authorized filesystem, process, transfer and supported local operations |
| Persistence | PostgreSQL, SQLAlchemy, Alembic and backend-managed files |
| Frontend | Vue/Quasar presentation and interaction; Pinia server state |

The production container serves API, SPA and handbook. Development uses separate frontend and backend processes. Infinity supplies local embeddings; it is infrastructure, not a selectable LLM provider. Module ownership and import boundaries are defined in [modularity.md](modularity.md).

## Authentication and Ownership Boundary

`UserAccount` is the principal. Local cookie authentication is the implemented login method. The first local account becomes administrator; the last active administrator is protected.

Personal sessions, messages, files, memories, credentials, approvals, settings and computers are owner-bound. Services and queries enforce ownership before retrieval or trust filtering. Administrator status grants account and instance-configuration management, not impersonation or access to another user's personal content. Shared instance providers, credentials and integrations are deliberately available under instance policy.

The debug MCP is a revocable, read-only administrator diagnostic path. Owner-bound tools filter IDs by the token owner. Its backend-log tool additionally exposes instance-wide application logs, potentially including entries concerning other users. Diagnostic recording is bounded and excludes credentials and unfiltered provider responses. Logs are untrusted data; loss of administrator status or token revocation removes access. See [debug access](user/debug-zugang.md).

## Projects and Coding Boundary

Projects group owned sessions without merging their managed files. Project defaults initialize new sessions; changing a project does not rewrite existing sessions or grant the project filesystem access.

A project may bind one personal OpenCode project/workspace. OpenCode performs read-only analysis; the MatoLoop agent writes the canonical plan. Execution snapshots the exact plan revision and selected models/variants into a persistent FIFO queue with at most one active execution per project. External remote activity pauses the queue. Unknown post-dispatch outcomes are not replayed automatically. The binding is fixed after the first saved plan. See [OpenCode integration](integrations.md#opencode).

Git runs on the selected computer. Managed worktrees are ordinary directories, may be shared by sessions, and are not OS or service isolation. Their original repository must remain accessible. Preparation and removal have explicit authorization and data-loss boundaries; see [commands and worktrees](commands.md).

## Chat Submission Boundary

Unsent drafts are browser-local. Sending a first message creates the session, internal storage, message and run. A session-bound Control Center app may create an empty session first; opening Home, fixed settings or global notifications does not.

The owner-scoped browser outbox uses stable request IDs and FIFO follow-up delivery. A retry after an unknown HTTP outcome returns canonical state rather than starting duplicate work. Accepted direct messages join the active run, supersede pending interactions and replan after active tool work finishes. Completed effects remain. Direct messages apply their selected profile at the continuation boundary; queued messages retain their submitted profile for admission. Other run settings remain frozen, with live availability and permission boundaries. This is not resumable backend execution. See [run continuations](run-continuations.md).

`/fork` creates an independent session and completed managed-file history, not a second workspace. During an active run, the copied conversation ends before that run's input message; the original run continues. It does not copy pending file writes, the message queue, approvals, grants, automations or children. Reusing the computer directory means local files remain shared. Undo and retry remove their defined history suffix, never reverse external effects.

## PWA and Push Boundary

The service worker caches the application shell, not authenticated APIs, files or chats. A user-confirmed update reloads app windows and can lose unsaved memory-only state. API and documentation requests stay outside the shell cache.

Web Push uses encrypted owner/device subscriptions and allowlisted browser push services. After canonical state commits, the notification service dispatches requests without additional delay and batches ordinary notices per owner. Focused-client presence and delivery batches are process-local. Delivery is best effort, never a transaction prerequisite. See [Push setup](user/betrieb-und-datensicherung.md#app-und-push-benachrichtigungen).

## Configuration Boundaries

Environment variables supply bootstrap/infrastructure configuration. Typed Pydantic documents in PostgreSQL own runtime settings with explicit instance or user scope. Partial writes preserve omitted values; additive fields have defaults. Application services own commits.

Secrets reside in encrypted `credentials`, not settings JSON, API errors or prompts. The Fernet key is stored under `/data/secrets/credential.key` and must be backed up with the database. Personal credentials remain owner-only; instance credentials are intentionally shared. Protected input returns opaque references to the model. Resolving a secret supplies authentication, not action permission; redaction is defense in depth against textual disclosure, not containment of commands that transform or persist secrets. See [protected tool text](protected-tool-text.md) and [questions](questions.md).

Personal-provider, profile and tool-customization gates may ignore saved values without deleting them. A gate exception for administrators does not override technically disabled adapters. Integration availability, action policy and tool loading are separate settings. Models belong to [model defaults and session templates](session-templates.md), not agent workflows.

## Memory Boundary

Current-session history remains canonical. Optional memory context adds complete visible pins, a bounded project-session index and relevant snippets. Owner, audience, session mode, trust and project filters apply before ranking and limits. Project sessions do not automatically fall back to unrelated projects. Retrieval data is not an instruction or proof of current state.

The maintained user profile is separate from searchable memory. Dreaming creates or reconciles durable knowledge from eligible evidence; age or non-mention never authorizes forgetting. Its persistent revision-checked approvals survive backend restart, unlike live agent-action approvals. Infinity failure degrades to PostgreSQL lexical search. [memory.md](memory.md) owns retrieval, budgets, audiences, Dreaming and API contracts.

## Provider Boundary

Adapters normalize messages, schemas, text deltas and tool calls. The host validates returned calls, even where provider schema support is incomplete. Discovery preserves local model choices; failed synchronization does not replace the last valid catalog. Provider failures map to controlled domain errors, not upstream response bodies. See the [backend provider overview](../apps/backend/README.md) and [model context settings](harness.md#model-context-settings).

Selecting a model releases already-authorized run content to that provider; there is no second blanket session-release approval. Effective model trust selects policy branches and retrieval audiences. Session confidentiality classifies a history source and is not changed by subsequent model selection. Public models automatically retrieve only public prior sessions; explicit private-history access uses the separate sensitive-read action. Disabling model distinction makes runtime consumers use private classifications without rewriting stored choices.

Pseudonymization is independently configurable. It protects recognized identifiers and structured secrets, not arbitrary confidential meaning or binary media. Main requests derive stable owner/session-specific marker identities from the instance credential key; forks, children and other owners do not share restoration authority. Restoration remains request-local. A bounded process-local protection cache may retain original text until eviction or restart; it is neither persisted nor logged. Rotating the credential key changes marker identities.

Reasoning replay is private transport state bound to provider, model, credential, endpoint, trust and privacy settings. It is not reconstructed from answer text and never becomes Memory or session-read evidence. Active pseudonymization drops replay state. Direct input and credential lifetime are defined in [run-continuations.md](run-continuations.md).

## Integration Boundary

An integration is a configured instance of a host-installed adapter. Product identity is independent of transport: a supported product has its own adapter even when it uses MCP internally. Generic MCP remains a separate connector. External services cannot supply handlers, capabilities, frontend components or authorization behavior.

Backend adapters execute in MatoLoop. Agent adapters bind one owned computer and require its advertised static operation. There is no automatic execution-location fallback. Request-bound tool snapshots, connection revisions, ownership, resource policies and post-approval checks prevent stale bindings from authorizing effects. Remote ACLs remain hard limits. [integrations.md](integrations.md) owns contribution, loading, binding and product contracts.

## Browser Boundary

Browser workers isolate disposable website state by chat/computer, not arbitrary code at OS level. Human and model share the same chat browser; model actions briefly fence manual input. Closing a viewer does not close the browser. Explicit close or runtime loss discards website logins.

Ordinary browser use includes authenticated website interaction and JavaScript. Python requires an additional permission and has worker-account OS authority. Clicks cannot be reliably classified by business effect; an allowed action may purchase or delete. Use OS isolation or disable code when that authority is unacceptable. See [browser.md](browser.md).

## Channel Boundary

Channels are owner-bound message endpoints, not integrations that contribute tools. Authenticated, deduplicated inbound events enter ordinary sessions and runs. Durable outbox delivery remains separate from process-local typing/activity previews. Disabling a channel stops pending external deliveries rather than replaying them after re-enabling.

Teams uses a separately operated public transport relay and backend-initiated HTTPS long polling; the backend needs no public ingress. The relay cannot run models or tools, but is trusted with channel content and sender assertions. See the [relay contract](../apps/matoloop-teams-relay/README.md) for deployment, bounded spool retention, receiver fencing, and uncertain delivery.

Channel approvals/questions revalidate owner, destination, source message, active run and pending request. Skill publication still needs exact package review in MatoLoop; channels can only deny it. Voice input uses the same attachment/transcription pipeline as web input. See [channel usage](user/kanaele.md) and [command entry points](commands.md#shared-command-entry-points).

## Agent and Tools

One provider-neutral runtime assembles a host registry, projects context, requests model output and authorizes concrete tool operations. Model arguments, descriptions and retrieved text cannot grant authority. Host-controlled prompts and schemas use concise English; dynamic content may use any language.

File mutation schemas keep revision bookkeeping out of model arguments. Execution-time conflict handling and internal repair presentation are defined in the [tool contract](harness.md#tool-contract); neither replaces authorization or hides uncertain effects.

Profiles define workflow, not an alternative permission engine. Plan analyzes without implementing; Build requires an actual implementation request. Tool approval or switching profiles alone is not such a request. Skills supply scoped instructions/resources, never new credentials, tools or permissions. See [harness.md](harness.md), [tool-permissions.md](tool-permissions.md) and [skills.md](skills.md).

## MatoLoop Agent Boundary

The standalone Agent connects outbound using strict protocol v1 and belongs to one owner. It receives authorized bounded operations, not provider credentials, the system prompt or chat history. Pairing uses a short-lived token and locally generated durable key; revocation removes authentication before closing connections. Operation advertisement determines availability, not permission. Version equality is not a connection requirement; missing required protocol features block affected operations until update.

Filesystem RPCs use normalized absolute paths and no-follow/no-reparse traversal. These checks prevent link redirection, not general OS access. Native Agents use their account's rights; the prepared container currently runs as root. Direct owner-authenticated browser filesystem/terminal actions intentionally bypass model policies while retaining ownership and technical validation.

**Accepted shell tradeoff:** `shell.read` means no recognized mutation, not side-effect-free execution. Unknown programs, tests and builds may still mutate. Complete effect analysis and an OS sandbox are not prerequisites for the coding agent. Review concrete regressions in classification, approvals, revision checks or enforced boundaries; revisit the tradeoff with new evidence or a changed threat model. Deployments may choose stronger isolation.

Shell execution is workspace-independent but retains computer-zone, sensitive-data, category and revision checks. A command's working directory is descriptor-bound; executable validation followed by path-based exec is not atomic executable-identity protection. Cancellation cannot roll back effects and uncertain outcomes are not replayed. [The harness reference](harness.md#computer-execution) owns execution and consent details; [computer-maintenance.md](computer-maintenance.md) owns installation/update lifecycle.

## Working Directories and Approvals

There are three independent locations: persistent backend-managed session files, the Agent's session-temporary subtree, and an explicitly selected ordinary workspace. The backend never synchronizes or automatically deletes workspaces. Managed storage uses session identity even inside a project.

Permissions bind actual operations to typed owner/resource scopes. Run grants end with their run; session grants survive until revocation. Write includes ordinary read on the same scope, not delete or sensitive read. Navigation, file access and shell execution are separate rights. Hard denials never become overridable approvals. [Typed resource matchers](harness.md#typed-resource-matchers) define scopes; [managed files](harness.md#managed-files) defines revisions, quotas and irreversible deletion.

## Context and Compaction

Provider input is a transient, budgeted projection. Compaction summarizes a completed prefix without editing canonical messages or re-executing tools. Tool calls and results stay paired; omitted evidence retains authorized retrieval references. Pins are never silently dropped to fit. Failed summary generation uses an explicitly marked fallback, not invented evidence. [Harness context contracts](harness.md#history-and-request-context) own fitting, compaction and output allocation.

[Prompt construction and cache stability](harness.md#prompt-construction-and-cache-stability) defines assembly order, turn-local replay, deterministic serialization and adapter-local routing. Cache reuse never overrides current authorization, privacy or required context changes.

## Error Contract

API, tool and persistent runtime failures use `error.code`, a centrally controlled English `error.message`, optional `error.params` and `error.field`. Never expose raw Pydantic, provider or exception text. HTTP status supplies a safe fallback for unknown codes.

The frontend translates centrally in `apps/frontend/src/utils/api-errors.js`. Axios failures produce one central negative toast; persistent agent errors are ordinary canonical chat responses, not duplicate toasts. SSE accelerates that state. [AGENTS.md](../AGENTS.md#error-contract) defines the required response shape and implementation rules.

## Notifications

Notifications are owner-scoped pointers/previews of canonical messages or requests, created with their source transaction. Reading or deleting one does not resolve an approval, change a session or undo work. Response and live-request notices disappear after acknowledgement; durable agent/Memory notices may retain read history. Push is only an optional delivery accelerator.

Focused-session presence and read watermarks govern acknowledgement. Run-local explicit notify/silence markers control completion notices without suppressing errors or interaction requests. Silenced responses also do not contribute to session/project unread indicators or unread-response priority; earlier unread responses remain unread. [The handbook](user/anmeldung-und-oberflaeche.md#benachrichtigungen) describes the user-facing lifecycle.

## Automations

An automation is an owned saved task attached to an ordinary visible session. Schedules/event triggers enqueue the normal AgentRuntime; execution uses that session's current configuration and permissions. Creation does not preauthorize later effects. Simultaneous schedules are bundled; overlapping work is skipped, downtime occurrences are not replayed, and completed mutations are not retried.

Event triggers deduplicate by source run and exclude automation-originated work, including retries and children, to avoid loops. Archiving blocks new starts and cancels queued automation work without deleting history. Removing an automation retains its session, files and running agent. [Automation instructions](user/automationen.md) describe configuration and safe advance permissions.

## Frontend Interaction

The frontend uses compiled routes/apps only; stored IDs never load executable content. Rendered Markdown, math, SVG and diagrams are bounded and sanitized, with no raw HTML or content-controlled executable configuration. External answer images require explicit origin consent; file previews do not load external images.

Server state is authoritative. Owner-scoped SSE carries invalidation hints after commit; REST reconciliation recovers reconnects and slow subscribers. Streams are process-local acceleration, not a durable event bus. Editor drafts are memory-only; switching tabs preserves them, reloading may lose them. SHA-256 conflict checks prevent silent overwrite. Destructive actions require confirmation. Desktop/mobile and light/dark remain supported.

## Runtime Data

Canonical relational data and managed files must be backed up together with encryption keys and configuration. Deleting an account removes owned database data and backend session trees, but not external service data or Agent files/state. Shared instance resources remain.

Use the development Compose stack through `scripts/dev.sh`; never use production templates for development. Development `reset-storage` deletes named data volumes, including the Agent workspace, and is not routine cleanup. [Development](development.md) and [backup/recovery](user/betrieb-und-datensicherung.md) own exact paths and procedures.

## CI and Release Boundary

CI checks and publishes, never deploys or mutates installation storage. Builds use immutable UTC-date/SHA versions; managed Agents target the running backend's exact full commit, frozen per operation. GitHub is primary and Forgejo a pull mirror. See [ci-and-releases.md](ci-and-releases.md) for publication, releases and rollback.

## User Documentation

MkDocs publishes only the German handbook at `/docs/`; English technical documents remain repository references. The site is public and contains no personal data. OpenAPI remains at `/api/docs`. Build and review procedures live in [development.md](development.md#documentation).

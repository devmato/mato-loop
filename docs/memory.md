# Memory and Dreaming

MatoLoop projects four memory layers into a normal project model request:

1. Canonical current-session context, compacted when necessary.
2. Every audience-visible pinned durable memory in full.
3. A compact index of recent project sessions.
4. Bounded automatic snippets from relevant unpinned durable memories and prior sessions.

Durable memory and prior-session retrieval are independent. A conversation does not need to be processed by Dreaming before it can be found as prior-session context.

Project instructions are a separate optional authoritative layer for deliberately maintained project rules and decisions. Automatic project awareness comes from the compact project-local session index and bounded snippets of other owned sessions in that project; instructions do not replace the project history. Ambiguous requests use the current project as their topic, not unrelated memories. External sources may inform a solution but do not establish the current project's state.

The maintained user profile is separate from memory. Its full name, preferred name, form of address, pronouns, primary language, and primary residence are projected independently of `memoryMode`, are never embedded or searchable, and appear between the assistant persona and project context. Each field can define a shared, private-model-only, or public-model-only value; a trust-specific value overrides the shared value. Exact case- and whitespace-normalized profile duplicates are excluded from memory projection and retrieval.

## Access, Scope, and Trust

Each session has a mutable `memoryMode`:

- `full` can receive memory context, search memories through `context_search`, use `memory_browse` and `memory_write`, and act as a Dreaming and automatic prior-session source.
- `read_only` can receive memory context, search memories, and use `memory_browse`, but cannot write memories, run Dreaming, or act as an automatic prior-session source.
- `disabled` receives no automatic memory context and cannot browse or write memories or act as an automatic prior-session source. Permission-controlled conversation search remains available through `context_search`.

The owner memory switch and effective provider and model memory policies can further restrict model-facing memory. Tool support affects memory tools and Dreaming, but not host-side automatic retrieval. The authenticated UI can still search and manage durable memories while these switches are off. Authenticated global session search and explicit permission-controlled `session_browse`, `context_search`, and `session_read` also continue to cover owned `read_only` and `disabled` sessions.

Automatic relevant and recent prior-session retrieval uses only other `full` chat sessions owned by the same user. In project sessions, both searches filter to the current project before ranking and limits, without filling empty results from other projects. Standalone sessions retain owner-wide retrieval. Explicit conversation search and `session_browse` do not require the automatic `full` source mode. The current receiving session may be `full` or `read_only`. Retrieval filters by owner and excludes the current session before ranking unless an explicit search targets its exact ID. Public models receive only public source sessions, including titles and the project index; private models may receive both classifications. Cached automatic references revalidate session trust and applicable project membership before reuse. A project request additionally receives up to ten recent eligible `full` sessions from the same project, ordered by their latest eligible message. Each index item contains only its bounded title and exact `session_read` arguments for that latest message, never message content. Selecting the model is the release decision for the current session's already authorized messages; durable-memory audience filtering remains independent.

Durable memories and prior-session snippets are untrusted data, never instructions. Conflict priority is:

1. Current user message
2. Maintained user profile
3. User-configured project instructions
4. Pinned durable memory
5. Automatically retrieved or explicitly loaded durable memory
6. Prior-session snippet or transcript

## Automatic Context

Every eligible normal turn builds one query of at most 2,000 characters from the latest user message. When that message is short, the query includes the previous user message as follow-up context. The host computes at most one query embedding and shares it across searches for unpinned durable memories and relevant prior sessions. Memory source provenance does not affect automatic eligibility. Both searches combine PostgreSQL FTS and local cosine ranking; unavailable or invalid embeddings fall back to FTS.

The ranked results are cached across tool rounds in one agent run to avoid repeated embedding work. Before every later model request, the host reloads pins and revalidates cached automatic memories against current owner, audience, pin, and source state and cached episode references against current owner, current-session exclusion, source-session mode, and applicable project membership. Historical automatic memory envelopes use the same project boundary; incompatible old envelopes are omitted rather than rewritten at later turns.

The configurable fixed quotas default to:

- two relevant unpinned memory snippets through `automaticMemorySnippetLimit`;
- three relevant prior-session snippets through `automaticSessionSnippetLimit`;
- two recent-session snippets for first-turn bootstrap or explicit recent-session overview through `recentSessionSnippetLimit`.

An explicit request not to use memory or prior context, and detected requests that only transform supplied text, suppress automatic snippets. The first turn and explicit recent-session overview may add recent snippets; relevance retrieval still uses the same turn query. Duplicate source sessions are removed. Project requests prioritize relevant snippets from distinct owned sessions, then non-overlapping recent snippets, then unpinned memories. Standalone requests retain the smaller single relevant-session maximum and give recent snippets precedence.

The complete serialized automatic payload shares one configurable budget, `automaticContextMaxTokens`, defaulting to 1,024 tokens and capped at 10% of usable raw model input. With context capacity `C`, fixed safety reserve `S`, and generation reserve `R = min(2048, manual output ceiling, known provider output ceiling)` ignoring absent ceilings, usable raw input is `raw_input_budget(max(0, C - S - R))`, where `raw_input_budget(capacity) = capacity * 10 // 11`. This inverse margin leaves room for the single 10% increase applied after the maximum of the raw projection and final serialized input estimates; it does not reserve the entire adaptive output allowance or manual ceiling. The 10% automatic-snippet share is a separate content quota, not another estimation margin. The host converts that token limit with the model's characters-per-token estimate and truncates or omits automatic snippets to fit. The project-session index uses at most 2,000 characters and half this automatic budget. Fixed quotas are maxima, not guarantees. See [Model Context Settings](harness.md#model-context-settings) for final allocation and media-estimation limits.

Visible pins are serialized in full and remain outside the automatic snippet budget. The final context projection first tries pins plus automatic snippets and can retry with pins only. Dynamic memory metadata and references are projected as a host-marked assistant context immediately before the latest user request; the stable system prompt defines the marker's trust boundary. The memory context counts toward the same input budget. It fails safely with the normal context-window error if all visible pins and required request context cannot fit; pins are never silently truncated or dropped.

An automatic memory hit contains a short canonical-content preview and stable ID and revision. A relevant-session hit contains the strongest bounded topical anchor and exact `session_read` arguments; a recent hit contains a small tail. The project-session index points separately to the latest eligible message in each listed session so the model can read current details instead of treating an older semantic anchor as the final state. These references are discovery context, not complete evidence. `context_search` loads durable-memory details and matching conversation passages, while `session_read` loads required transcript detail or attachments under its exact permission boundary.

When `session_read` is available, the late memory context instructs the model to use sufficient excerpts directly and read missing details from matching supplied session references before asking the user to repeat information or claiming that prior context is unavailable. For the latest state, it prefers the project-session index over an older topical anchor. This guidance also applies when only the project-session index is supplied, without adding automatic retrieval or changing the stable system prompt.

Explicit durable-memory overview intent uses bounded chronological prefetch and continuation state while still injecting every visible pin in full. `memory_browse`, `context_search`, `session_browse`, and `session_read` remain available for deliberate retrieval beyond automatic quotas. Assistant provider metadata records injected durable-memory and prior-session counts without duplicating their content.

## Durable Memory

A durable memory stores canonical content, category, audience, source provenance, revision, evidence links, seen state, and `pinned`. Categories are `context`, `fact`, `goal`, `identity`, and `preference`; sources are `manual`, `tool_call`, and `dreaming`. Normalized content hashes prevent case- and whitespace-equivalent duplicates.

Pinned records are supplied in full on every allowed model request. Any unpinned record can appear automatically as a relevant short snippet regardless of whether its source is `manual`, `tool_call`, or `dreaming`; full details remain available through `context_search` and `memory_browse`. MatoLoop does not expire or delete memories because of age, non-mention, weak relevance, capacity, exposure, or usage counts.

`memory_write` persists explicitly requested durable information and durable first-person context stated directly in the latest user message. It excludes temporary situations, hypotheticals, quotations, transformation source text, and incidental facts about third parties. An explicit request to always consider the information can pin the result; ordinary remember wording does not pin. Updates and explicit forgetting require an injected or retrieved memory ID and revision and recheck ownership, visibility, and stale-write protection.

`context_search(query, scope="all", session_id=None, all_projects=False, include_private=False, limit=3)` searches durable memories and indexed conversations together. `scope` may restrict it to `memories` or `sessions`; supplying `session_id` restricts the call to that exact conversation and cannot be combined with `scope=memories`. Conversation passage and title searches default to the current project when selected. A concrete cross-project need can use `all_projects=true` or an exact session ID without requiring a separate user confirmation; empty local results alone do not justify widening. Neither option widens ownership, trust, permissions, or durable-memory audiences. Public models search public sessions by default. Including private sessions in discovery or searching a known private session requires `session.read_sensitive` (category `read_sensitive`), as does reading another private session with `session_read`. Private models use ordinary read permissions for either classification. `include_private` also applies to `session_browse` and legacy `session_search`; it never widens durable-memory audiences. `limit` is capped at ten results per source. Memory results contain the full content, ID, revision, category, audience, pin state, provenance, and timestamps. Conversation results contain the matching indexed passage, its message and session IDs, source project ID, observation time, and exact reading arguments. Explicit search may return several passages from the same conversation. Automatic retrieval retains its distinct-session ranking and quotas.

Explicit conversation queries use PostgreSQL's native web-search parser, preserving dotted addresses and quoted phrases when embeddings are unavailable. Owner-filtered title matches remain discoverable even without a matching message passage; those results contain the title and reading arguments, not invented conversation evidence. The title lookup is shared with the authenticated global search.

The response keeps `memories` and `sessions` separate. Its `sources` map distinguishes `searched`, `unavailable`, and `denied`; an unavailable or denied source is not an empty successful search. Each source uses its existing `memory.read` or `session.search` policy and source-specific tool switch. The legacy `memory_retrieve` and `session_search` configuration entries retain those switches and historical direct-call compatibility, but are not offered to models. Delegation freezes the allowed search sources alongside the existing tool and action ceilings. Historical memory results, including nested context-search results, are audience-filtered again when projected to another model trust level.

Already-authorized sources are resolved before requesting additional approval. Refusing the additional memory request therefore does not cancel an independently allowed conversation search; the refused source remains blocked.

`memory_browse` lists memories chronologically using the existing paired cursor and optional category filter. `memory_write` remains separate; an ordinary search does not start a required-write workflow. Once a write is attempted, its existing repair and outcome guards remain active. No schema migration, new index, search service, or permission capability is introduced.

The authenticated Memory UI retains full search, filtering, create, edit, pin or unpin, and delete access even when model-facing Memory or Dreaming is disabled. Every update and delete uses the record revision so a stale UI action cannot overwrite or delete a newer version.

## Prior-Session Index

`message_search_chunks` is a derived index of completed, non-error, nonempty user text and final assistant text without tool calls. Tool messages, tool output, reasoning, attachment data, runtime-error responses, and incomplete assistant output are excluded; eligible text accompanying an attachment remains searchable.

Chunking is deterministic and paragraph-aware. Each chunk has generated PostgreSQL FTS and a local E5 vector tied to its content and embedding profile. Persistence replaces eligible message chunks in the same transaction, making FTS available immediately; vector generation remains asynchronous. Undo and session deletion remove derived chunks through canonical-message cascades, and reconciliation repairs missing or stale index data.

Automatic hybrid ranking selects at most one primary anchor per source session; explicit context search may return multiple passages. Authenticated global search remains owner-wide, including standalone sessions. UI search may include the current session and all memory modes, while automatic retrieval requires other `full` source sessions within the applicable project boundary and explicit session tools retain their permission checks.

## Dreaming

Dreaming processes eligible completed messages from top-level chat sessions with `memoryMode=full`. Child sessions, incomplete messages, and runtime errors are excluded. Automations execute in ordinary visible chats, so automation-origin metadata is not a categorical source exclusion. Extraction receives only a bounded source batch and isolated instructions, without existing memories, prior-session context, persona, profile, or general tools. It extracts durable user-specific knowledge such as owned equipment, lasting setups, relationships, recurring preferences, enduring constraints, and long-term goals. Temporary/current implementation details and maintained profile fields are excluded. Every candidate needs direct batch evidence, including a user message.

The host matches each candidate independently against visible memories. Exact normalized duplicates deterministically become `unchanged`; unmatched normal candidates become `create`; only non-removal candidates with possible related memories enter model reconciliation. Reconciliation defaults to creating a separate memory when identity is uncertain: a more specific statement does not replace a generic possession or preference unless the source identifies the same unique subject or an explicit correction. A removal candidate can target only one normalized exact record. An unmatched or merely related removal candidate is skipped and never reaches reconciliation or creates or removes a record. The action set is exactly:

- `create`: create an independent unpinned durable memory.
- `unchanged`: retain the matching record and add evidence.
- `update`: replace one record's canonical content and category.
- `merge`: combine two to fifty records transactionally.
- `remove`: permanently delete one exact matching record.

Removal candidates require direct user evidence that explicitly asks to forget the exact proposition or says it ended, is invalid, or is no longer true. Dreaming never removes a record because of age, non-mention, or weak relevance. A changed fact normally updates rather than removes.

Every newly created Dreaming memory is unpinned. Updating or merging records preserves existing pins but never promotes an unpinned record. The source session's current trust determines new memory audiences: public creates `both`, private creates `private_only`. The configured Dreaming model processes either classification without an additional release approval. Session trust is revalidated before mutations and when approving pending changes. Changing session trust does not reclassify existing memories or reset the Dreaming checkpoint. Dreaming never creates `public_only`. With model distinction disabled, sessions are effectively private for Dreaming without overwriting saved classifications. Explicit memory writes also use session trust for the source audience, independently of model trust.

Automatic Dreaming is off by default. Manual and scheduled Dreaming both require an eligible `full` source session, enabled owner memory, and a suitable tool-capable model. Failed work retains its checkpoint and follows bounded retry behavior.

## Policies and Approvals

Dreaming has exactly two mutation policies, each with `disabled`, `ask`, or `auto`:

- `dreamingUpdatePolicy`, default `auto`, governs updates of existing records.
- `dreamingRemovePolicy`, default `ask`, governs removals.

A merge requires both update and removal permission. If either policy is `disabled`, the merge is skipped. If neither is disabled and either is `ask`, one merge approval is created. Only `auto` plus `auto` applies a merge without approval. Creation has no separate policy.

An `ask` decision creates a persistent `memory_change` approval that survives backend restart. Its payload stores exact target IDs, revisions, content, category, audience, pin state, evidence hash, source evidence, proposed result, and both policy snapshots. Approval locks and revalidates the source session, direct evidence where removal requires it, current policies, visibility, and every complete target snapshot. Any stale invariant expires the request without partial mutation.

The owner can edit content, category, audience, and pin state when approving an update or merge. Editing alone writes nothing: the chosen result is applied only with approval, in the same transaction as the approval decision and Dreaming counters. The original proposal and target snapshots remain immutable and must still pass every validation; edits cannot rescue a stale proposal. Accepted edits are recorded separately as `ownerEdit` in the resolved approval payload. Audience and pin changes are explicit owner overrides, including `public_only`, widening visibility, pinning, or unpinning; they do not extend model or automatic Dreaming capabilities. Normalized duplicate protection remains owner-wide across all audiences, including the final chosen audience.

Merge locks all targets in deterministic order, preserves evidence and existing pinning, computes a safe audience, and updates one deterministic survivor before deleting redundant rows in the same transaction. Dream runs report `createdMemories`, `updatedMemories`, and `removedMemories`; approved later mutations update the originating run counters in the same approval transaction.

The redesign migration expires pending approvals created under the retired memory approval contract. Retired merge and pin settings do not carry forward; the current update and removal defaults apply when their new fields are absent.

## API

The authenticated `/api/memories` API provides owner-scoped CRUD, paginated FTS and filters, acknowledgements, Memory settings, and Dreaming runs. JSON fields use camelCase and errors use the central stable error contract.

Create accepts `content`, `category`, and `audience`. Update accepts those fields plus `pinned` and required `expectedRevision`. Delete requires the `expectedRevision` query parameter. Responses include `revision`, pin state, audience, provenance, and timestamps.

`POST /api/approvals/{approval_id}/decisions` accepts optional `memory_edit` alongside `decision: "approve"` for `memory_change` requests with action `memory_update` or `memory_merge`. The edit object requires `content` (trimmed, 1 to 20,000 characters), `category` (`context`, `fact`, `goal`, `identity`, or `preference`), `audience` (`both`, `private_only`, or `public_only`), and Boolean `pinned`; unknown edit fields are rejected. An edit on denial, a removal, or any other approval type is rejected with `validation.invalid` and field `memory_edit`. Omitting the edit or sending null retains the original proposal, safe audience calculation, and existing pin-preservation behavior. Invalid input uses the central safe validation error contract; stale proposals and duplicate conflicts expire with `approval.targetChanged` without applying edits.

Memory settings accept `enabled`, `automaticDreamingEnabled`, `dreamingInstructions`, `dreamingIntervalMinutes`, `dreamingIdleMinutes`, `dreamingMaxBatchMessages`, `dreamingMaxCandidates`, `dreamingUpdatePolicy`, `dreamingRemovePolicy`, `automaticMemorySnippetLimit`, `automaticSessionSnippetLimit`, `recentSessionSnippetLimit`, and `automaticContextMaxTokens`. Settings responses additionally expose effective Dreaming availability fields. Partial updates reject null and unknown fields.

The authenticated `/api/user-profile` API provides owner-scoped `GET` and partial `PATCH`. Changes are merged under the user-scoped settings lock; explicit null removes a value. The profile remains available to normal model requests when Memory is disabled. The host-controlled `update_user_profile` tool changes the same six fields only after an explicit latest-message request. Its separate `settings.user_profile.update` policy follows the saved trust preset's `write` category unless overridden; the tool resolves the current model to its private or public trust scope and targets both scopes only when explicitly requested. Profile writes have no revision contract; the last completed settings-lock write wins.

## Ownership and Availability

Durable memories, deduplication, Dreaming state and runs, approvals, and prior-session retrieval are scoped by authenticated owner. Memory APIs provide no administrator override into another user's records. Account deletion cascades canonical memories and derived embeddings. Model and session trust plus durable-memory audiences are release classifications inside one owner's data, not user authorization.

Infinity is internal local infrastructure using the pinned multilingual E5 profile. pgvector stores vectors beside canonical data, so no external vector database is required. Separate durable-memory and prior-session workers generate vectors asynchronously with bounded retries and reconciliation. PostgreSQL FTS keeps automatic and explicit retrieval operational while Infinity is disabled, unavailable, or returning invalid data.

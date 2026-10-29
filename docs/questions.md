# Agent questions

The built-in `question` tool lets the model pause an agent run for structured user input.
One tool call contains one to five questions of type `single_choice`, `multiple_choice`, or
`free_text`. Choice questions allow one custom answer by default. Questions may remain unanswered.
The tool must be the only tool call in that model response so later actions cannot run before the
reply is known.

Pending requests and accepted answers are persisted in `question_requests`. The runtime uses the
same single-process waiter semantics as approvals. Question answers, including protected
administrator input, create no permission grants. Password entry and session retention provide
authentication only; actions independently require effective policy or matching explicit consent.
Users can also reject a request, which returns a controlled rejected tool result. The
frontend loads pending requests independently of SSE and renders a sequential question card
inline at its tool call, with unmatched requests below the transcript. The composer remains
available and mounting a card does not move focus away from an existing draft. Backend restarts expire pending requests and fail their active runs
through the normal recovery path.

A direct message supersedes a pending question in the same run, just like an approval. The
request expires without submitting its draft, the waiting call returns `tool.superseded`, and
the run continues with the new instruction. This also applies to protected input: a chat
message never submits a password or confirms storage. Queued follow-ups still wait for the run.
When a parent receives a direct message, delegated runs paused for approval or input are
cancelled through the normal cleanup path. Active child work finishes first; a later input
request also cancels that child while the parent's message is pending. The parent then
continues with the new instruction rather than leaving it blocked behind a child question.

The question dock offers Next for navigation and Done on every step to submit the current draft,
including answers to previously visited questions. Empty answers are allowed, even when all
questions are unanswered. Ordinary questions retain their configured default-answer behavior.
The `answered` status means the request is closed, not that every field has a value. Rejection
discards the draft instead of submitting it.

Protected questions from `secret_input` and `variable_write` also allow empty answer lists.
Elevated file and shell tools reuse the same protected-input flow directly when no password
reference or passwordless sudo is available. The suspended tool resumes after input without a
model turn, then independently authorizes its action. Skipping, rejecting, or expiring input
stops that invocation. `secret_input` remains available for advance or grouped credential input.
Only filled fields receive secret references or can be remembered for the session. A skipped
administrator password supplies no authentication. Temporary protected answers persist only a received
marker for filled fields and an empty list for skipped fields; their values are never persisted.
`secret_input` returns the available references in `environment` and the skipped input names in
`unanswered`, with instructions to explain blocked work instead of assuming values or repeating
skipped questions unless requested. An empty durable-storage answer creates or updates nothing;
`variable_write` reports `saved: false`. Protected values remain hidden from the model, so users
must send a change of direction as a normal chat message rather than entering it in a credential
field.

`secret_input` reuses existing run/session values by name unless a field sets `refresh: true`.
After authentication rejects credentials, the model must refresh the affected fields under their
existing names. Submitting the protected question invalidates the previous run and session
references for refreshed fields, including skipped fields; rejection or expiry leaves them unchanged.
New values use the user's current remember selection. Owners can also delete individual remembered
values under Session permissions; bulk permission revocation does not remove them.

Ordinary question delivery is channel-neutral. A run started from a paired messaging endpoint persists a
versioned question envelope in that endpoint's outbox. Providers may render the envelope with
native controls and direct-reply text input, but all interactions use the same backend state
machine and answer validation. Every interaction revalidates the endpoint owner, source message,
session, run, delivered prompt, and pending request before changing canonical state.

Protected credential questions are handled in MatoLoop rather than delivered as ordinary channel
envelopes. Pending owned requests use `GET /api/questions/pending`; decisions use
`POST /api/questions/{question_id}/answers` or `/reject`. See [protected tool text](protected-tool-text.md)
for the reference/redaction boundary.

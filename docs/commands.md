# Commands and managed Git worktrees

## Shared command system

`sessions/commands.py` owns the host-registered command catalog, parsing and dispatch.
Providers register one `CommandProvider` from `sessions/command_contracts.py` with a
unique source, one or more commands, an availability callback and a preparation
callback. Command names use lowercase letters, digits and hyphens. The parser maps
underscores to hyphens in invocations without changing arguments. Duplicate
sources or names are rejected instead of silently replacing another command.
The Git provider lives in `sessions/git_commands.py`; the generic menu has no Git
dependency. Skill discovery, package installation and executable plugin hooks are
outside this first extension contract.

`POST /commands/catalog` accepts an owned session ID or explicit browser-draft
context. Stored sessions use their server-side workspace; draft discovery does not
create storage or prepare a worktree. Providers return relevant command names and
controlled German reasons for unavailable commands. Responses contain menu metadata,
not execution handlers or model instructions. Preparation rechecks actual state on
submission, and normal agent permissions remain authoritative.

The composer palette opens as a bounded, scrollable overlay above the composer.
Choosing a row by click, Enter or Tab inserts `/name `, retaining existing argument
text and attachments. The palette closes and focus stays in the input. Only another
submission starts the command. Arrow keys navigate enabled entries; Escape dismisses
the palette. Git app action buttons remain direct, explicit agent requests.

## Ownership and configuration

Projects retain their selected owner-bound computer and repository root.
`gitTargetBranch` and `gitWorkspaceMode=local|worktree` supply defaults for new sessions.
The Git app selects or configures the remote; an unambiguous remote or `origin` is
suggested automatically. Repository
discovery runs on that computer, including before a session exists. It is repeated
when opening project settings, changing the computer/path, or explicitly refreshing;
creating a repository later does not require recreating the project. Discovery does
not fetch and never replaces an explicitly saved target with a discovered default.

Session templates contain no Git configuration. Before session creation, the **Arbeitsbereich**
field above the composer offers **Lokal**, **Neuer Worktree**, and registered project worktrees.
New project sessions can override the project default before creation. Worktrees
require the configured project computer and repository. Without a workspace the
field shows **Nicht verfügbar**; a selected offline computer does not change an
existing selection. A worktree is an ordinary session working directory, not a session
lock or ownership relation. Sessions can change computer or directory through normal
settings and tools, including leaving a missing worktree. The Git app offers registered
worktrees as directory choices. Switching preserves the conversation and leaves all files
in their original directory. Template worktree support gates creation and selection,
not later template changes, directory changes or session deletion.
Existing-session management follows the session's computer and Git inventory, even
after project defaults change. **Lokal** is Git's main checkout; new creation still
uses the configured project computer and repository. Initial creation rejects a
different selected directory instead of silently replacing it.
The Git app displays the workspace path with a copy action. Opening it in a browser
draft does not create a session or prepare a worktree.

Choosing worktree mode explicitly authorizes preparation on the selected computer.
Session creation in worktree mode starts at a detached HEAD without creating a branch.
An occupied destination selects another short ID, with at most ten candidates.
Candidates derive from the independent creation UUID so repeated requests authorize
the same paths; the Agent records its choice before preparation. It never adopts or
overwrites an occupied destination. Existing worktrees retain their paths and branches.
Preparation requires a committed local target branch. When a remote is selected and
contains the same-named target branch, preparation fetches it and fast-forwards the
local target before freezing the base. Local commits already containing the remote
tip are retained. Fetch failures, local changes in the target checkout, divergent
history, or a target checked out in another worktree stop creation without resetting
or stashing local work. No remote or an absent remote branch uses the local target.
Already prepared worktrees and frozen preparation bases are not refreshed on retry.
It freezes that base commit and checks it out under
`<agent-data>/worktrees/<project-id8>/<eight-hex-id>`. The project folder uses the first eight hex digits of the project UUID;
collisions or unrelated existing directories select `-2`, `-3`, and so on. A private
`.matoloop-project` marker identifies the full project UUID so later worktrees reuse
the folder, independently of session selection or project names. Repository inspection
proposes the folder without creating it; preparation revalidates it under its lock.
The project prefix only groups newly created directories; it does not
restrict Git discovery or workspace selection to that project. Existing worktrees keep
their recorded paths and branches, including flat short paths and the original
`<project-id>/<session-id>` layout. No migration or renaming is performed. The backend
checks each exact candidate's filesystem policy before preparation. New creation uses
only the short-project/short-worktree layout and requires explicit permitted destinations.
Update the Backend and Agent together: historical creation layouts, remote-only
preparation and receipt-based removal protocols are no longer supported. Existing
worktrees remain selectable and removable through Git inventory without preparation.
Creation and switching use the normal working-directory selection and navigation grant.
Only the concrete worktree is selected and granted, never its project parent or the
Agent data directory. Existing action grants are not copied to the new path. Normal
computer filesystem and shell policies still apply, including external-path denials.
No model, file browser, or terminal can use the session before this preparation succeeds.
The selected project directory must be the repository root and the managed worktree
directory must be outside that repository. The Backend authorizes exact candidates;
the Agent validates the fixed layout and selects an unoccupied destination under its lock.
Hooks and checkout filters are disabled during automatic preparation.
Linked metadata descendants and alternate object stores are rejected rather than
following them beyond the authorized metadata directory.

The session persists its original repository, remote, target and selected worktree path.
Git is the source of truth for inventory and the current branch; no shared target registry
or database migration is needed. Target overrides remain session-scoped and apply only
to the matching computer and repository; entering another repository ignores the old
target. Git commands resolve worktree membership from Git, including when the current
directory is a subdirectory or the conversation was forked. Stored workspace modes
do not block ordinary session operations.
Repeated preparation
recovers only a completed matching worktree and does not reset existing work.
The Agent keeps a private preparation record with the repository metadata identity,
target and base commit. A process-shared lock and the filesystem mutation lock protect preparation.
New worktrees use an independent creation identity in the existing preparation record;
the legacy `sessionId` wire field carries that key. Short-path recovery checks its marker.
Changed inputs, replacement repositories
and incomplete registered checkouts are rejected; they require explicit inspection.
Historical branch-based preparation records remain recoverable without detaching their worktrees.
An existing preparation branch without a completed registered worktree is never adopted after an uncertain checkout failure.
Requests with a run ID derive a stable session ID so retries can recover preparation.
Archiving and session-only deletion retain worktrees. The Git app's compact status rows open
separate workspace, branch and remote dialogs. The workspace dialog removes any registered
non-main worktree without selecting it first, after explicit deletion consent, preserving
existing branches and conversations. The confirmation warns that commits not retained by a branch
may be lost after removal; detached HEAD does not block confirmed removal or require a safety branch.
Pending, untracked and ignored files are lost on confirmed
forced removal. The original checkout, locked worktrees and overlapping
active runs block removal. Before removing a worktree used by sessions, the backend
validates the local project checkout and prepares normal directory grants. Affected
sessions, including those in its subdirectories, then return to that local checkout.
The session deletion dialog offers unchecked worktree removal after live repository discovery,
showing the path, irreversible file loss and other affected sessions. The optional
`worktreeImpact` query parameter binds deletion consent to the previewed computer, current
directory, worktree path, shared Git metadata and affected session IDs. The backend revalidates that target after
stopping the deleted session's runs and calls the same removal service as the Git app.
Removal failure preserves the session. Successful removal cannot be undone if the later
database commit fails. Session-only deletion still needs no reachable Agent or external
working directory; optional worktree discovery has a three-second budget and falls back
to session-only deletion when unavailable or timed out.

Workspace and branch dialogs use searchable compact lists: selecting a row only marks it
and reveals its full path/name in the detail area. Workspace rows show their parent directory
and use a location icon for the active workspace, without an active-color highlight.
Workspaces activate through their per-row menu, including the local checkout; branches
retain the shared switch button.
Session links and dirty status appear only for the marked workspace. Per-row menus retain
independent removal and current-branch rename actions. Occupied branches show their
worktree name and its full path in details, but cannot be switched to. Target-branch editing
is separate from branch switching. Display shortening does not rename branches or paths.

Opening the workspace dialog or explicitly refreshing its settings requests worktree
details. `git.changes` with `statusOnly` reports pending tracked and non-ignored untracked
changes without generating diffs or executing Git filters; submodule contents remain
excluded, as in the changes view. Failed or unsupported checks
remain unknown, never clean. The backend checks only inventory paths allowed by the
computer filesystem policy and derives session links from owner, computer and current
directory containment, including subdirectories. No session-worktree assignment is stored.
Session chips navigate to conversations; they do not select, reserve or own a worktree.

`git.repository` reports registered worktrees and local branches. `git.worktree` and
`git.worktree.remove` remain the creation/removal operations; `git.branch` supplies
local create/switch/rename/delete. Explicit branch creation starts at the current HEAD
without switching branches. The authenticated browser validates project, owner,
computer, paths and Git membership before dispatch. The session workspace endpoint
checks template availability and active runs. Leaving a detached HEAD through the branch dialog
requires confirmation that commits not retained by a branch may be lost; creating a branch first is optional.
Branch switching refuses dirty trees and
does not overwrite ignored files. Removal of a branch requires it to be unused and
contained in the selected target. Deletion checks that target, not the branch's upstream,
and removes only the checked commit so a concurrently advanced branch is preserved.
Management status checks disable
Git filters just like change inspection. Published branches are not renamed when known through
tracking configuration or remote-tracking refs. No fetch is performed to discover them.
No automatic cleanup or synchronization of branch names across sessions is needed.

Repository inspection checks whether the selected local target contains a commit.
The new-worktree option is disabled for a missing local commit before preparation.

Local worktrees need no remote. Review remains available; publication, pull requests
and remote synchronization require explicit remote setup. Adding a remote later does
not reset the worktree or silently change its session target: adopt it in the Git app.

Worktrees share repository metadata and objects. The original repository must remain
accessible at its existing path. Docker deployments need durable mounts for both
locations. Ignored configuration, dependencies, processes, ports and databases are
not copied or isolated. See [parallel development worktrees](development.md#parallel-worktrees).

## Shared command entry points

The composer palette and Git app use the shared catalog. Telegram registers
command names with hyphens mapped to underscores in its native menu and `/help`.
Incoming Telegram names resolve through the shared parser. Additional channel
providers should project this catalog and preserve the normal authenticated session
selection and ingress semantics.

| Command | Task |
| --- | --- |
| `/git-init` | Initialize Git in the selected workspace without committing, adding a remote, or publishing. |
| `/git-review` | Review working changes and unpublished commits without editing or publishing. |
| `/git-commit` | Inspect and explicitly commit selected task changes without fetching or pushing. |
| `/git-finish` | Complete missing review, safe in-scope repairs, affected checks and task commits; integrate locally only when not already on the target branch. Never publish. |
| `/git-merge` | Commit task changes if needed, rebase own unpublished commits onto the local target, review, check, and fast-forward the clean original checkout. No fetch or push. |
| `/git-push` | Publish existing commits to a project-appropriate remote branch without committing, fetching or merging. |
| `/git-pr` | Commit task changes if needed, incorporate the fetched remote target in the source worktree, review, check, normally push to the selected publication branch and create or update the PR. Never update the local target or merge the PR. |
| `/git-sync` | Fetch and fast-forward the clean original project checkout without switching branches or discarding work. |
| `/git-remote [name] [url]` | Configure only the explicitly requested remote through normal agent tools; ask for missing values. |

All Git actions follow current repository instructions and referenced guidelines for branches,
commits, integration, publication and required checks. Conflicting instructions or necessary
unresolved decisions require a question, not an inferred exception.

The Git catalog is fixed and does not probe repositories or disable commands for missing
Git prerequisites. The agent checks those prerequisites when processing the command and
explains blockers or asks for a decision. Finish works in the current checkout when it is
already on the configured target branch: after review, safe in-scope repairs, affected checks
and any needed task commit, it stops without integration, a separate worktree or push.
Otherwise Finish, like Merge and PR, requires a separate worktree; an attached source
branch must differ from the target. Detached HEAD is valid for review, commit, Finish,
Merge and PR. PR preserves the current branch or detached HEAD, rebases own unpublished
commits onto the fetched target only when needed and publishes the reviewed commit with an explicit remote-branch refspec. The selected
remote publication branch is passed explicitly as the PR source; no local branch is created or switched,
so the saved branch binding remains valid on retry. If a suitable worktree
is missing, the agent asks the user to select a
suitable worktree in the Git app rather than creating or switching one implicitly.
Review also works in a local repository; sync needs a remote and target.
MatoLoop owns worktree creation/selection/removal and owner/path/target binding safeguards,
not a second Git workflow. Missing Git prerequisites do not reject command admission;
access, repository identity and changed publication-target checks remain enforced.
An unavailable selected computer can still pause queue admission through the normal error path.

Push inspects the current Git state. If a separate worktree's current HEAD is contained in the
configured local target, the agent publishes that target's current commit, including any other
integrated work, rather than publishing the older worktree HEAD. Otherwise it publishes the
current HEAD using the user-selected branch, an appropriate existing branch/upstream mapping,
or the non-technical current branch name. Detached or technically named work without an explicit
publication target leads to a question, not a mandatory merge or PR. An explicit request to publish
a technical branch name is sufficient.
Push and PR respect repository publication conventions on the configured remote. PR asks if its
publication branch or name is unclear; a different remote branch name does not require renaming
an existing local branch. Publishing worktree commits does not require prior Finish, local
integration or an existing PR. These decisions are agent instructions, not backend push routing.
Push leaves pending working changes
untouched and never commits, fetches, merges, rebases, discards work or force-pushes.

Review checks correctness, security and overengineering and reports only; it never repairs
project files. Temporary check files outside the project remain subject to normal permissions.

For reviews and checks required by the selected action, Git instructions reuse verified work from the conversation when its evidence still covers the
current code, requested scope and relevant execution conditions. A commit alone does not invalidate
a review of unchanged content. Edits and repairs require review and checks of affected areas;
new target commits require checking integration effects, not repeating unchanged task reviews.
Missing, incomplete or stale evidence requires the necessary review and checks. The agent verifies
completed Git effects and performs only remaining authorized work. This is a prompt contract, not
a stored review flag or workflow engine, and never bypasses live permissions, target checks,
unresolved required findings or failed checks. Review criteria occur once per command prompt.

Git command replies start with an action/outcome heading and use short, relevant labeled
lines or bullets. Each command specifies its useful result fields: commit identity,
branches, publication target or PR URL, checks and remaining issues. Empty fields and
raw logs are omitted; partial effects and blockers are explicit. Reviews briefly explain
the change, group continuously numbered findings as required or optional, and include
locations, concrete consequences, minimal corrections, checks, limits and a verdict.
No-findings reviews remain short and do not claim error-free code. These are model
instructions, not a rendered UI template or additional execution authority. Labels and
explanations follow the global response-language contract in [the runtime reference](harness.md#profiles-and-workflow).

Finish makes safe in-scope repairs; local integration resolves clear conflicts and integration
findings in the task worktree and asks about unresolved intent. PR stops on conflicts,
required findings or failed checks without repairing them. It creates or updates the existing
PR after a normal push, without updating the local target or automatically merging the PR.
Later hosted integration should use squash or rebase, not a merge commit.
Finish, Merge and PR authorize task commits when needed, using the standalone commit instructions only
in the session checkout. Explicit user restrictions on creating commits take precedence;
pending changes remain untouched if committing is restricted. Unclear selections require a
question; unrelated work, staged changes and secrets are excluded. Already committed work needs
no new task commit. A remaining dirty tree or unresolved failed checks still block integration.
If the target is already an ancestor of HEAD, no rebase is needed. Otherwise only the task's own
unpublished commits may be rebased. The agent records HEAD and verifies the range and publication
status with current remote evidence; stale tracking refs or author identity alone are insufficient.
Published, shared, unrelated or merge commits, or uncertain publication status, require a question
before rewriting. No new merge commit or force-push is substituted. Existing target history stays unchanged.
Review stays report-only; Push, fast-forward Pull, Init and Remote do not create commits.
Telegram users select an existing coding session with `/sessions` and `/use <ID>`;
messaging commands never create an unrelated empty session. Web submission can
start a new session using its explicit draft configuration. Existing command source,
run-ID deduplication, channel pairing, questions, approvals and outbox delivery apply.
A busy session does not silently queue a messaging publication command for later.
Coding commands are rejected as in-run continuations. Retry revalidates the saved
executor, directory and command context before dispatching a new run.
Saved command names with underscores remain readable for retries and workspace
admission; new preparations and retries store canonical hyphenated names.
Opening or selecting in the web palette never starts an agent or creates a session.

New web project-session drafts open the Git app once when the existing workspace probe
detects a repository. If detection is still pending at session creation, the persistent
chat page also uses the session's normal Git change discovery. Opening does not create a session or worktree, and manual closure
is respected until the next draft. Existing sessions restore their saved Control Center state.

The Git app has two tabs in order: **Änderungen** for changed files and **Repository**
with agent requests under **Aktionen** first, then workspace selection/removal, branch
management, the session target and remote configuration under **Einstellungen** in the
same scroll area. Settings keep labels above values; branch, target and remote share one row
when the card is wide enough and stack in narrow panels. Its footer places finish scheduling
on the left and project defaults on the right, stacking them in narrow panels. The changes tab shows known positive addition totals in green
and deletion totals in red; zero and unknown totals are omitted. Existing repositories start in the
changes view; missing repositories use the repository tab for setup.
The primary actions are **Prüfen** (`git-review`), **Abschließen** (`git-finish`),
**Pushen** (`git-push`) and **Pull Request** (`git-pr`). The initially collapsed
**Weitere Git-Aktionen** section contains Commit, local integration (`git-merge`) and
Pull (Fast-forward). Command IDs remain unchanged. Finish includes missing task review,
safe in-scope repairs, affected checks and task commits; Merge focuses on integration.
Merge and PR descriptions disclose their task-commit authorization; there is no commit
checkbox or additional UI mode. Push and Pull never commit.
Repository initialization is offered under **Einstellungen** when no repository is detected,
not among daily actions. It remains available through the shared command palette.
The overview keeps a separate store context from the active editor so
opening a different repository's file cannot replace the session's change list.
Individual paths still open normal editor tabs in comparison mode. Remote address
changes run as agent requests. Addresses must be network URLs
without embedded credentials, query strings or fragments; authentication belongs on
the computer. The app never exposes credential-bearing stored addresses. Existing
sessions explicitly adopt a changed target through `PUT /coding/sessions/{id}/target`,
which checks the preview's URL fingerprint against the current repository. Local
sessions retain the same target guard. Project remote defaults change only when the
session uses that project's configured computer and repository. Init and remote
setup from a worktree draft first create a local session so setup can succeed before
the first remote fetch.

## Agent flow contract

Under **Einstellungen**, **Abschluss einplanen** is available in local checkouts and worktrees and reflects a pending `/git-finish`
message in the current session. Checking it submits that ordinary command through the
[durable message queue](harness.md#message-queue); unchecking removes the pending message.
Removal stays available without a connected computer or a loaded repository catalog.
There is no next-Build flag or generated follow-up. Without an active run the command can
start immediately, exactly like manual submission. Draft text and attachments remain untouched.
Queued commands display their original slash invocation; sent commands display the captured
prompt actually submitted. `/git-finish` is also available in the palette and Git app.

The command shares Review, Commit and Merge instruction cores: complete only missing or stale
review/check evidence, safely correct required in-scope findings, recheck affected areas and
commit task changes. On the configured target branch it stops there; otherwise it integrates
through a separate worktree and a fast-forward-only update of the original target checkout.
Valid prior work is reused under the shared Git evidence rules above; no automatic branch
switching or moving user work makes a checkout ready.
Review, Finish, Merge and PR use the same risk-first review criteria: affected code, callers,
dependencies, tests and documented load; correctness, security/data loss, concurrency/scaling,
missing protection for risky logic, significant performance problems, then unnecessary complexity.
Findings require a confirmed code path, a concrete failure case or maintenance cost, and the
smallest complete fix. Reusing existing helpers and deleting unnecessary code take precedence
over new abstractions or test infrastructure; safeguards and documented tradeoffs remain.
Optional simplifications neither block integration nor authorize unrelated refactoring.
Finish and Merge require resolving required findings and failed checks before proceeding;
PR reports these blockers without repairs. All ask for necessary decisions;
questions and analysis do not authorize Git effects. There is no completion tool or phase
progression. Standalone review remains report-only. Normal admission, permissions, profile
selection and error handling apply; dispatch revalidates the computer, checkout, branch and
target. Predecessor-bound queue entries retain their failed/cancelled-predecessor checks. Pending
messages survive restart, but an already-started run and its Git effects are never replayed
automatically.

Git action buttons are shortcuts to the same message queue, not direct run starters. Active
runs, pending messages, missing Git prerequisites and an offline computer do not disable them.
The action list remains visible without repository discovery; message-submission restrictions
such as read-only sessions still apply. Draft text and attachments remain untouched.
Workspace selection retains its same-session active-run check and validates the selected
computer, repository and directory permissions.

There is no host merge queue, target reservation or workspace-occupancy inventory in model
context. A stable instruction says that the workspace may be shared, not that another worker
is active. When tools show ongoing work or a temporary Git lock, the model waits between
checks and resumes on the current state. Dirty files alone are not evidence of active work.
For separate-worktree local integration, preparation requires a clean session worktree and an existing local target branch,
not a clean original checkout or its target-branch checkout. The original remains untouched
while its committed target is incorporated and the combined result is reviewed in the worktree.
Only immediately before the fast-forward-only update through the original checkout must both
working trees be clean, the original be on the target branch and the reviewed commit be verified.
If the target advances, the model incorporates it and reviews/checks affected integration areas before retrying.
Before asking about a blocked original checkout, the model checks current tool evidence for
temporary activity or Git locks and completes independent permitted work. It never makes the
original ready by committing, stashing, resetting or discarding unrelated changes.
Clear conflicts and required integration findings are repaired in the task worktree; unresolved intent requires options and a
recommendation. Sessions do not reserve checkouts or shared Git metadata: another session's
queued, running or waiting run does not block chat, Git commands, worktree creation or
workspace changes. Git effects retain normal shell authorization and run cancellation.
Git locks and rechecks do not provide guaranteed mutual exclusion against external writers.

Commands enter the existing AgentRuntime through the ordinary message path. The
canonical user message retains the short command. Host-resolved task instructions
and Git context are snapshotted in its private runtime metadata and projected only
as the selected command's model-facing task. Compaction and model-facing session
retrieval preserve this task text and context. This is not a new tool permission or
workflow engine. Mutation commands switch the built-in Plan selection to Build only
when the session allows that profile; custom profile and tool restrictions remain.
The private metadata key `codingCommand` is retained for compatibility with existing
histories and retries; it carries any prepared command contribution. New commands
also snapshot their composed task instructions plus arguments as `prompt`. Git reuses
the same commit core and commit-message rules for standalone Commit and optional
pre-commit steps; review instructions are shared by Review, Finish, Merge and PR. Prompt
composition removes the option token and avoids conflicting commit prohibitions.
This adds no backend Git execution. The message API
exposes that snapshot as `commandPrompt`, including for predecessor-bound commands; execution wrappers
and resolved runtime context remain private. The canonical message content retains
the original invocation for undo and retry. Older messages without a prompt snapshot
keep showing their invocation rather than rebuilding it from the current catalog.

Git results include the short hash and full stored message of each reported resulting commit,
read from Git and quoted verbatim in a text block, including body and trailers. This includes
all commits created by the action even if a later integration step is blocked. Output brevity
does not truncate commit messages; local integration and publication remain separate outcomes.

The agent uses ordinary file, shell and available integration tools. Git credentials
and host CLIs such as `gh` belong on the selected computer. Missing PR authentication
or tooling is reported, not supplied by a new implicit integration. Project instructions
continue to govern commit conventions and checks. Review and publication safeguards
are agent instructions plus the existing tool authorization; they are not a Git sandbox
or a deterministic proof of review quality. The task explicitly requires non-force
pushes of the reviewed commit, affected review/checks after target advancement, resolution of
in-scope local integration findings and failed checks (PR stops instead), and preservation of unrelated changes. Execution remains
coordinated per session, not per repository. Worktree removal alone rejects overlapping
active run directories on the same owner-bound computer, using run snapshots rather than
the sessions' current directory preferences. This includes queued runs and runs waiting
for approval or input, but not unrelated worktrees sharing Git metadata. Git-state,
permission and deletion-consent checks remain binding. External processes can still change
the repository and must be inspected by the agent.

Git subprocesses run in a separate process group/tree. Cancellation terminates the
tree and bounds pipe cleanup; a dispatched preparation with a lost response,
cancellation or transport timeout has an unknown outcome and must be inspected.

The existing changed-files view compares HEAD with the working tree. It is not a
publication indicator: the agent must also inspect committed differences against the
target. Publication URLs and results remain canonical chat messages, and repeat PR
commands inspect the host for an existing request instead of blindly creating one.

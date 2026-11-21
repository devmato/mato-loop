# Skills

MatoLoop implements the [Agent Skills package format](https://agentskills.io/specification):
a directory containing `SKILL.md` with YAML frontmatter and Markdown instructions,
plus optional scripts, references, assets, and other files. `name` is a lowercase
hyphenated identifier of at most 64 characters; `description` is at most 1,024
characters. Optional and vendor metadata is retained as inert data. Metadata cannot
register tools, install dependencies, inject credentials, or grant permissions.

## Central library

The backend is the sole source of truth. There is no discovery of skill directories
on connected computers or in workspaces. The existing scoped `skills` settings
documents retain only stable IDs, names used to locate packages, content revisions,
enabled state, and the instance's personal-skills switch. Content lives as ordinary
files in the persistent data volume:

```text
/data/skills/global/<skill-id>/<revision>/<name>/SKILL.md
/data/skills/users/<user-id>/<skill-id>/<revision>/<name>/SKILL.md
```

`SKILL.md` supplies catalog names, descriptions, instructions, and metadata. A
content revision hashes sorted relative paths, file bytes, and executable flags;
ZIP timestamps and compression do not affect identity. Directories are immutable
publications, not editable mirrors. Use the UI or skill API to publish changes.
Publishing writes a complete private staging directory, then atomically renames
it before switching the registry reference inside the scoped database transaction.
A failed database commit leaves only an unreferenced directory. Startup reclaims
unreferenced publications older than one day; loaded session copies have their own
session lifecycle. There is no user-facing revision-history feature.

The first startup after upgrading converts inline settings skills to `SKILL.md`
packages before serving requests. It preserves scoped IDs, owners, enabled state,
and instructions, normalizes names, and disambiguates normalized collisions with
the stable ID. Inline content is removed only after complete publication. This
conversion is restart-safe and fails startup on error. Normal runtime has only
the package-backed path. No relational schema changes are needed.

Personal overrides of shipped defaults keep their existing scoped identities.
At startup, bundled packages update in global and personal scopes. The registry's
`bundled_files` map tracks the last shipped hash of each managed file, including its
executable flag. Only files still matching that hash are replaced or removed;
user-edited or deleted files and untracked resources are preserved. New shipped
files are added only at unused paths. Packages are published before registry changes
commit; repeated startup is idempotent. If a generated update fails package validation,
startup keeps the valid original and logs a warning; failures reading the original
still abort startup. No background worker or runtime inheritance
is involved, and exports contain complete standard Agent Skills packages without
the registry's update metadata.

Writing-style instructions remain in `SKILL.md`; personal examples belong in
`references/writing-examples.md`, which is not a bundled file. The skill reads that
reference through ordinary file permissions before using stored examples. Legacy
packages are split only when their instruction prefix exactly matches the known
shipped version. Example text and unrelated resources are preserved; custom
instructions or a conflicting examples path leave the package untouched.
Changing a bundled description intentionally changes the catalog prefix; changing
bundled instructions changes the active skill prefix for subsequent runs. Running
catalogs and historical requests remain fixed.

Shipped defaults define their initial enabled state individually: `writing-style`
starts enabled, while `weather`, `assistant` and `github-repo-ops` start disabled. They support reset and cannot be
deleted. Personal skills may be disabled instance-wide, with the existing administrator exception.
Back up the database and `/data` together; the database alone does not contain skills.

The bundled `assistant` skill (ID `00000000-0000-4000-8000-000000000103`)
guides setup and scheduled reviews. Setup enables it for the user and creates an
ordinary visible session with a disabled automation. The automation owns enabled
state, schedules, and model selection. Assistant settings retain only session and automation IDs.
`GET /assistant` returns those IDs, `initialized`, `enabled`, and an optional `errorCode`;
`POST /assistant/setup` ensures the stable binding and enables the personal skill. There
is no assistant configuration or context mutation API and no `customize_matoloop` area.

The setup command guides the user to the assigned session. The model publishes
`references/preferences.json` in the personal skill through ordinary `edit_skill` or
`save_skill`: a JSON object with integer `version: 1`, a nonempty `sources` list of objects,
and optional string `instructions`. Source objects hold user-chosen normal tool/integration
references and scope, not host-defined source types or filters. The host does not generate
preferences. Initialization requires this valid published file in the owner's enabled
personal skill. Activation, enqueueing, and execution of the assigned automation check
initialization; pausing remains possible. Scheduling starts disabled with a 15-minute cron;
enable it through normal automation controls after permission preparation.

Reviews use ordinary discovered tools directly, including their pagination and permission
checks.
Working style is plain text in `instructions`, not a separate mode or permission.
The assistant uses the shared [runtime interaction lifecycle](harness.md#runtime-and-coordinator),
including nonblocking automation checks during open approvals/questions. It has no
separate admission, action-ownership or permission path. Unanswered requests are not consent.

Notes use `session_state` context; processed
source IDs/versions use `message_metadata` namespace `assistant`, with successful
tool-message references. These annotations are not proof of external effects or
authorization. There is no assistant worker or separate source adapter.
Cross-chat target-session permission preflight is not implemented; prepare permissions
in the assigned assistant session. Setup does not copy grants or change tool policies.

## Import and editing

The existing Skills pages manage personal and global scopes. Import accepts a
standalone `SKILL.md` or a ZIP containing one package, either at the archive root
or in its correctly named directory. Export produces a standard ZIP with the
named directory. Text files use the existing file editor; binary assets can be
uploaded, downloaded, replaced, or removed. Updating `SKILL.md` preserves all
other files and updating a supporting file preserves the definition and metadata.
Mutations compare the reviewed package revision and reject concurrent changes.

Packages are limited to 256 files, 512 directories including the package root,
16 directory levels below that root, 4 MiB per file, 8 MiB uncompressed total, and
128 KiB for `SKILL.md`. These limits are checked before publication as well as
when reading stored packages. Archive uploads have a bounded HTTP request body. Portable
relative paths are required; traversal, absolute paths, duplicate/case-colliding
paths, conflicting file/directory paths, symlinks, special files, and encrypted
archive entries are rejected. The library uses no-follow directory walks.

## Runtime and connected computers

At run start, no skills are active and the model receives only the effective catalog. `load_skill` loads
a named or clearly relevant entry before it is used. Several skills may be
active; loading the same skill does not duplicate its instructions. Instructions
are included in subsequent model requests under ordinary context budgets and
remain subordinate to explicit user instructions and host policies. Required web
search may follow one initial skill load; further skills remain available after
the required search. A direct acceptance in the latest user message may identify
a skill from the immediately preceding assistant's own offer to use or set it up.
This only resolves skill selection; explicit-intent requirements and action permissions
still apply. The model uses the conversation to resolve follow-up requests before
matching the resulting task against catalog descriptions, and reloads matching
skills in each new run. Earlier skill use or offers alone do not select a skill
for an unrelated task. File contents and retrieved material cannot trigger skill
loading or modification on their own. The selection contract is stable system text; changing
it invalidates older catalog-bearing prefixes without rewriting historical turns.

Loading provides a fixed package revision and a session-files base path. Reference
files are read on demand through normal file tools. `load_skill(on_computer=true)`
also transfers the complete package through the existing authenticated transfer
tickets and prepares a working directory in the selected Agent's session cache.
The result supplies its real computer path. Folder structure, bytes, and executable
flags are preserved (Windows uses its normal executable semantics). Active copies
are leased against cache eviction until the run ends or the connection closes.
An older Agent lacking these operations reports normal capability unavailability.

Use normal file transfers for task inputs and normal file tools to return results.
Use the existing shell tool to run scripts on the selected computer, with its
installed programs and current permissions. Backend paths must never be passed
as computer paths. After switching computers, load with `on_computer=true` again;
the old computer base path is no longer offered in the active prompt. Preparing
a package does not grant filesystem, external-directory, shell, or credential
access. Working-copy changes do not modify the central library.

## Chat authoring and approval

`save_skill` defaults to personal, including for administrators. `scope=instance`
requires an explicit request for a global skill and live administrator authority.
`edit_skill` uses the selected catalog ID and scope; it does not silently promote
or demote a skill. The service checks authorization before generation and again
when saving. Personal and global approval grants have different scope identities.

The normal session agent researches and authors packages with the existing web,
file, transfer, and shell tools and their permissions. It writes `SKILL.md` and
supporting files into a session-files directory, then calls `save_skill(path=...)`.
Downloaded documentation can be copied into `references/` without being regenerated
through a model response. Scripts can be tested on the selected computer through
normal transfers and shell tools. There is no separate authoring model, brief,
structured draft generator, or skill-specific output-token limit.

For edits, `edit_skill` checks ownership and policy, then provides a complete
working copy without activating the skill's instructions. It returns the target
ID, scope, expected revision and executable paths. The agent changes that copy
with normal file tools and passes these values to `save_skill`. Publication replaces
the complete package, so unchanged files and metadata must remain in the working
copy. Executable paths are explicitly supplied because session files do not store
portable executable flags.

`save_skill` authorizes `session.files.read` for the source directory before
reading it, then materializes a separate,
editable review copy outside the run's file leases. Scope and file list are visible
in the existing approval card. The startup settings migration removes the retired
skill-creation model assignment and transfers the old `build_skill` availability
flag to `save_skill` without widening access.

Approval binds the entire package hash, including supporting scripts, rather than
only `SKILL.md`. After editing the draft, refresh the package preview before
approving. The host rereads the complete draft and rejects any change after review.
`skills.write=auto` uses the original submitted package revision. Drafts remain in
the session for inspection. Created and edited skills enter the next run's catalog.

## Privacy and budgets

Authoring uses the session model's normal context, trust, pseudonymization and
output budgets. Sources reach the model through ordinary authorized reads;
copying a reference file into a package does not require sending all its bytes
to the model. Source content remains untrusted and cannot authorize publishing.
Secrets do not belong in skills. All authoring tools consume the profile's normal
tool-call budget when finite. Package and file bounds apply regardless of the
model or tool-call limit.

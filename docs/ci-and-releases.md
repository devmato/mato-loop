# CI and Releases

GitHub is the primary host at [devmato/mato-loop](https://github.com/devmato/mato-loop). GitHub Actions verifies MatoLoop and publishes GHCR images and GitHub releases. Forgejo remains a Git pull mirror with Actions disabled. Both `.github/workflows` and `.forgejo/workflows` stay in the repository; Forgejo workflows remain usable if it is the primary host. CI does not deploy MatoLoop, update a server, touch deployment storage, or perform Docker garbage collection on runners.

## Published Artifacts

The registry contains two OCI images:

```text
ghcr.io/devmato/mato-loop/mato-loop
ghcr.io/devmato/mato-loop/matoloop-agent
```

Both images currently target `linux/amd64` only. Every source version uses `YYYY-MM-DD-<SHA8>`, derived from the commit date in UTC and its first eight SHA characters. The same commit has the same version on `develop` and `main`; local changes append `-dirty`. Native MatoLoop Agent installations fetch the running backend's exact commit; CI does not publish native archives.

The tag policy deliberately limits mutable artifact storage:

| Source | Checks | Image build | Publish |
| --- | --- | --- | --- |
| Pull request | Full checks and tests | Both images | Never |
| Feature branch push | Checks and tests only | No | Never |
| `develop` push | Full checks and tests | Both images | Mutable `develop` and immutable date/SHA tag |
| `main` push | Full checks and tests | No | Never; release tags only |
| Date/SHA release tag | Full checks and tests | Both images | Immutable date/SHA tag and `latest` |
| Manual GitHub Release | Full checks, tests, and migration round trip | Both images | Immutable date/SHA tag and `latest` |
| Manual GitHub CI | Full checks, tests, migrations, and version validation | Both images | Never |

Feature branches therefore consume runner cache only, each accepted `develop` revision retains an immutable image tag plus the moving `develop` tag, and releases retain fixed version tags. Registry retention remains an operator responsibility. The retained Forgejo workflow publishes to its configured `${CONTAINER_REGISTRY}/${CONTAINER_IMAGE_NAMESPACE}` instead.

The verification path also includes migration and source-version jobs; "checks and tests" does not mean only unit tests. Manual CI dispatch builds without publication. Use tags only from successful publication runs.

Pull requests run the complete test and production-image build path, but registry login, publication, and release creation are disabled. Repository secrets must not be passed to pull request jobs. This applies to same-repository and fork pull requests.

## Runner Requirements

GitHub workflows use GitHub-hosted `ubuntu-latest` runners. Native test jobs keep the same pinned toolchains and system packages as the Forgejo jobs. The migration job publishes its PostgreSQL service port and connects through `localhost`; Forgejo job containers use the `db` hostname.

The GitHub Agent test step runs its installed virtual-environment Python as root, matching the development container and supporting device-node and ownership fixtures. Image builds and publication copy the caller's Docker `config.json` into a private temporary directory so existing runner authentication remains available; cleanup removes only the temporary copy. No personal Docker Hub token is required by these workflows, and Docker Hub transport or rate-limit failures can still occur.

The retained Forgejo jobs require a self-hosted Forgejo Runner registered with the `ubuntu-latest` label and running on `linux/amd64`. The runner needs:

- a Docker daemon available to job containers, commonly through `/var/run/docker.sock` or an isolated Docker service
- Bash, Python 3.11 or newer, Git, and permission to install the checked toolchains and system packages
- permission to build, tag, inspect, and push OCI images
- outbound access to Forgejo, GitHub, Node.js, PyPI, Yarn/npm package sources, base-image registries, and the Forgejo container registry
- enough CPU, memory, and disk for the MatoLoop and MatoLoop Agent images to build concurrently
- a current Git client and normal TLS trust for the configured endpoints
- Forgejo Actions task-queue concurrency enabled so publication workflows with the same group are serialized
- an empty runner `[container].network` setting so the migration job can reach its `db` service by hostname

Docker socket access is effectively root access to the runner host. Pull request builds execute repository code and Dockerfiles even though they receive no secrets. Run untrusted pull requests only on ephemeral, isolated runners with no production mounts, credentials, internal-network privileges, or reusable trusted-job workspace. Do not colocate this runner with a MatoLoop production deployment.

Forgejo Runner does not own host Docker retention. Configure image, layer, BuildKit cache, container, and volume garbage collection outside the workflows and monitor disk use. Never solve runner pressure by mounting or pruning deployment Docker storage.

Each workflow uses its host's Actions cache for locked UV and Yarn package downloads. Yarn's global cache override is disabled so `YARN_CACHE_FOLDER` selects the persisted cache in CI and development containers. Cache misses only make dependency installation slower; they never skip `uv sync --frozen` or `yarn install --immutable`. Docker image builds separately reuse normal builder layers and BuildKit package-cache mounts, whose retention remains a runner-administrator responsibility.

Backend and agent test jobs report their slowest tests using the test runners' built-in duration reporting. Develop publication checks the current branch head before building and again before publishing; superseded revisions skip unnecessary image builds, and revisions superseded during a build skip publication.

Develop and release publication workflows share one serialized concurrency group without automatic cancellation: a release and develop push can reference the same immutable version. Superseded develop revisions still skip building or publishing through the branch-head checks. Other refs supersede older runs of that ref. Interrupted publication can leave an incomplete image pair; only use tags from successful runs. Retries reuse validated existing fixed images. Runs already queued with an older workflow retain its concurrency settings.

## Test Environment Parity

The backend, frontend, and agent test jobs run directly in the runner environment, not in the application images. System dependency changes must update both Dockerfiles and the corresponding CI installation. Webfetch's `curl_cffi` transport is installed from the backend Python lockfile; no separate browser binary is required.

After a Dockerfile change, run `./scripts/dev.sh compose build backend` (or the affected service) before validation: `dev.sh check` and `dev.sh test` reuse existing images. Bind-mounted source changes do not install new system dependencies.

For shared prompt, tool schema, settings default, and error contract changes, run the complete affected target test suite and update its expectations in the same change. Prefer focused behavior assertions over a digest of all prompt text and schemas; a digest also fails on unrelated, intentional wording changes and cannot explain which contract changed.

Passing the default suites does not imply all integration tests ran. Backend PostgreSQL tests need their explicit flags or environment variables, RAG tests use `./scripts/dev.sh test rag`, and additional agent browser scenarios require their documented opt-in environment. Native Windows tests skip on Linux. The agent's packaged OpenSCAD test also skips when OpenSCAD/Xvfb are absent; the native CI job does not install them. Review skip reasons separately from failures and report which opt-in checks actually ran.

### Test structure

Keep tests with the behavior they exercise. Approval-card interactions share one component suite; notification-center tests cover the notification UI. Reused backend setup lives in domain-specific `tests/*_helpers.py` modules, not in another test module. Prefer standard-library helpers such as `contextlib.nullcontext` to no-op session context classes; retain custom fakes when their lifecycle is itself under test.

Backend tests with incomplete database mocks explicitly request `mock_db_settings`; settings behavior is not patched automatically for the entire suite. Frontend component tests reuse `tests/quasar-options.js` for real Quasar components and directives. Supply required router context and mock only browser APIs unavailable in jsdom instead of suppressing Vue warnings.

Keep compatibility cases only for a concrete supported boundary: persisted user data, supported upgrades, or active interfaces. Retire obsolete fixtures with their removed feature. A guard that rejects an obsolete pending approval is still a safety boundary, not an alternate execution path to preserve for its own sake.

## GitHub Configuration

Set `develop` as the default branch. Checks use `contents: read`; image publication gets `packages: write`, and release preparation/publication gets `contents: write`. All GitHub jobs use the automatic `GITHUB_TOKEN`, with no personal token, registry secrets, or publication toggle. After the first publication, make both GHCR packages public to allow anonymous pulls.

The manually dispatched **Release** workflow verifies the migration round trip, then runs `./scripts/release.sh` to check, test, commit, tag, and push. Pushes using `GITHUB_TOKEN` do not trigger push workflows, so a dependent publication job checks out the newly generated commit and builds/publishes both images before creating the GitHub release. Both GitHub workflows use the same `ci-publication` concurrency group. Local releases still trigger CI through the maintainer's normal Git credentials. No repository secret is required for the Release button.

Protect `develop`, `main`, and date/SHA tags. Branch and tag rules must permit the GitHub Actions token to push the checked release commit, fast-forward `main`, and create the release tag; `contents: write` does not bypass protection rules or required checks. Include `.github/workflows`, `.forgejo/workflows`, CI helpers, Dockerfiles, and lockfiles in workflow reviews.

Shared helpers `scripts/ci/create-release.py` and `scripts/ci/check-develop-revision.py` receive `CI_API_URL`, `CI_REPOSITORY`, and `CI_TOKEN` from the workflow. GitHub passes its API URL; Forgejo passes its server URL with `/api/v1`. Release notes, changelog generation, image tags, and source-version checks are shared.

## Historical GitHub Cutover and Current Forgejo Mirror

The cutover checklist below records the migration procedure, not installation steps for a new MatoLoop deployment. GitHub is now the primary source. The current mirror copies Git refs only and should keep Actions disabled.

1. Create an empty public `devmato/mato-loop` repository without an initial README or license commit. Configure GitHub permissions before uploading; matching pushes start CI and publication immediately.
2. Commit the migration changes. Transfer the existing Forgejo server branches and tags to GitHub from a fresh bare clone, rather than pushing every local working branch. Include the migration commit on `develop`.
3. Set GitHub's default branch to `develop`, verify the first CI/publication run and anonymous pulls of both public GHCR images, then switch this checkout's `origin` to `git@github.com:devmato/mato-loop.git`. Keep the old remote as `forgejo` if useful.
4. Rename the existing Forgejo repository to preserve its issues, PRs, and Actions history. Create a new migration under the previous `devmato/MatoLoop` name from `https://github.com/devmato/mato-loop.git`, selecting **This repository will be a mirror**. Forgejo cannot convert an existing repository into a pull mirror. Disable Actions on the mirror, synchronize it, and compare branch/tag object IDs with GitHub.
5. Update deployment image addresses deliberately. Keep existing Forgejo images available for rollback. Existing managed Agents preserve their stored source URL and can continue fetching from the Forgejo mirror; new installations default to GitHub.

The mirror copies Git commits, branches, and tags; it does not continuously synchronize issues, PRs, release entries, or images. Start releases only on the primary host or locally against it.

## Retained Forgejo Configuration

Configure these repository or organization Action variables:

| Variable | Value |
| --- | --- |
| `CONTAINER_REGISTRY` | Forgejo registry host and optional port, without `https://` |
| `CONTAINER_IMAGE_NAMESPACE` | Optional image path; defaults to the lowercase Forgejo `owner/repository` |

Configure these Action secrets only for trusted `develop` and release jobs:

| Secret | Purpose |
| --- | --- |
| `CONTAINER_USERNAME` | Dedicated registry publisher account |
| `CONTAINER_PASSWORD` | Token with permission to write packages in the image namespace |
| `FORGEJO_TOKEN` | Token with repository branch, tag, and release write permission |

The release job uses the configured `FORGEJO_TOKEN`; it is not a registry credential. Pull request jobs receive none of these configured secrets. The runner registration token belongs in the runner's external configuration, not in repository variables or secrets.

Use a dedicated least-privilege publisher account. Allow it to update protected `develop`, `main`, and date/SHA release tags. The registry token needs no MatoLoop application, database, deployment-host, provider, or MatoLoop Agent credentials.

Protect `develop`, `main`, and date/SHA tags so only trusted maintainers can modify them. Require the CI workflow for branch changes and protect reviews of `.forgejo/`, `scripts/ci/`, `scripts/release.sh`, Dockerfiles, `libs/python/version`, and package lockfiles. The Forgejo server URL must use HTTPS.

## Release Process

`develop` is the default branch. Releases originate there and fast-forward release-only `main`.

Noteworthy changes use native Git trailers at the end of the commit message. Write the text in English and repeat the trailer when one commit contains multiple noteworthy changes:

```text
Add mail integration

Release-Note: Added: Add IMAP and SMTP integration.
Release-Note: Security: Reject credentials in mail server URLs.
```

The supported categories are `Added`, `Changed`, `Fixed`, and `Security`. Keep multiple trailers together in the final commit-message paragraph so Git recognizes all of them. The trailer is optional; commits without one and merge commits are omitted. CI rejects malformed trailers.

There are no major/minor/patch increments or `Release-Bump` trailers. Python/npm manifest versions are fixed packaging placeholders, not product versions. The shared `libs/python/version` helper calculates the product version and writes `build-version.txt` before packaging. Backend, Linux Agent, and Windows Agent report this version; installed packages do not need Git. Development checkouts read Git directly; missing metadata reports `unknown`.

Start a release manually from the `develop` branch in GitHub under **Actions > Release**, or from a clean, current local `develop` checkout. The retained Forgejo workflow has the same manual entry point when Forgejo is the primary host:

```bash
./scripts/release.sh
```

Local releases require Git, Python 3, Docker/Compose, and both local `develop` and `main` exactly matching their remote branches. The local script runs checks/tests; the GitHub manual workflow additionally verifies the migration round trip before invoking it. Inspect `--dry-run` before preparing a local release.

The script adds dated notes to `docs/user/changelog.md`, runs all checks and tests, and commits on `develop`. It derives the version from that final commit, fast-forwards `main`, creates the annotated date/SHA tag, returns to `develop`, and atomically pushes both branches and the tag. A committed changelog cannot contain its own commit hash, so it uses date headings while the hosted release carries the exact version. Package manifests and lockfiles are not rewritten. `--dry-run` previews the notes and validates the branch state. CI accepts only tags matching their clean source version and pointing to commits on `main`. Do not create release tags manually. Previous `v*` tags remain valid release-note baselines during migration.

After checks and both `linux/amd64` builds succeed, the release job publishes these tags on both images:

```text
2026-10-08-a1b2c3d4
latest
```

It then creates the matching hosted release from the same categorized notes stored in the changelog. Workflow retries preserve existing fixed tags, publication workflows are serialized, and `latest` never moves to an older source revision. Git ancestry determines release order, including multiple releases on the same day; hashes are not compared alphabetically. Forgejo concurrency is best-effort, so the required task queue and branch/tag protections remain part of the integrity boundary when running there. Registry pushes across two repositories are not transactional: if publication stops partway through, no hosted release is created; retry to publish the missing image. For a failed manual GitHub publication, use **Re-run failed jobs** so the successful preparation job's commit/tag outputs are reused instead of creating another release. Artifact publication is not deployment automation; operators choose when to pull a version.

CI passes the calculated version through `MATOLOOP_VERSION` and the full commit through `BUILD_REVISION`. Local Docker builds calculate them from a read-only Git mount in a version build stage, without copying Git into the runtime image. Linked worktrees need host-calculated `MATOLOOP_VERSION` and `BUILD_REVISION` arguments because their Git metadata is outside the build context. Recompute them after source changes. Without a valid full revision the backend cannot provide a managed Agent installation target. The shared helper can be run directly:

```bash
python3 libs/python/version/src/matoloop_version/__init__.py
```

### Local release failure recovery

The local release script does not automatically undo preparation after failure. Before retrying, inspect `git status`, local/remote `develop` and `main`, release tags, and any triggered publication run. A failure may occur after a changelog edit, release commit, local fast-forward/tag, or remote push. Determine the last completed stage before making another release; blindly rerunning preparation can create duplicate or conflicting release state. For failed manual GitHub publication, reuse **Re-run failed jobs** as described above.

## Compose and Rollback

`docker-compose.yaml` uses the published MatoLoop and MatoLoop Agent images configured in the production `.env`. Start from the tracked template:

```bash
cp .env.example .env
# Set the password, public origin, host paths/binding, and matching published image tags.
# Configure HTTPS before production login with Secure cookies.
docker compose pull
docker compose up -d
```

Use `docker compose -f docker-compose.no-agent.yaml up -d --remove-orphans` for a production stack without a MatoLoop Agent. `--remove-orphans` also removes a `matoloop-agent` container left by an earlier standard-stack deployment.

For a reproducible `develop` deployment, set the same immutable commit tag on both images and pull before recreating containers:

```env
MATO_LOOP_IMAGE=ghcr.io/devmato/mato-loop/mato-loop:2026-10-08-a1b2c3d4
MATOLOOP_AGENT_IMAGE=ghcr.io/devmato/mato-loop/matoloop-agent:2026-10-08-a1b2c3d4
```

```bash
docker compose pull
docker compose up -d
```

Use the same date/SHA tag for MatoLoop and the MatoLoop Agent. `develop` and `latest` are not reproducible deployment references. Managed Docker Agents can also synchronize to the running backend's commit through the WebUI. Such updates survive container restarts, while recreating the container restores the selected image; credentials and workspaces stay in their mounts. See [computer-maintenance.md](computer-maintenance.md).

Use the mutable `develop` tag only for short-lived test environments and run `docker compose pull` before every recreation. Never use `develop` as a rollback target.

Rollback by setting both image variables to the previous fixed date/SHA tag, pulling, and recreating the containers. Because startup applies Alembic migrations, an image rollback may also require restoring the database and `/data` from the matching pre-upgrade backup. Never use `develop` or `latest` as a rollback target.

Use the German [operations and recovery runbook](user/betrieb-und-datensicherung.md) for initial configuration, reverse-proxy requirements, monitoring, and a coordinated database/files/key backup. Documentation builds and worktree isolation are described in [development.md](development.md).

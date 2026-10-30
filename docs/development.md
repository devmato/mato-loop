# Development and Documentation Maintenance

Use `./scripts/dev.sh` from the repository root for all development containers, checks, tests, and migrations. Production Compose files are deployment templates, not development environments.

## Start and inspect

```bash
./scripts/dev.sh up --build -d
./scripts/dev.sh status
./scripts/dev.sh logs backend
./scripts/dev.sh logs matoloop-agent
./scripts/dev.sh doctor
```

Default endpoints are the Web UI at `http://localhost:10020`, handbook at `/docs/`, API at `http://localhost:6020/api`, and OpenAPI at `/api/docs` on the backend. `up --pwa --build -d` enables the installable web-app development mode. Mobile PWA tests require HTTPS.

Python and frontend sources reload from the repository mount. Agent Python changes restart its in-memory runtime, including running browsers; durable Agent data and profiles remain.

## Parallel worktrees

The tracked `.env.dev` sets the Compose project name to `mato-loop`; it is **not automatically worktree-local**. Before starting another checkout, create a gitignored `.env.dev.override` with a unique project name and unused host ports, for example:

```dotenv
MATO_LOOP_COMPOSE_PROJECT_NAME=mato-loop-my-task
MATO_LOOP_HOST_BACKEND_PORT=16020
MATO_LOOP_HOST_FRONTEND_PORT=16021
MATOLOOP_AGENT_DATA=./data/my-task-agent
```

Choose unused values for your host. Relative bind paths resolve within that checkout; do not point them at another Agent's state. The Agent workspace volume follows the Compose project name. Backend/frontend image names follow the Compose project; the development Agent image has the shared tag `matoloop-agent-dev`, so coordinate Agent image rebuilds between worktrees. A documentation-only `compose run --no-deps backend` does not need the Agent or exposed service ports.

`dev.sh check` and `test` mask installed dependency environments with temporary anonymous volumes, while named download caches follow the selected Compose project. These commands do not make the database, service containers, or bind-mounted Agent state worktree-local by themselves.

## Data

The selected Compose project owns `app_data` (backend files and secrets), `db_data`, `embedding_cache`, `backend_uv_data`, `frontend_node_modules`, `frontend_yarn_cache`, `matoloop_agent_dependency_data`, and `matoloop_agent_workspace` (Agent home at `/workspace`). Agent state remains a bind mount under `MATOLOOP_AGENT_DATA`. The development workspace no longer uses `MATOLOOP_AGENT_HOME`. Infinity's pinned model download can take minutes on first start. Its model cache is reproducible; lexical search remains available during embedding degradation.

`stop` stops services; `down` removes containers. **`reset-storage` removes all named development volumes, including accounts, database, secrets, backend files, and the Agent workspace.** It does not remove the Agent state bind path. Never run it as incidental cleanup, especially where checkouts share a project name.

## Checks, migrations, and evaluations

```bash
./scripts/dev.sh check backend
./scripts/dev.sh test backend
./scripts/dev.sh check frontend
./scripts/dev.sh test frontend
./scripts/dev.sh check matoloop-agent
./scripts/dev.sh test matoloop-agent
./scripts/dev.sh test rag
./scripts/dev.sh db current
./scripts/dev.sh db upgrade head
```

Omit the target to check/test all components. `test rag` starts PostgreSQL and Infinity and verifies real hybrid retrieval, trust filtering, and lexical fallback. Normal tests do not require that infrastructure. Rebuild an affected development image after system dependency changes; bind-mounted source does not update installed binaries.

Generate migrations with `./scripts/dev.sh db revision --autogenerate -m "description"`, then review the complete generated diff. See [AGENTS.md](../AGENTS.md) for validation scope and migration rules.

```bash
./scripts/eval.sh help
./scripts/eval.sh prompts --help
./scripts/eval.sh session-cache --help
./scripts/eval.sh project-context --help
./scripts/eval.sh coding-comparison --help
./scripts/eval.sh browser-performance --help
./scripts/eval.sh shell-decider --check
```

Evaluator arguments are forwarded unchanged. Browser-performance uses the running backend; the other targets use disposable containers. Shell-decider adds the evaluation-only Compose overlay and builds its image on demand. Raw outputs stay under ignored `data/` paths; link published conclusions from the [documentation index](index.md).

## Documentation

README and technical documentation are English. The handbook in `docs/user/`, its navigation, and application UI are German. Quote actual German UI labels in technical documents. Keep dated reports historical and compare current operational claims with code and configuration before changing them.

MkDocs builds only `docs/user/`; technical documents and component READMEs are repository documentation. The normal backend check includes a strict handbook build. For handbook-only changes, use the narrower build:

```bash
./scripts/dev.sh compose run --rm --no-deps \
  --env UV_LINK_MODE=copy --volume /opt/mato-loop/backend/venv \
  backend sh -lc 'uv sync --frozen --group docs && uv run --no-sync mkdocs build --strict --config-file /workspace/mkdocs.yml --site-dir /tmp/mato-loop-docs'
```

Use an isolated project as described above when running from a separate worktree. The output stays in the disposable container. A strict MkDocs build is not a whole-repository or external-link checker; review links and heading anchors outside the handbook separately.

The development backend builds the served handbook at startup. Markdown edits do not trigger Python reload. Refresh an already-running development preview with:

```bash
./scripts/dev.sh compose exec -T backend sh -lc 'uv run --no-sync mkdocs build --strict --config-file /workspace/mkdocs.yml --site-dir /tmp/mato-loop-static/docs && rm -f /tmp/mato-loop-static/docs/404.html'
```

Production images package the handbook during their build. Review navigation, code blocks, and edited pages in the generated site before publishing.

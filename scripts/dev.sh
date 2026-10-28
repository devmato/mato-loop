#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker-compose.dev.yaml"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

compose() {
    local args=(docker compose --env-file .env.dev)
    if [[ -f .env.dev.override ]]; then
        args+=(--env-file .env.dev.override)
    fi
    args+=(-f "$COMPOSE_FILE" "$@")
    "${args[@]}"
}

usage() {
    printf '%s\n' \
        "Usage: ./scripts/dev.sh <command> [args]" \
        "" \
        "Commands:" \
        "  up [--pwa] [--build] [-d] Start the development stack" \
        "  stop [service]      Stop services" \
        "  down                Remove containers" \
        "  reset-storage       Remove containers and volumes" \
        "  logs [service]      Show logs" \
        "  status              Show service status" \
        "  config              Show the resolved Compose configuration" \
        "  compose [args]      Run Docker Compose with development configuration" \
        "  doctor              Check pgvector and the embedding index" \
        "  db [args]           Run Alembic" \
        "  lock <target>       Update the backend, frontend, or MatoLoop Agent lockfile" \
        "  format <target>     Format backend or MatoLoop Agent code" \
        "  check [target]      Check backend, frontend, and MatoLoop Agent" \
        "  test [target]       Test backend, frontend, MatoLoop Agent, or local RAG stack" \
        "" \
        "Evaluations: ./scripts/eval.sh help"
}

run_backend() {
    compose run --rm --no-deps --env UV_LINK_MODE=copy --volume /opt/mato-loop/backend/venv backend sh -lc "$1"
}

run_frontend() {
    compose run --rm --no-deps --volume /workspace/apps/frontend/node_modules frontend sh -lc "$1"
}

run_matoloop_agent() {
    compose run --rm --no-deps --env UV_LINK_MODE=copy --volume /opt/matoloop-agent/venv --entrypoint sh matoloop-agent -lc "$1"
}

check_backend() {
    run_backend "uv sync --frozen --group docs && uv run --no-sync mkdocs build --strict --config-file /workspace/mkdocs.yml --site-dir /tmp/mato-loop-docs && uv run python -m compileall src/mato_loop_backend && uv run ruff check src tests /workspace/libs/python/version/src"
}

check_frontend() {
    run_frontend "yarn install --immutable && yarn lint && yarn build"
}

check_matoloop_agent() {
    run_matoloop_agent "test \"\$(id -u)\" = 0 && command -v apt-get git openscad rg xauth xvfb-run >/dev/null && uv sync --frozen && uv run --no-sync python -m compileall src tests && uv run --no-sync ruff check src tests /source/scripts/matoloop-agent-installer.py && uv run --no-sync ruff format --check src tests /source/scripts/matoloop-agent-installer.py"
}

test_backend() {
    local args=""
    if (( $# )); then printf -v args ' %q' "$@"; fi
    run_backend "uv sync --frozen && uv run pytest$args"
}

test_frontend() {
    run_frontend "yarn install --immutable && yarn test"
}

test_matoloop_agent() {
    run_matoloop_agent "uv sync --frozen && uv run --no-sync python -m unittest discover -s tests -v"
}

test_rag() {
    compose up -d --wait --wait-timeout 600 db embedding
    run_backend "uv sync --frozen && MATO_LOOP_RAG_INTEGRATION=1 uv run pytest tests/test_memory_rag_integration.py"
}

format_backend() {
    run_backend "uv sync --frozen && uv run ruff check --fix src tests"
}

format_matoloop_agent() {
    run_matoloop_agent "uv sync --frozen && uv run --no-sync ruff format src tests /source/scripts/matoloop-agent-installer.py"
}

run_target() {
    local operation="$1"
    local target="${2:-all}"
    case "$operation:$target" in
        check:all) check_backend; check_frontend; check_matoloop_agent ;;
        check:backend) check_backend ;;
        check:frontend) check_frontend ;;
        check:matoloop-agent) check_matoloop_agent ;;
        test:all) test_backend; test_frontend; test_matoloop_agent ;;
        test:backend) test_backend "${@:3}" ;;
        test:frontend) test_frontend ;;
        test:matoloop-agent) test_matoloop_agent ;;
        test:rag) test_rag ;;
        *) printf 'Unbekanntes Target: %s\n' "$target" >&2; exit 1 ;;
    esac
}

cd "$PROJECT_ROOT"

case "${1:-}" in
    up)
        shift
        up_args=()
        pwa=false
        for arg in "$@"; do
            if [[ "$arg" == "--pwa" ]]; then
                pwa=true
            else
                up_args+=("$arg")
            fi
        done
        if [[ "$pwa" == true ]]; then
            export MATO_LOOP_FRONTEND_MODE=pwa
        fi
        compose up "${up_args[@]}"
        ;;
    stop) shift; compose stop "$@" ;;
    down) shift; compose down "$@" ;;
    reset-storage) compose down -v ;;
    logs) shift; compose logs -f "$@" ;;
    status) compose ps ;;
    config) compose config ;;
    compose) shift; compose "$@" ;;
    doctor)
        compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT extversion AS vector_version FROM pg_extension WHERE extname = '\''vector'\'';"'
        compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT format_type(a.atttypid, a.atttypmod) AS embedding_type FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid WHERE c.relname = '\''memory_embeddings'\'' AND a.attname = '\''embedding'\'';"'
        compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT format_type(a.atttypid, a.atttypmod) AS episode_embedding_type FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid WHERE c.relname = '\''message_search_chunks'\'' AND a.attname = '\''embedding'\'';"'
        compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) AS memory_count FROM memories;"'
        compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT status, count(*) FROM memory_embeddings GROUP BY status ORDER BY status;"'
        compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) AS episode_chunk_count FROM message_search_chunks;"'
        compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT status, count(*) FROM message_search_chunks GROUP BY status ORDER BY status;"'
        compose exec -T embedding python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:7997/health', timeout=5).read().decode())"
        ;;
    db)
        shift
        compose run --rm backend uv run alembic "$@"
        if [[ "${1:-}" == "revision" ]]; then
            compose run --rm --no-deps --workdir /workspace backend \
                chown -R "$(id -u):$(id -g)" /workspace/apps/backend/alembic/versions
        fi
        ;;
    lock)
        case "${2:-}" in
            backend) compose run --rm --no-deps backend uv lock ;;
            frontend) run_frontend "yarn install --mode=update-lockfile" ;;
            matoloop-agent) run_matoloop_agent "uv lock" ;;
            *) printf 'Unbekanntes Lock-Target: %s\n' "${2:-}" >&2; exit 1 ;;
        esac
        ;;
    format)
        case "${2:-}" in
            backend) format_backend ;;
            matoloop-agent) format_matoloop_agent ;;
            *) printf 'Unbekanntes Format-Target: %s\n' "${2:-}" >&2; exit 1 ;;
        esac
        ;;
    check) run_target check "${2:-all}" ;;
    test) run_target test "${2:-all}" "${@:3}" ;;
    help|-h|--help|'') usage ;;
    *) usage >&2; exit 1 ;;
esac

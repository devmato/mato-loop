from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from mato_loop_backend import __version__
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api.errors import register_error_handlers
from mato_loop_backend.api.request_body_limit import MediaRequestBodyLimitMiddleware
from mato_loop_backend.api.router import api_router
from mato_loop_backend.automations import AutomationService
from mato_loop_backend.background import BackgroundProcessing
from mato_loop_backend.changelog import record_installed_build
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.session import AsyncSessionFactory, engine
from mato_loop_backend.diagnostics.logs import install_backend_log_handler
from mato_loop_backend.diagnostics.mcp import create_debug_mcp, debug_mcp_mount
from mato_loop_backend.diagnostics.service import diagnostic_retention_loop
from mato_loop_backend.execution.maintenance import stop_auto_updates
from mato_loop_backend.execution.terminals import terminal_coordinator
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.media.attachments import AttachmentService
from mato_loop_backend.memory.dreaming import DreamingService
from mato_loop_backend.memory.embeddings import embedding_client
from mato_loop_backend.memory.episode_indexing import (
    EpisodeIndexScheduler,
    EpisodeIndexService,
)
from mato_loop_backend.memory.indexing import MemoryIndexScheduler, MemoryIndexService
from mato_loop_backend.messaging.teams import TeamsScheduler
from mato_loop_backend.messaging.telegram import TelegramScheduler
from mato_loop_backend.models import (
    ApprovalRequest,
    ChatSession,
    Project,
    QuestionRequest,
    TitleGenerationStatus,
)
from mato_loop_backend.notifications import NotificationService
from mato_loop_backend.projects.coding import CodingScheduler
from mato_loop_backend.settings import InstanceLimits, SettingsStore
from mato_loop_backend.skill_migration import initialize_skill_library
from mato_loop_backend.voice.service import voice_coordinator

runtime_config = get_runtime_config()
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("mato_loop_backend").setLevel(runtime_config.log_level)
logger = logging.getLogger(__name__)


def _static_path(static_root: Path, full_path: str) -> Path | None:
    requested = (static_root / full_path).resolve()
    try:
        requested.relative_to(static_root)
    except ValueError:
        return None
    return requested


def _static_headers(full_path: str) -> dict[str, str]:
    if full_path == "sw.js":
        return {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        }
    if full_path in {"", "index.html", "manifest.webmanifest"}:
        return {"Cache-Control": "no-cache, no-store, must-revalidate"}
    return {}


def _mount_documentation(target: FastAPI, docs_dir: Path) -> None:
    @target.get("/docs", include_in_schema=False)
    async def documentation_redirect() -> RedirectResponse:
        return RedirectResponse("/docs/", status_code=308)

    target.mount(
        "/docs",
        StaticFiles(directory=docs_dir, html=True),
        name="documentation",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log_handler = install_backend_log_handler()
    try:
        async with _application_lifespan(_app):
            yield
    finally:
        logging.getLogger("mato_loop_backend").removeHandler(log_handler)
        log_handler.close()


@asynccontextmanager
async def _application_lifespan(_app: FastAPI):
    runtime_config.secrets_dir.mkdir(parents=True, exist_ok=True)
    # Migration failures must prevent serving a partially converted library.
    async with AsyncSessionFactory() as db:
        await initialize_skill_library(db)
    debug_mcp_stack = AsyncExitStack()
    try:
        debug_mcp, debug_mcp_app = create_debug_mcp()
        await debug_mcp_stack.enter_async_context(debug_mcp.session_manager.run())
        debug_mcp_mount.app = debug_mcp_app
    except Exception:
        logger.exception("Could not start the optional debug MCP")
        await debug_mcp_stack.aclose()
    try:
        async with AsyncSessionFactory() as db:
            session_storage = SessionStorage()
            owners = (await db.execute(select(ChatSession.id, ChatSession.user_id))).all()
            await asyncio.to_thread(session_storage.register_owners, owners)
            attachments = AttachmentService(settings=await SettingsStore.load(db, InstanceLimits))
            await attachments.backfill_derived_files(db)
            await AgentRuntime.recover(db)
            await voice_coordinator.recover(db)
            await db.commit()
            await AutomationService.recover(db)
            await db.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.status == "pending",
                    ApprovalRequest.request_type == "agent_action",
                )
                .values(status="expired", resolved_at=datetime.now(UTC))
            )
            await db.execute(
                update(QuestionRequest)
                .where(QuestionRequest.status == "pending")
                .values(status="expired")
            )
            await db.execute(
                update(ChatSession)
                .where(
                    ChatSession.title_generation_status
                    == TitleGenerationStatus.RUNNING.value
                )
                .values(title_generation_status=TitleGenerationStatus.FAILED.value)
            )
            await DreamingService.recover(db)
            await db.commit()
            session_locations = dict(
                (await db.execute(select(ChatSession.id, ChatSession.project_id))).all()
            )
            project_ids = set((await db.scalars(select(Project.id))).all())
            reconciliation = await asyncio.to_thread(
                session_storage.reconcile,
                session_locations,
                projects=project_ids,
                stale_staging_after_seconds=0,
            )
            if reconciliation.provisioned or reconciliation.restored or reconciliation.trashed:
                logger.info(
                    "Session storage reconciled: %s provisioned, %s restored, %s orphaned",
                    len(reconciliation.provisioned),
                    len(reconciliation.restored),
                    len(reconciliation.trashed),
                )
            if reconciliation.restored:
                await attachments.backfill_derived_files(db)
            removed_legacy_files = await asyncio.to_thread(
                session_storage.cleanup_legacy_attachment_storage,
                session_locations,
            )
            if removed_legacy_files:
                logger.info(
                    "Removed %s legacy attachment storage entries",
                    removed_legacy_files,
                )
            if runtime_config.embedding_enabled:
                await MemoryIndexService.recover(db)
                await MemoryIndexService.reconcile(db, retry_failed=True)
            await EpisodeIndexService.recover(db)
            await EpisodeIndexService.reconcile(db, retry_failed=True)
    except Exception:
        logger.exception("Failed to clean up stale runs and interactions during startup")
    from mato_loop_backend.api.trash_retention import purge_expired

    BackgroundProcessing.start((("trash", purge_expired, 3600),))
    MemoryIndexScheduler.start()
    EpisodeIndexScheduler.start()
    CodingScheduler.start()
    TelegramScheduler.start()
    TeamsScheduler.start()
    diagnostic_retention_task = asyncio.create_task(diagnostic_retention_loop())
    try:
        try:
            async with AsyncSessionFactory() as db, db.begin():
                await record_installed_build(db)
        except (SQLAlchemyError, ValueError):
            logger.exception("Could not record the installed build for the changelog")
        yield
    finally:
        debug_mcp_mount.app = None
        await stop_auto_updates()
        diagnostic_retention_task.cancel()
        await asyncio.gather(diagnostic_retention_task, return_exceptions=True)
        try:
            await debug_mcp_stack.aclose()
        except Exception:
            logger.exception("Could not stop the optional debug MCP")
        await BackgroundProcessing.stop()
        await MemoryIndexScheduler.stop()
        await EpisodeIndexScheduler.stop()
        await CodingScheduler.stop()
        await TelegramScheduler.stop()
        await TeamsScheduler.stop()
        from mato_loop_backend.agent.session_secrets import session_secrets
        from mato_loop_backend.execution.browsers import browser_coordinator
        from mato_loop_backend.integrations.mcp.oauth import oauth_logins

        await asyncio.gather(
            terminal_coordinator.shutdown(), browser_coordinator.shutdown(), AgentRuntime.shutdown(), voice_coordinator.shutdown()
        )
        await NotificationService.stop_pushes()
        await oauth_logins.shutdown()
        session_secrets.clear_all()
        await embedding_client.close()
        await engine.dispose()


app = FastAPI(
    title=runtime_config.app_name,
    version=__version__,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=runtime_config.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(MediaRequestBodyLimitMiddleware)

app.include_router(api_router, prefix="/api")
app.mount("/api/debug", debug_mcp_mount)
register_error_handlers(app)


@app.get("/manifest.webmanifest", include_in_schema=False)
async def appearance_manifest() -> RedirectResponse:
    return RedirectResponse("/api/appearance/manifest.webmanifest", headers={"Cache-Control": "no-store"})


if runtime_config.static_dir and runtime_config.static_dir.exists():
    docs_dir = runtime_config.static_dir / "docs"
    if docs_dir.is_dir():
        _mount_documentation(app, docs_dir)

    assets_dir = runtime_config.static_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        static_root = runtime_config.static_dir.resolve()
        requested = _static_path(static_root, full_path)
        if requested is None:
            raise HTTPException(status_code=404)
        if full_path and requested.is_file():
            return FileResponse(requested, headers=_static_headers(full_path))
        index = static_root / "index.html"
        if not index.exists():
            raise HTTPException(status_code=404)
        return FileResponse(index, headers=_static_headers("index.html"))

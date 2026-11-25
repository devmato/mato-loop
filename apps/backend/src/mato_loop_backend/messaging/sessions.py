from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.assistant import ensure_personal_chat
from mato_loop_backend.automations import AutomationError
from mato_loop_backend.files.storage import SessionStorage, SessionStorageError
from mato_loop_backend.media.attachments import AttachmentInput
from mato_loop_backend.messaging.endpoints import stop_endpoint_work
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    Message,
    MessagingEndpoint,
    MessagingInbox,
    TitleGenerationStatus,
)
from mato_loop_backend.sessions.commands import parse_command, registered_commands
from mato_loop_backend.sessions.service import SessionServiceError, get_owned_session
from mato_loop_backend.sessions.workflow_commands import WORKFLOW_COMMANDS
from mato_loop_backend.settings.effective import default_agent_profile_id

logger = logging.getLogger(__name__)


class ChannelMessageError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def new_session(db: AsyncSession, endpoint: MessagingEndpoint) -> ChatSession:
    item = ChatSession(
        user_id=endpoint.user_id,
        title="Neue Session",
        title_generation_status=TitleGenerationStatus.PENDING.value,
        model_config_id=None,
        agent_profile_id=await default_agent_profile_id(db),
    )
    db.add(item)
    await db.flush()
    storage = SessionStorage()
    try:
        await asyncio.to_thread(storage.provision, item.id, user_id=item.user_id)
        return item
    except BaseException:
        await db.rollback()
        try:
            await asyncio.to_thread(storage.remove_session, item.id)
        except (OSError, SessionStorageError):
            logger.exception("Unpersisted channel session storage could not be removed")
        raise


def session_command_parts(text: str) -> tuple[str, str]:
    command, _, argument = text.strip().partition(" ")
    return command.split("@", 1)[0].casefold(), argument


def session_help_sections(provider: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    provider_name = "Telegram" if provider == "telegram" else "Teams"
    return (
        [
            ("/new", "nächste Nachricht in einer neuen Session"),
            ("/sessions", "aus den letzten Chats wählen" if provider == "teams" else "letzte Sessions anzeigen"),
            ("/use <ID>", "Session auswählen"),
            ("/current", "aktuelle Session anzeigen"),
            ("/stop", "laufende Antwort stoppen"),
            ("/unlink", f"{provider_name}-Verknüpfung lösen"),
            *((f"/{item.name}", item.description) for item in WORKFLOW_COMMANDS),
        ],
        [
            (
                "/" + (item.name.replace("-", "_") if provider == "telegram" else item.name),
                item.description,
            )
            for item in registered_commands() if item not in WORKFLOW_COMMANDS
        ],
    )


async def recent_chat_sessions(
    db: AsyncSession, endpoint: MessagingEndpoint, *, limit: int, since: datetime | None = None,
) -> list[ChatSession]:
    activity = select(Message.session_id, Message.created_at)
    if since is not None:
        activity = activity.where(Message.created_at >= since)
    activity = (
        activity.distinct(Message.session_id)
        .order_by(Message.session_id, Message.created_at.desc())
        .subquery()
    )
    return list((await db.scalars(
        select(ChatSession)
        .join(activity, activity.c.session_id == ChatSession.id)
        .where(
            ChatSession.user_id == endpoint.user_id,
            ChatSession.session_type == "chat",
            ChatSession.parent_session_id.is_(None),
        )
        .order_by(activity.c.created_at.desc(), ChatSession.id.desc())
        .limit(limit)
    )).all())


async def session_command(
    db: AsyncSession,
    inbox: MessagingInbox,
    endpoint: MessagingEndpoint,
    text: str,
    *,
    create_session: Callable[
        [AsyncSession, MessagingEndpoint], Awaitable[ChatSession]
    ] = new_session,
) -> tuple[str, bool] | None:
    command, argument = session_command_parts(text)
    if command not in {"/new", "/sessions", "/use", "/current", "/stop", "/unlink", "/help"}:
        return None
    provider = "Telegram" if endpoint.provider == "telegram" else "Teams"
    if command == "/help":
        sessions, coding = session_help_sections(endpoint.provider)
        response = (
            "\n".join(f"{name} - {description}" for name, description in sessions)
            + "\n\nCoding (zuerst eine Session mit Computer und Workspace über /use <ID> auswählen):\n"
            + "\n".join(f"{name} - {description}" for name, description in coding)
        )
    elif command == "/new":
        session = await create_session(db, endpoint)
        endpoint.current_session_id = session.id
        response = "Die nächste Nachricht startet eine neue Session."
    elif command == "/sessions":
        sessions = await recent_chat_sessions(
            db, endpoint, limit=10, since=datetime.now(UTC) - timedelta(days=7),
        )
        response = (
            "Letzte Sessions:\n"
            + "\n".join(f"{str(item.id)[:8]}  {item.title}" for item in sessions)
            if sessions
            else "Keine Session aus den letzten sieben Tagen gefunden."
        )
    elif command == "/use":
        short_id = argument.strip().casefold()
        session = None
        if 4 <= len(short_id) <= 36 and all(c in "0123456789abcdef-" for c in short_id):
            matches = list(
                (
                    await db.scalars(
                        select(ChatSession).where(
                            ChatSession.user_id == endpoint.user_id,
                            ChatSession.session_type == "chat",
                            ChatSession.parent_session_id.is_(None),
                            cast(ChatSession.id, type_=String).startswith(short_id),
                        )
                    )
                ).all()
            )
            session = matches[0] if len(matches) == 1 else None
        if session is None:
            response = "Session nicht gefunden. Nutze /sessions für gültige IDs."
        else:
            endpoint.current_session_id = session.id
            response = f"Aktuelle Session: {session.title} ({str(session.id)[:8]})"
    elif command == "/current":
        session = (
            await db.get(ChatSession, endpoint.current_session_id)
            if endpoint.current_session_id
            else None
        )
        response = (
            f"Aktuelle Session: {session.title} ({str(session.id)[:8]})"
            if session is not None and session.user_id == endpoint.user_id
            else "Keine Session ausgewählt. Die nächste Nachricht öffnet deinen persönlichen Chat."
        )
    elif command == "/stop":
        run_id = (
            (
                await db.scalar(
                    select(AgentRun.id)
                    .join(ChatSession, ChatSession.id == AgentRun.session_id)
                    .where(
                        AgentRun.session_id == endpoint.current_session_id,
                        ChatSession.user_id == endpoint.user_id,
                        ChatSession.parent_session_id.is_(None),
                        AgentRun.parent_run_id.is_(None),
                        AgentRun.status.in_(
                            ("queued", "running", "waiting_approval", "waiting_input")
                        ),
                    )
                )
            )
            if endpoint.current_session_id
            else None
        )
        response = (
            "Antwort wird gestoppt."
            if run_id and AgentRuntime.cancel(run_id)
            else "Keine laufende Antwort."
        )
    else:
        await stop_endpoint_work(db, endpoint.id, exclude_inbox_id=inbox.id)
        endpoint.paired_user_id = None
        endpoint.paired_chat_id = None
        endpoint.paired_display_name = None
        endpoint.current_session_id = None
        if endpoint.provider == "teams":
            endpoint.external_identity = f"pending:{endpoint.id}"
            endpoint.pairing_code_hash = endpoint.pairing_expires_at = None
            endpoint.channel_state = {"unlinkedBy": str(inbox.id)}
        response = f"{provider} wurde von MatoLoop getrennt."
    return response, command == "/unlink"


async def start_message(
    db: AsyncSession,
    endpoint: MessagingEndpoint,
    inbox: MessagingInbox,
    content: str,
    attachments: list[AttachmentInput],
) -> AgentRun:
    if len(content) > 200_000:
        raise ChannelMessageError("validation.tooLong")
    try:
        command = parse_command(content)
        selected = (
            (inbox.session_id or endpoint.current_session_id)
            if command is not None
            else endpoint.current_session_id
        )
        if command is not None and command not in WORKFLOW_COMMANDS and selected is None:
            raise ChannelMessageError("git.sessionRequired")
        session = await get_owned_session(db, selected, endpoint.user_id) if selected else None
        if selected is not None and (
            session is None or session.user_id != endpoint.user_id or session.session_type != "chat"
        ):
            raise ChannelMessageError("session.notFound")
        if selected is None:
            session = await ensure_personal_chat(db, endpoint.user_id)
        if command is not None:
            inbox.session_id = session.id
        return await AgentRuntime.start(
            db,
            session,
            content,
            run_id=inbox.id,
            attachments=attachments,
            source_type="endpoint",
            source_provider=endpoint.provider,
            source_endpoint_id=endpoint.id,
        )
    except (AutomationError, SessionServiceError) as exc:
        raise ChannelMessageError(exc.code) from exc

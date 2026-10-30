"""Shared command registry and dispatch for chat and messaging."""

import re

from mato_loop_backend.sessions.command_contracts import Command, CommandProvider, command_arguments
from mato_loop_backend.sessions.git_commands import GIT_COMMAND_PROVIDER

_providers: dict[str, CommandProvider] = {}
FORK_COMMAND = Command("fork", "Session kopieren", "In einer unabhängigen Kopie weiterschreiben.", "", icon="call_split")


def register_command_provider(provider: CommandProvider) -> None:
    names = [command.name for command in provider.commands]
    existing = {command.name for command in (FORK_COMMAND, *registered_commands())}
    if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider.source)
            or provider.source in _providers or len(names) != len(set(names))
            or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name) or name in existing for name in names)):
        raise ValueError("Command providers must have unique sources and valid, unique command names")
    _providers[provider.source] = provider


def registered_commands() -> tuple[Command, ...]:
    return tuple(command for provider in _providers.values() for command in provider.commands)


def parse_command(content: str) -> Command | None:
    text = content.strip()
    if not text.startswith("/"):
        return None
    name = text.split(None, 1)[0][1:].split("@", 1)[0].casefold().replace("_", "-")
    return next((command for command in (FORK_COMMAND, *registered_commands()) if command.name == name), None)


async def command_catalog(db, session) -> list[dict]:
    items = []
    if session.id is not None and session.parent_session_id is None:
        items.append({"name": FORK_COMMAND.name, "label": FORK_COMMAND.label,
                      "description": FORK_COMMAND.description, "source": "session",
                      "icon": FORK_COMMAND.icon, "disabledReason": None})
    for provider in _providers.values():
        available = await provider.availability(db, session)
        for command in provider.commands:
            if command.name in available:
                items.append({"name": command.name, "label": command.label, "description": command.description,
                              "source": provider.source, "icon": command.icon,
                              "disabledReason": available[command.name]})
    return items


async def prepare_command(db, session, command: Command, content: str | None = None) -> dict:
    from mato_loop_backend.sessions.service import SessionServiceError

    if session.parent_session_id is not None:
        raise SessionServiceError(409, "session.readOnly")
    if command == FORK_COMMAND:
        raise SessionServiceError(422, "validation.invalid", field="content")
    provider = next(provider for provider in _providers.values() if command in provider.commands)
    prepared = await provider.prepare(db, session, command, content)
    arguments = command_arguments(content)
    return {**prepared, "name": command.name, "label": command.label,
            "prompt": command.instructions + (f"\n\n{arguments}" if arguments else ""),
            "context": {**prepared.get("context", {}), "commandSource": provider.source,
                        "arguments": arguments}}


register_command_provider(GIT_COMMAND_PROVIDER)

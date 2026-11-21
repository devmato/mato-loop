from __future__ import annotations

import time
from uuid import UUID

PRESENCE_TTL_SECONDS = 15.0
RESPONSE_PUSH_GRACE_SECONDS = 120.0
_presence: dict[tuple[UUID, UUID | None], tuple[UUID | None, float]] = {}


def mark_user_present(
    user_id: UUID, *, client_id: UUID | None = None,
    session_id: UUID | None = None, active: bool = True,
) -> None:
    now = time.monotonic()
    _prune(now)
    key = (user_id, client_id)
    if active:
        _presence[key] = (session_id, now + PRESENCE_TTL_SECONDS)
    elif key in _presence and _presence[key][1] > now:
        _presence[key] = (None, now)


def user_is_present(user_id: UUID) -> bool:
    now = time.monotonic()
    _prune(now)
    return any(owner == user_id and expires > now for (owner, _client), (_, expires) in _presence.items())


def response_push_delay(user_id: UUID) -> float:
    now = time.monotonic()
    _prune(now)
    return max((expires + RESPONSE_PUSH_GRACE_SECONDS - now
                for (owner, _client), (_, expires) in _presence.items() if owner == user_id), default=0.0)


def session_is_present(user_id: UUID, session_id: UUID) -> bool:
    now = time.monotonic()
    _prune(now)
    return any(
        owner == user_id and current_session == session_id and expires > now
        for (owner, _client), (current_session, expires) in _presence.items()
    )


def _prune(now: float) -> None:
    for key, (_session_id, expires_at) in list(_presence.items()):
        if expires_at + RESPONSE_PUSH_GRACE_SECONDS <= now:
            del _presence[key]

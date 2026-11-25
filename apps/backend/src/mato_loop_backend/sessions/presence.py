from __future__ import annotations

import time
from uuid import UUID

PRESENCE_TTL_SECONDS = 15.0
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
    else:
        _presence.pop(key, None)


def user_is_present(user_id: UUID) -> bool:
    return presence_remaining(user_id) > 0


def presence_remaining(user_id: UUID) -> float:
    now = time.monotonic()
    _prune(now)
    return max((expires - now
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
        if expires_at <= now:
            del _presence[key]

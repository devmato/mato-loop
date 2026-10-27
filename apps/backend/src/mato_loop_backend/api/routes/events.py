from __future__ import annotations

import json
import time
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.change_events import change_events
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.session import AsyncSessionFactory, get_db_session
from mato_loop_backend.models import UserAccount
from mato_loop_backend.security import require_user

router = APIRouter(prefix="/events")


@router.get("")
async def stream_changes(
    request: Request,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> StreamingResponse:
    user_id = user.id
    token = request.cookies.get(get_runtime_config().session_cookie_name)
    await db.rollback()

    async def events():
        next_auth_check = time.monotonic() + 15
        async with aclosing(change_events.subscribe(user_id)) as stream:
            async for changes in stream:
                if time.monotonic() >= next_auth_check:
                    async with AsyncSessionFactory() as auth_db:
                        try:
                            current_user = await require_user(auth_db, token)
                        except ApiError:
                            return
                        if current_user.id != user_id:
                            return
                    next_auth_check = time.monotonic() + 15
                payload = [{"resource": resource, "sessionId": str(session_id) if session_id else None}
                           for resource, session_id in changes]
                kind = "changed" if changes else "keepalive"
                yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })

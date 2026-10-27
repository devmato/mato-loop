from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.changes import ChangeTrackingSession

runtime_config = get_runtime_config()

engine = create_async_engine(runtime_config.database_url, pool_pre_ping=True)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False, sync_session_class=ChangeTrackingSession)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionFactory() as session:
        yield session

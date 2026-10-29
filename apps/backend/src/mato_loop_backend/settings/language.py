from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.settings.models import UserPreferences
from mato_loop_backend.settings.store import SettingsStore


async def preferred_language(db: AsyncSession, user_id: UUID) -> str | None:
    return (await SettingsStore.load(db, UserPreferences, user_id=user_id)).preferred_language

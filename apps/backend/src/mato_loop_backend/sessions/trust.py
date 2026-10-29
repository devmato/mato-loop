from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.models import ChatSession
from mato_loop_backend.settings import GeneralSettings, SettingsStore, TrustLevel


async def effective_session_trust(db: AsyncSession, session: ChatSession) -> TrustLevel:
    general = await SettingsStore.load(db, GeneralSettings)
    if not general.model_trust_enabled:
        return TrustLevel.PRIVATE
    return TrustLevel(session.trust_level or TrustLevel.PRIVATE.value)


def session_visibility_filters(readable_by: TrustLevel) -> list:
    return [ChatSession.trust_level == TrustLevel.PUBLIC.value] if readable_by == TrustLevel.PUBLIC else []

from __future__ import annotations

from typing import Annotated, Any, Self

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.models import UserAccount
from mato_loop_backend.security import require_user
from mato_loop_backend.settings import (
    SettingsStore,
    UserProfileFact,
    UserProfilePhoneNumber,
    UserProfileSettings,
)
from mato_loop_backend.user_profile import PROFILE_FIELDS

router = APIRouter(prefix="/user-profile", dependencies=[Depends(require_user)])


class UserProfileChanges(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    full_name: UserProfileFact | None = None
    preferred_name: UserProfileFact | None = None
    form_of_address: UserProfileFact | None = None
    pronouns: UserProfileFact | None = None
    primary_language: UserProfileFact | None = None
    primary_residence: UserProfileFact | None = None
    phone_numbers: list[UserProfilePhoneNumber] | None = None

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one profile field is required")
        if any(
            (fact := getattr(self, field_name)) is not None
            and not fact.model_fields_set
            for field_name, _ in PROFILE_FIELDS
            if field_name in self.model_fields_set
        ):
            raise ValueError("a profile fact must include at least one audience")
        return self


def user_profile_payload(settings: UserProfileSettings) -> dict[str, Any]:
    return settings.model_dump(mode="json", by_alias=True)


@router.get("")
async def get_user_profile(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    settings = await SettingsStore.load(db, UserProfileSettings, user_id=user.id)
    return user_profile_payload(settings)


@router.patch("")
async def update_user_profile(
    changes: UserProfileChanges,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    current = await SettingsStore.load(
        db, UserProfileSettings, user_id=user.id, for_update=True
    )
    values = current.model_dump()
    if "phone_numbers" in changes.model_fields_set:
        values["phone_numbers"] = changes.phone_numbers or []
    for field_name, _ in PROFILE_FIELDS:
        if field_name not in changes.model_fields_set:
            continue
        update = getattr(changes, field_name)
        if update is None:
            values[field_name] = UserProfileFact().model_dump()
        else:
            values[field_name].update(
                update.model_dump(include=update.model_fields_set)
            )
    settings = UserProfileSettings.model_validate(values)
    await SettingsStore.save(db, settings, user_id=user.id)
    await db.commit()
    return user_profile_payload(settings)

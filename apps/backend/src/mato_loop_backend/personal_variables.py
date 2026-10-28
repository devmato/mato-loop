from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.models import Credential, UserAccount
from mato_loop_backend.security import read_credential, store_credential

PERSONAL_VARIABLE_KIND = "personal_variable"
PERSONAL_VARIABLE_LIMIT = 100
PERSONAL_VARIABLE_VALUE_MAX_BYTES = 64 * 1024
PERSONAL_VARIABLE_DESCRIPTION_MAX_LENGTH = 500
PERSONAL_VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,119}$")


@dataclass(frozen=True, slots=True)
class PersonalVariableError(RuntimeError):
    code: str
    field: str | None = None


def validate_personal_variable_value(value: str) -> str:
    if "\0" in value or len(value.encode()) > PERSONAL_VARIABLE_VALUE_MAX_BYTES:
        raise PersonalVariableError("personalVariable.valueInvalid", "value")
    return value


def normalize_personal_variable_description(value: str | None) -> str | None:
    if value is None or not (value := value.strip()):
        return None
    if "\0" in value or len(value) > PERSONAL_VARIABLE_DESCRIPTION_MAX_LENGTH:
        raise PersonalVariableError("personalVariable.descriptionInvalid", "description")
    return value


async def _lock_owner(db: AsyncSession, owner_id: UUID) -> None:
    await db.scalar(
        select(UserAccount.id).where(UserAccount.id == owner_id).with_for_update()
    )


async def list_personal_variables(
    db: AsyncSession, owner_id: UUID
) -> list[Credential]:
    return list(
        (
            await db.scalars(
                select(Credential)
                .where(
                    Credential.user_id == owner_id,
                    Credential.kind == PERSONAL_VARIABLE_KIND,
                )
                .order_by(func.lower(Credential.label), Credential.id)
            )
        ).all()
    )


async def get_personal_variable(
    db: AsyncSession,
    owner_id: UUID,
    variable_id: UUID,
    *,
    for_update: bool = False,
    populate_existing: bool = False,
) -> Credential | None:
    statement = select(Credential).where(
        Credential.id == variable_id,
        Credential.user_id == owner_id,
        Credential.kind == PERSONAL_VARIABLE_KIND,
    )
    if for_update:
        statement = statement.with_for_update()
    if populate_existing or for_update:
        statement = statement.execution_options(populate_existing=True)
    return await db.scalar(statement)


async def get_personal_variable_by_name(
    db: AsyncSession, owner_id: UUID, name: str
) -> Credential | None:
    return await db.scalar(
        select(Credential).where(
            Credential.user_id == owner_id,
            Credential.kind == PERSONAL_VARIABLE_KIND,
            func.lower(Credential.label) == name.lower(),
        )
    )


def personal_variable_value(variable: Credential) -> str:
    value = read_credential(variable).get("value")
    if not isinstance(value, str):
        raise PersonalVariableError("personalVariable.valueInvalid")
    return value


def personal_variable_payload(variable: Credential) -> dict[str, object]:
    secret = variable.metadata_json.get("secret") is True
    description = variable.metadata_json.get("description")
    result: dict[str, object] = {
        "id": str(variable.id),
        "name": variable.label,
        "description": description if isinstance(description, str) else None,
        "secret": secret,
    }
    if not secret:
        result["value"] = personal_variable_value(variable)
    return result


async def create_personal_variable(
    db: AsyncSession,
    owner_id: UUID,
    *,
    name: str,
    value: str,
    secret: bool,
    description: str | None = None,
) -> Credential:
    validate_personal_variable_value(value)
    description = normalize_personal_variable_description(description)
    await _lock_owner(db, owner_id)
    if await get_personal_variable_by_name(db, owner_id, name) is not None:
        raise PersonalVariableError("personalVariable.duplicate", "name")
    count = await db.scalar(
        select(func.count()).select_from(Credential).where(
            Credential.user_id == owner_id,
            Credential.kind == PERSONAL_VARIABLE_KIND,
        )
    )
    if (count or 0) >= PERSONAL_VARIABLE_LIMIT:
        raise PersonalVariableError("personalVariable.limitReached")
    return await store_credential(
        db,
        owner_id=owner_id,
        kind=PERSONAL_VARIABLE_KIND,
        label=name,
        payload={"value": value},
        metadata={"secret": secret, **({"description": description} if description else {})},
    )


async def update_personal_variable(
    db: AsyncSession,
    owner_id: UUID,
    variable_id: UUID,
    *,
    name: str,
    value: str | None,
    secret: bool,
    description: str | None = None,
) -> Credential:
    if value is not None:
        validate_personal_variable_value(value)
    description = normalize_personal_variable_description(description)
    await _lock_owner(db, owner_id)
    variable = await get_personal_variable(db, owner_id, variable_id, for_update=True)
    if variable is None:
        raise PersonalVariableError("personalVariable.notFound")
    duplicate = await get_personal_variable_by_name(db, owner_id, name)
    if duplicate is not None and duplicate.id != variable.id:
        raise PersonalVariableError("personalVariable.duplicate", "name")
    if value is None:
        if variable.metadata_json.get("secret") is True and not secret:
            raise PersonalVariableError("personalVariable.valueRequired", "value")
        value = personal_variable_value(variable)
    return await store_credential(
        db,
        owner_id=owner_id,
        kind=PERSONAL_VARIABLE_KIND,
        label=name,
        payload={"value": value},
        metadata={"secret": secret, **({"description": description} if description else {})},
        credential=variable,
    )

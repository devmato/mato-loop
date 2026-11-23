from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.models.account import UserAccount
from mato_loop_backend.settings import (
    DEFAULT_SKILLS,
    InstanceSkillsSettings,
    SettingsStore,
    SkillDefinition,
    SkillsSettings,
)
from mato_loop_backend.settings.effective import personal_skills_allowed
from mato_loop_backend.settings.models import SkillReference
from mato_loop_backend.skill_packages import (
    SkillError,
    SkillFile,
    SkillPackage,
    bundled_file_hashes,
    parse_skill_md,
    skill_md,
)
from mato_loop_backend.skill_storage import SkillStorage

SkillScope = Literal["instance", "personal"]


@dataclass(slots=True)
class RunSkillState:
    catalog: dict[UUID, SkillDefinition] = field(default_factory=dict)
    active: dict[UUID, SkillDefinition] = field(default_factory=dict)

    @classmethod
    def from_skills(cls, skills: list[SkillDefinition]) -> RunSkillState:
        return cls({skill.id: skill.model_copy(deep=True) for skill in skills if skill.enabled})


def default_package(skill_id: UUID) -> SkillPackage:
    skill = DEFAULT_SKILLS[skill_id]
    return SkillPackage({"SKILL.md": SkillFile(skill_md(skill.name, skill.description, skill.instructions))})


def definition(reference: SkillReference, owner_id: UUID | None, package: SkillPackage) -> SkillDefinition:
    metadata, instructions = parse_skill_md(package.files["SKILL.md"].content)
    return SkillDefinition(
        id=reference.id, name=metadata["name"], description=metadata["description"],
        instructions=instructions, enabled=reference.enabled, owner_id=owner_id,
        document=package.files["SKILL.md"].content.decode("utf-8-sig"),
        revision=package.revision, resource_paths=sorted(package.files),
    )


class SkillService:
    @staticmethod
    async def require_write(db: AsyncSession, user_id: UUID, scope: SkillScope) -> None:
        if scope == "instance":
            user = await db.scalar(select(UserAccount).where(UserAccount.id == user_id).with_for_update().execution_options(populate_existing=True))
            if user is None or not user.is_active or not user.is_admin:
                raise SkillError("auth.adminRequired")
        elif not await personal_skills_allowed(db, user_id, for_update=True):
            raise SkillError("skill.personalDisabled")

    @staticmethod
    async def _load_scoped(db: AsyncSession, user_id: UUID, scope: SkillScope, *, for_update: bool = False) -> tuple[SkillsSettings | InstanceSkillsSettings, UUID | None]:
        if for_update:
            await SkillService.require_write(db, user_id, scope)
        owner_id = user_id if scope == "personal" else None
        settings = await SettingsStore.load(db, SkillsSettings if owner_id else InstanceSkillsSettings, user_id=owner_id, for_update=for_update)
        return settings, owner_id

    @staticmethod
    async def _definition(reference: SkillReference, owner_id: UUID | None) -> SkillDefinition:
        package = await asyncio.to_thread(SkillStorage().read, owner_id, reference.id, reference.revision, reference.name)
        return definition(reference, owner_id, package)

    @staticmethod
    async def list_scoped(db: AsyncSession, user_id: UUID, scope: SkillScope) -> list[SkillDefinition]:
        settings, owner_id = await SkillService._load_scoped(db, user_id, scope)
        skills = [await SkillService._definition(item, owner_id) for item in settings.skills]
        if scope == "personal":
            existing = {item.id for item in skills}
            instance = await SettingsStore.load(db, InstanceSkillsSettings)
            skills = [await SkillService._definition(item, None) for item in instance.skills if item.id in DEFAULT_SKILLS and item.id not in existing] + skills
        return skills

    @staticmethod
    async def effective_references(db: AsyncSession, user_id: UUID) -> tuple[list[tuple[SkillReference, UUID | None]], bool]:
        instance = await SettingsStore.load_optional(db, InstanceSkillsSettings) or InstanceSkillsSettings()
        allowed = await personal_skills_allowed(db, user_id)
        personal = await SettingsStore.load(db, SkillsSettings, user_id=user_id) if allowed else SkillsSettings()
        overrides = {skill.id for skill in personal.skills}
        references = [(item, None) for item in instance.skills if item.id not in overrides] + [(item, user_id) for item in personal.skills]
        return [(item, owner_id) for item, owner_id in references if item.enabled], allowed

    @staticmethod
    async def list(db: AsyncSession, user_id: UUID) -> list[SkillDefinition]:
        references, allowed = await SkillService.effective_references(db, user_id)
        skills = [await SkillService._definition(item, owner_id) for item, owner_id in references]
        user = await db.get(UserAccount, user_id)
        for skill in skills:
            skill.editable = bool(isinstance(user, UserAccount) and user.is_admin) if skill.owner_id is None else allowed
            if allowed and skill.id in DEFAULT_SKILLS:
                skill.editable = True
        return skills

    @staticmethod
    async def package(skill: SkillDefinition) -> SkillPackage:
        return await asyncio.to_thread(SkillStorage().read, skill.owner_id, skill.id, skill.revision, skill.name)

    @staticmethod
    async def get(db: AsyncSession, user_id: UUID, skill_id: UUID, scope: SkillScope) -> SkillDefinition:
        skill = next((item for item in await SkillService.list_scoped(db, user_id, scope) if item.id == skill_id), None)
        if skill is None:
            raise SkillError("skill.notFound")
        return skill

    @staticmethod
    async def save_package(
        db: AsyncSession, user_id: UUID, skill_id: UUID, package: SkillPackage, *,
        scope: SkillScope = "personal", create: bool = False, expected_revision: str | None = None,
    ) -> SkillDefinition:
        settings, owner_id = await SkillService._load_scoped(db, user_id, scope, for_update=True)
        current = next((item for item in settings.skills if item.id == skill_id), None)
        if current is None and not create and scope == "personal" and skill_id in DEFAULT_SKILLS:
            current = await SkillService._default_base(db, skill_id)
        if create and current is not None:
            raise SkillError("skill.duplicate")
        if not create and current is None:
            raise SkillError("skill.notFound")
        if current is not None and expected_revision != current.revision:
            raise SkillError("skill.changed")
        if any(item.id != skill_id and item.name == package.name for item in settings.skills):
            raise SkillError("skill.duplicate")
        if create and sum(item.id not in DEFAULT_SKILLS for item in settings.skills) >= 20:
            raise SkillError("skill.limitReached")
        revision = await asyncio.to_thread(SkillStorage().publish, owner_id, skill_id, package)
        replacement = SkillReference(
            id=skill_id, name=package.name, revision=revision, enabled=current.enabled if current else True,
            bundled_files=(bundled_file_hashes(package) if skill_id in DEFAULT_SKILLS and package == default_package(skill_id)
                           else current.bundled_files if current else {}),
        )
        settings.skills = [replacement if item.id == skill_id else item for item in settings.skills]
        if not any(item.id == skill_id for item in settings.skills):
            settings.skills.append(replacement)
        await SettingsStore.save(db, settings, user_id=owner_id)
        return definition(replacement, owner_id, package)

    @staticmethod
    async def delete(db: AsyncSession, user_id: UUID, skill_id: UUID, scope: SkillScope = "personal") -> None:
        if skill_id in DEFAULT_SKILLS:
            raise SkillError("skill.defaultProtected")
        settings, owner_id = await SkillService._load_scoped(db, user_id, scope, for_update=True)
        if not any(item.id == skill_id for item in settings.skills):
            raise SkillError("skill.notFound")
        settings.skills = [item for item in settings.skills if item.id != skill_id]
        await SettingsStore.save(db, settings, user_id=owner_id)

    @staticmethod
    async def set_enabled(db: AsyncSession, user_id: UUID, skill_id: UUID, enabled: bool, scope: SkillScope = "personal") -> SkillDefinition:
        settings, owner_id = await SkillService._load_scoped(db, user_id, scope, for_update=True)
        current = next((item for item in settings.skills if item.id == skill_id), None)
        if current is None and scope == "personal" and skill_id in DEFAULT_SKILLS:
            current = await SkillService._default_base(db, skill_id)
            package = await asyncio.to_thread(SkillStorage().read, None, current.id, current.revision, current.name)
            await asyncio.to_thread(SkillStorage().publish, owner_id, current.id, package)
            settings.skills.append(current)
        if current is None:
            raise SkillError("skill.notFound")
        current.enabled = enabled
        await SettingsStore.save(db, settings, user_id=owner_id)
        return await SkillService._definition(current, owner_id)

    @staticmethod
    async def reset(db: AsyncSession, user_id: UUID, skill_id: UUID, scope: SkillScope = "personal") -> SkillDefinition:
        if skill_id not in DEFAULT_SKILLS:
            raise SkillError("skill.notFound")
        current = await SkillService.get(db, user_id, skill_id, scope)
        return await SkillService.save_package(db, user_id, skill_id, default_package(skill_id), scope=scope, expected_revision=current.revision)

    @staticmethod
    async def _default_base(db: AsyncSession, skill_id: UUID) -> SkillReference:
        instance = await SettingsStore.load(db, InstanceSkillsSettings)
        return next(item.model_copy(deep=True) for item in instance.skills if item.id == skill_id)

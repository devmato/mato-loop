from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date as Date
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    ToolContribution,
    remote_error_params,
)
from mato_loop_backend.integrations.execution import (
    bound_integration_ids as _bound_ids,
)
from mato_loop_backend.integrations.execution import (
    operation_key as _operation_key,
)
from mato_loop_backend.integrations.execution import selected_integration_binding
from mato_loop_backend.integrations.mealie.client import (
    MealieClient,
    MealieEntryType,
    MealieError,
    MealieMealPlanEntry,
    MealieRecipe,
    MealieRecipeIngredient,
    MealieRecipeInstruction,
    MealieRecipeSummary,
)
from mato_loop_backend.media.attachments import MAX_IMAGE_ATTACHMENT_BYTES
from mato_loop_backend.media.files import read_media_file
from mato_loop_backend.models import Credential
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.security import CredentialCipher, load_credential, read_credential
from mato_loop_backend.settings import IntegrationInstance
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.argument_errors import invalid_argument
from mato_loop_backend.tools.arguments import FileReference
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.snapshots import ToolSnapshotPolicy

MEALIE_CREDENTIAL_KIND = "mealie.api_token"

_ACCOUNTS_READ = ToolAction(
    "mealie.accounts.read", "Mealie-Konten auflisten", category="read"
)
_RECIPES_READ = ToolAction(
    "recipes.read", "Rezepte lesen", category="read"
)
_RECIPES_WRITE = ToolAction(
    "recipes.write", "Rezepte ändern", category="write", mutates=True
)
_RECIPES_DELETE = ToolAction(
    "recipes.delete", "Rezepte löschen", category="delete", mutates=True
)
_MEAL_PLAN_READ = ToolAction(
    "meal_plan.read", "Essensplan lesen", category="read"
)
_MEAL_PLAN_WRITE = ToolAction(
    "meal_plan.write", "Essensplan ändern", category="write", mutates=True
)
_MEAL_PLAN_DELETE = ToolAction(
    "meal_plan.delete",
    "Essensplan-Einträge löschen",
    category="delete",
    mutates=True,
)


class MealieConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: HttpUrl

    @model_validator(mode="after")
    def validate_url(self) -> MealieConfig:
        if (
            self.base_url.username is not None
            or self.base_url.password is not None
            or self.base_url.query is not None
            or self.base_url.fragment is not None
        ):
            raise ValueError("Mealie URL must not contain credentials, query, or fragment")
        return self


class _RecipeToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    integration_id: UUID
    binding_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    recipe_id: UUID
    slug: str = Field(max_length=1_000)
    name: str = Field(max_length=500)
    snapshot_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class _MealPlanToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    integration_id: UUID
    binding_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    meal_plan_id: int = Field(ge=1)
    date: Date
    entry_type: MealieEntryType
    title: str = Field(max_length=500)
    text: str = Field(max_length=5_000)
    recipe_id: UUID | None
    group_id: UUID
    user_id: UUID
    household_id: UUID
    recipe_name: str | None = Field(max_length=500)


def _validate_encrypted_token(value: str, model: type[BaseModel], label: str) -> str:
    try:
        model.model_validate(CredentialCipher().decrypt(value.encode("ascii")))
    except Exception as exc:
        raise invalid_argument("Use a token from a fresh Mealie read or search.") from exc
    return value


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MealieListAccountsArguments(_Arguments):
    pass


class MealieIntegrationArguments(_Arguments):
    integration_id: UUID


class RecipesSearchArguments(MealieIntegrationArguments):
    query: str | None = Field(default=None, max_length=400)
    limit: int = Field(default=20, ge=1, le=50)
    page: int = Field(default=1, ge=1)
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]] = Field(default_factory=list, max_length=20, description="Exact tag names; all must match.")
    categories: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]] = Field(default_factory=list, max_length=20, description="Exact category names; all must match.")
    ingredients: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]] = Field(default_factory=list, max_length=20, description="Exact structured food names; all must match. Use query for unparsed ingredient text.")


class RecipeGetArguments(MealieIntegrationArguments):
    recipe_token: str = Field(min_length=1, max_length=50_000)

    @field_validator("recipe_token")
    @classmethod
    def validate_recipe_token(cls, value: str) -> str:
        return _validate_encrypted_token(value, _RecipeToken, "recipe")


class RecipeIngredientInput(_Arguments):
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=500)
    food: str | None = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=5_000)
    title: str | None = Field(default=None, max_length=500)
    original_text: str | None = Field(default=None, max_length=5_000)

    @model_validator(mode="after")
    def require_content(self) -> RecipeIngredientInput:
        if not any((value or "").strip() for value in (self.food, self.note, self.title, self.original_text)):
            raise PydanticCustomError("tool_argument_invalid", "ingredient content is required", {"reason": "missing", "hint": "Provide at least one of food, note, title, or original_text."})
        return self


class RecipeInstructionInput(_Arguments):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]
    title: str = Field(default="", max_length=500)
    summary: str = Field(default="", max_length=5_000)


class RecipeFields(_Arguments):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None
    description: str | None = Field(default=None, max_length=5_000)
    servings: float | None = Field(default=None, ge=0)
    yield_quantity: float | None = Field(default=None, ge=0)
    yield_text: str | None = Field(default=None, max_length=500)
    total_time: str | None = Field(default=None, max_length=500)
    prep_time: str | None = Field(default=None, max_length=500)
    cook_time: str | None = Field(default=None, max_length=500)
    perform_time: str | None = Field(default=None, max_length=500)
    ingredients: list[RecipeIngredientInput] | None = Field(default=None, max_length=200, description="Complete replacement ingredient list; omit to preserve it.")
    instructions: list[RecipeInstructionInput] | None = Field(default=None, max_length=200, description="Complete replacement instruction list; omit to preserve it.")

    @model_validator(mode="after")
    def reject_null_fields(self) -> RecipeFields:
        for field in self.model_fields_set & RecipeFields.model_fields.keys():
            if getattr(self, field) is None:
                raise PydanticCustomError("tool_argument_invalid", "recipe field cannot be null", {"field": field, "hint": "Omit the field to preserve its current value; do not send null."})
        return self


class RecipeCreateArguments(RecipeFields, MealieIntegrationArguments):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class RecipeUpdateArguments(RecipeFields, RecipeGetArguments):
    @model_validator(mode="after")
    def require_patch(self) -> RecipeUpdateArguments:
        if not self.model_fields_set & RecipeFields.model_fields.keys():
            raise PydanticCustomError("tool_argument_invalid", "recipe changes are required", {"reason": "missing", "hint": "Provide at least one recipe field to update."})
        return self


class RecipeDeleteArguments(RecipeGetArguments):
    pass


class RecipeSetImageArguments(RecipeGetArguments):
    image: FileReference


class RecipeImportArguments(MealieIntegrationArguments):
    url: HttpUrl = Field(max_length=2_000)

    @field_validator("url")
    @classmethod
    def validate_source_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None or value.fragment is not None:
            raise invalid_argument("Use a recipe URL without credentials or a fragment.", reason="invalid_format")
        return value


class MealPlanGetArguments(MealieIntegrationArguments):
    start_date: Date
    end_date: Date
    limit: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def validate_range(self) -> MealPlanGetArguments:
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 365:
            raise PydanticCustomError("tool_argument_invalid", "invalid date range", {"field": "end_date", "hint": "Use end_date on or after start_date, at most 365 days later."})
        return self


class MealPlanAddArguments(MealieIntegrationArguments):
    date: Date
    entry_type: MealieEntryType = "dinner"
    title: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=5_000)
    recipe_token: str | None = Field(default=None, min_length=1, max_length=50_000)

    @field_validator("recipe_token")
    @classmethod
    def validate_recipe_token(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_encrypted_token(value, _RecipeToken, "recipe")
        return value

    @model_validator(mode="after")
    def require_content(self) -> MealPlanAddArguments:
        if self.recipe_token is None and not self.title.strip():
            raise PydanticCustomError("tool_argument_invalid", "meal plan content is required", {"reason": "missing", "hint": "Provide recipe_token or a non-blank title."})
        return self


class MealPlanTokenArguments(MealieIntegrationArguments):
    meal_plan_token: str = Field(min_length=1, max_length=50_000)

    @field_validator("meal_plan_token")
    @classmethod
    def validate_meal_plan_token(cls, value: str) -> str:
        return _validate_encrypted_token(value, _MealPlanToken, "meal plan")


class MealPlanUpdateArguments(MealPlanTokenArguments):
    date: Date | None = None
    entry_type: MealieEntryType | None = None
    title: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=5_000)
    recipe_token: str | None = Field(default=None, min_length=1, max_length=50_000)

    @field_validator("recipe_token")
    @classmethod
    def validate_recipe_token(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_encrypted_token(value, _RecipeToken, "recipe")
        return value

    @model_validator(mode="after")
    def require_patch(self) -> MealPlanUpdateArguments:
        fields = self.model_fields_set & {
            "date",
            "entry_type",
            "title",
            "text",
            "recipe_token",
        }
        if not fields:
            raise PydanticCustomError("tool_argument_invalid", "meal plan changes are required", {"reason": "missing", "hint": "Provide at least one meal plan field to update."})
        for field in fields - {"recipe_token"}:
            if getattr(self, field) is None:
                raise PydanticCustomError("tool_argument_invalid", "meal plan field cannot be null", {"field": field, "hint": "Omit the field instead of setting it to null."})
        return self


class MealPlanDeleteArguments(MealPlanTokenArguments):
    pass


_MEALIE_ERROR_CODES = {
    "mealie.authenticationFailed": "integration.authenticationFailed",
    "mealie.invalidResponse": "integration.invalidResponse",
    "mealie.invalidRequest": "integration.requestFailed",
    "mealie.invalidUrl": "integration.invalidConfiguration",
    "mealie.malformedResponse": "integration.invalidResponse",
    "mealie.notFound": "integration.remoteNotFound",
    "mealie.outcomeUnknown": "integration.outcomeUnknown",
    "mealie.permissionDenied": "integration.permissionDenied",
    "mealie.rateLimited": "integration.rateLimited",
    "mealie.redirectRejected": "integration.unavailable",
    "mealie.responseTooLarge": "integration.responseTooLarge",
    "mealie.unavailable": "integration.unavailable",
    "mealie.unexpectedStatus": "integration.unavailable",
}


def mealie_error_code(error: MealieError) -> str:
    return _MEALIE_ERROR_CODES.get(error.code, "integration.unavailable")


def _binding(context: Any, integration_id: UUID) -> tuple[IntegrationInstance, str]:
    return selected_integration_binding(
        context, integration_id, integration_type="recipes", adapter_type="mealie",
    )


def _credential_payload(record: Credential | None) -> dict[str, Any]:
    if record is None or record.kind != MEALIE_CREDENTIAL_KIND:
        raise MealieError("mealie.authenticationFailed")
    try:
        payload = read_credential(record)
    except Exception as exc:
        raise MealieError("mealie.authenticationFailed") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"token"}
        or not isinstance(payload["token"], str)
        or not payload["token"]
    ):
        raise MealieError("mealie.authenticationFailed")
    return payload


@asynccontextmanager
async def _selected_client(
    integration_id: UUID, context: Any
) -> AsyncIterator[tuple[MealieClient, str]]:
    instance, revision = _binding(context, integration_id)
    if instance.credential_id is None:
        raise MealieError("mealie.authenticationFailed")
    record = await load_credential(
        context.db, instance.credential_id, owner_id=instance.owner_id
    )
    token = _credential_payload(record)["token"]
    config = MealieConfig.model_validate(instance.config)
    async with MealieClient(str(config.base_url), token) as client:
        yield client, revision


def _fingerprint(revision: str) -> str:
    return hashlib.sha256(revision.encode()).hexdigest()


def _encrypt(payload: BaseModel) -> str:
    return (
        CredentialCipher()
        .encrypt(payload.model_dump(mode="json"))
        .decode("ascii")
    )


def _recipe_token(
    recipe: MealieRecipeSummary, integration_id: UUID, revision: str
) -> str:
    if recipe.id is None:
        raise MealieError("mealie.malformedResponse")
    return _encrypt(
        _RecipeToken(
            version=1,
            integration_id=integration_id,
            binding_revision=_fingerprint(revision),
            recipe_id=UUID(recipe.id),
            slug=_bounded(recipe.slug, 1_000) or "",
            name=_bounded(recipe.name, 500) or "",
            snapshot_hash=recipe.snapshot_hash if isinstance(recipe, MealieRecipe) else None,
        )
    )


def _meal_plan_token(
    entry: MealieMealPlanEntry, integration_id: UUID, revision: str
) -> str:
    if len(entry.title) > 500 or len(entry.text) > 5_000:
        raise MealieError("mealie.malformedResponse")
    return _encrypt(
        _MealPlanToken(
            version=1,
            integration_id=integration_id,
            binding_revision=_fingerprint(revision),
            meal_plan_id=entry.id,
            date=entry.date,
            entry_type=entry.entry_type,
            title=entry.title,
            text=entry.text,
            recipe_id=UUID(entry.recipe_id) if entry.recipe_id is not None else None,
            group_id=UUID(entry.group_id),
            user_id=UUID(entry.user_id),
            household_id=UUID(entry.household_id),
            recipe_name=(
                _bounded(entry.recipe.name, 500) if entry.recipe is not None else None
            ),
        )
    )


def _decode_token(
    model: type[BaseModel], value: str, integration_id: UUID, revision: str
) -> Any:
    try:
        payload = model.model_validate(
            CredentialCipher().decrypt(value.encode("ascii"))
        )
    except Exception as exc:
        raise ValueError("invalid token") from exc
    if (
        payload.integration_id != integration_id
        or payload.binding_revision != _fingerprint(revision)
    ):
        raise ValueError("token binding mismatch")
    return payload


def _bounded(value: str | None, limit: int) -> str | None:
    return value[:limit] if value is not None else None


def _bounded_names(values: tuple[str, ...]) -> list[str]:
    return [_bounded(value, 500) or "" for value in values[:100]]


def _recipe_summary(
    recipe: MealieRecipeSummary, integration_id: UUID, revision: str
) -> dict[str, Any]:
    return {
        "recipeToken": _recipe_token(recipe, integration_id, revision),
        "integrationId": str(integration_id),
        "name": _bounded(recipe.name, 500),
        "description": _bounded(recipe.description, 5_000),
        "servings": recipe.recipe_servings,
        "yieldQuantity": recipe.recipe_yield_quantity,
        "yield": _bounded(recipe.recipe_yield, 500),
        "totalTime": _bounded(recipe.total_time, 500),
        "prepTime": _bounded(recipe.prep_time, 500),
        "cookTime": _bounded(recipe.cook_time, 500),
        "performTime": _bounded(recipe.perform_time, 500),
        "categories": _bounded_names(recipe.categories),
        "tags": _bounded_names(recipe.tags),
    }


def _ingredient_payload(ingredient: MealieRecipeIngredient) -> dict[str, Any]:
    return {
        "quantity": ingredient.quantity,
        "unit": _bounded(ingredient.unit, 500),
        "food": _bounded(ingredient.food, 500),
        "note": _bounded(ingredient.note, 5_000),
        "display": _bounded(ingredient.display, 5_000),
        "title": _bounded(ingredient.title, 500),
        "originalText": _bounded(ingredient.original_text, 5_000),
    }


def _instruction_payload(instruction: MealieRecipeInstruction) -> dict[str, Any]:
    return {
        "title": _bounded(instruction.title, 500),
        "summary": _bounded(instruction.summary, 5_000),
        "text": _bounded(instruction.text, 10_000),
    }


def _recipe_payload(
    recipe: MealieRecipe, integration_id: UUID, revision: str
) -> dict[str, Any]:
    if (
        len(recipe.ingredients) > 200
        or len(recipe.instructions) > 200
        or any(
            len(value) > limit
            for value, limit in (
                (recipe.description or "", 5_000),
                *(
                    (value or "", limit)
                    for ingredient in recipe.ingredients
                    for value, limit in (
                        (ingredient.unit, 500),
                        (ingredient.food, 500),
                        (ingredient.note, 5_000),
                        (ingredient.display, 5_000),
                        (ingredient.title, 500),
                        (ingredient.original_text, 5_000),
                    )
                ),
                *(
                    (value or "", limit)
                    for instruction in recipe.instructions
                    for value, limit in (
                        (instruction.title, 500),
                        (instruction.summary, 5_000),
                        (instruction.text, 10_000),
                    )
                ),
            )
        )
    ):
        raise MealieError("mealie.responseTooLarge")
    return {
        **_recipe_summary(recipe, integration_id, revision),
        "image": recipe.image,
        "ingredients": [
            _ingredient_payload(item) for item in recipe.ingredients[:200]
        ],
        "instructions": [
            _instruction_payload(item) for item in recipe.instructions[:200]
        ],
    }


def _meal_plan_payload(
    entry: MealieMealPlanEntry, integration_id: UUID, revision: str
) -> dict[str, Any]:
    if entry.recipe is not None and entry.recipe.id != entry.recipe_id:
        raise MealieError("mealie.malformedResponse")
    return {
        "mealPlanToken": _meal_plan_token(entry, integration_id, revision),
        "integrationId": str(integration_id),
        "date": entry.date.isoformat(),
        "entryType": entry.entry_type,
        "title": _bounded(entry.title, 500),
        "text": _bounded(entry.text, 5_000),
        "recipe": (
            _recipe_summary(entry.recipe, integration_id, revision)
            if entry.recipe is not None
            else None
        ),
    }


def _result(value: dict[str, Any]) -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.json({**value, "contentIsUntrusted": True})


def _error(error: MealieError) -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.error(mealie_error_code(error), params=remote_error_params(error.status_code))


def _invalid(field: str) -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.error("integration.staleReference", field=field)


def _remote_changed(field: str = "meal_plan_token") -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.error("integration.remoteChanged", field=field)


async def mealie_list_accounts(
    _arguments: MealieListAccountsArguments, context: Any
) -> Any:
    bound_ids = _bound_ids(context)
    if not bound_ids:
        from mato_loop_backend.tools.contracts import ToolResult

        return ToolResult.error("tool.unavailable")
    accounts = [
        {"integrationId": str(instance.id), "name": instance.name[:160]}
        for instance in context.integration_settings.instances
        if str(instance.id) in bound_ids
        and instance.enabled
        and instance.integration_type == "recipes"
        and instance.adapter_type == "mealie"
    ]
    return _result({"accounts": accounts})


async def recipes_search(arguments: RecipesSearchArguments, context: Any) -> Any:
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            revision,
        ):
            result = await client.search_recipes(
                arguments.query, arguments.limit, page=arguments.page,
                tags=tuple(arguments.tags), categories=tuple(arguments.categories),
                ingredients=tuple(arguments.ingredients),
            )
        return _result(
            {
                "recipes": [
                    _recipe_summary(recipe, arguments.integration_id, revision)
                    for recipe in result.recipes[: arguments.limit]
                ],
                "page": result.page,
                "total": result.total,
                "totalPages": result.total_pages,
                "nextPage": result.page + 1 if result.page < result.total_pages else None,
            }
        )
    except ValueError:
        return _invalid("integration_id")
    except MealieError as exc:
        return _error(exc)


async def recipe_get(arguments: RecipeGetArguments, context: Any) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _RecipeToken,
            arguments.recipe_token,
            arguments.integration_id,
            revision,
        )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            recipe = await client.get_recipe(str(token.recipe_id))
        if recipe.id != str(token.recipe_id):
            raise MealieError("mealie.malformedResponse")
        return _result(
            {"recipe": _recipe_payload(recipe, arguments.integration_id, revision)}
        )
    except ValueError:
        return _invalid("recipe_token")
    except MealieError as exc:
        return _error(exc)


def _recipe_changes(arguments: RecipeFields) -> dict[str, Any]:
    fields = {
        "name": "name", "description": "description", "servings": "recipeServings",
        "yield_quantity": "recipeYieldQuantity", "yield_text": "recipeYield",
        "total_time": "totalTime", "prep_time": "prepTime", "cook_time": "cookTime",
        "perform_time": "performTime",
    }
    changes = {
        remote: getattr(arguments, local)
        for local, remote in fields.items() if local in arguments.model_fields_set
    }
    if "ingredients" in arguments.model_fields_set:
        changes["recipeIngredient"] = [
            {
                "quantity": ingredient.quantity,
                "unit": {"name": ingredient.unit} if ingredient.unit else None,
                "food": {"name": ingredient.food} if ingredient.food else None,
                "note": ingredient.note, "title": ingredient.title,
                "originalText": ingredient.original_text,
            }
            for ingredient in arguments.ingredients or []
        ]
    if "instructions" in arguments.model_fields_set:
        changes["recipeInstructions"] = [step.model_dump() for step in arguments.instructions or []]
    return changes


def _recipe_write_result(recipe: MealieRecipe, integration_id: UUID, revision: str) -> Any:
    try:
        return _result({"recipe": _recipe_payload(recipe, integration_id, revision)})
    except (MealieError, ValueError):
        return _error(MealieError("mealie.outcomeUnknown"))


async def recipe_create(arguments: RecipeCreateArguments, context: Any) -> Any:
    try:
        changes = _recipe_changes(arguments)
        changes.pop("name")
        async with _selected_client(arguments.integration_id, context) as (client, revision):
            recipe = await client.create_recipe(arguments.name, changes)
        return _recipe_write_result(recipe, arguments.integration_id, revision)
    except ValueError:
        return _invalid("integration_id")
    except MealieError as exc:
        return _error(exc)


async def recipe_import_url(arguments: RecipeImportArguments, context: Any) -> Any:
    try:
        async with _selected_client(arguments.integration_id, context) as (client, revision):
            recipe = await client.import_recipe(str(arguments.url))
        return _recipe_write_result(recipe, arguments.integration_id, revision)
    except ValueError:
        return _invalid("integration_id")
    except MealieError as exc:
        return _error(exc)


async def recipe_update(arguments: RecipeUpdateArguments, context: Any) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(_RecipeToken, arguments.recipe_token, arguments.integration_id, revision)
        if token.snapshot_hash is None:
            return _invalid("recipe_token")
        async with _selected_client(arguments.integration_id, context) as (client, _revision):
            current = await client.get_recipe(str(token.recipe_id))
            if current.id != str(token.recipe_id) or current.snapshot_hash != token.snapshot_hash:
                return _remote_changed("recipe_token")
            recipe = await client.update_recipe(str(token.recipe_id), _recipe_changes(arguments))
        if recipe.id != str(token.recipe_id):
            return _error(MealieError("mealie.outcomeUnknown"))
        return _recipe_write_result(recipe, arguments.integration_id, revision)
    except ValueError:
        return _invalid("recipe_token")
    except MealieError as exc:
        return _error(exc)


async def recipe_set_image(arguments: RecipeSetImageArguments, context: Any) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(_RecipeToken, arguments.recipe_token, arguments.integration_id, revision)
        if token.snapshot_hash is None:
            return _invalid("recipe_token")
    except ValueError:
        return _invalid("recipe_token")
    source = await read_media_file(
        arguments.image, context, kind="image", limit=MAX_IMAGE_ATTACHMENT_BYTES,
    )
    if isinstance(source, ToolResult):
        return source
    try:
        context.integration_settings = await context.current_integrations()
        for request in _recipe_mutation_permission(arguments, context):
            if await context.ask_handler(request, check_only=True) != PermissionDecision.ALLOW:
                return ToolResult.error("permission.denied")
        async with _selected_client(arguments.integration_id, context) as (client, _revision):
            current = await client.get_recipe(str(token.recipe_id))
            if current.id != str(token.recipe_id) or current.snapshot_hash != token.snapshot_hash:
                return _remote_changed("recipe_token")
            recipe = await client.set_recipe_image(str(token.recipe_id), source.data, source.mime_type)
        return _recipe_write_result(recipe, arguments.integration_id, revision)
    except ValueError:
        return _invalid("recipe_token")
    except MealieError as exc:
        return _error(exc)


async def recipe_delete(arguments: RecipeDeleteArguments, context: Any) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(_RecipeToken, arguments.recipe_token, arguments.integration_id, revision)
        if token.snapshot_hash is None:
            return _invalid("recipe_token")
        async with _selected_client(arguments.integration_id, context) as (client, _revision):
            current = await client.get_recipe(str(token.recipe_id))
            if current.id != str(token.recipe_id) or current.snapshot_hash != token.snapshot_hash:
                return _remote_changed("recipe_token")
            await client.delete_recipe(str(token.recipe_id))
        return _result({"deleted": True})
    except ValueError:
        return _invalid("recipe_token")
    except MealieError as exc:
        return _error(exc)


async def meal_plan_get(arguments: MealPlanGetArguments, context: Any) -> Any:
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            revision,
        ):
            entries = await client.get_meal_plans(
                arguments.start_date, arguments.end_date, arguments.limit
            )
        return _result(
            {
                "entries": [
                    _meal_plan_payload(entry, arguments.integration_id, revision)
                    for entry in entries[: arguments.limit]
                ]
            }
        )
    except ValueError:
        return _invalid("integration_id")
    except MealieError as exc:
        return _error(exc)


async def meal_plan_add(arguments: MealPlanAddArguments, context: Any) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        recipe_id = None
        if arguments.recipe_token is not None:
            recipe_id = str(
                _decode_token(
                    _RecipeToken,
                    arguments.recipe_token,
                    arguments.integration_id,
                    revision,
                ).recipe_id
            )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            entry = await client.create_meal_plan(
                arguments.date,
                arguments.entry_type,
                arguments.title,
                arguments.text,
                recipe_id,
            )
        try:
            return _result(
                {"entry": _meal_plan_payload(entry, arguments.integration_id, revision)}
            )
        except (MealieError, ValueError):
            from mato_loop_backend.tools.contracts import ToolResult

            return ToolResult.error("integration.outcomeUnknown")
    except ValueError:
        return _invalid("recipe_token")
    except MealieError as exc:
        return _error(exc)


def _entry_matches(token: _MealPlanToken, entry: MealieMealPlanEntry) -> bool:
    return (
        token.meal_plan_id == entry.id
        and token.date == entry.date
        and token.entry_type == entry.entry_type
        and token.title == entry.title
        and token.text == entry.text
        and (str(token.recipe_id) if token.recipe_id is not None else None)
        == entry.recipe_id
        and str(token.group_id) == entry.group_id
        and str(token.user_id) == entry.user_id
        and str(token.household_id) == entry.household_id
    )


async def meal_plan_update(arguments: MealPlanUpdateArguments, context: Any) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _MealPlanToken,
            arguments.meal_plan_token,
            arguments.integration_id,
            revision,
        )
        recipe_id = None
        if "recipe_token" in arguments.model_fields_set and arguments.recipe_token is not None:
            recipe_id = str(
                _decode_token(
                    _RecipeToken,
                    arguments.recipe_token,
                    arguments.integration_id,
                    revision,
                ).recipe_id
            )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            current = await client.get_meal_plan(token.meal_plan_id)
            if not _entry_matches(token, current):
                return _remote_changed()
            changes = {
                field: getattr(arguments, field)
                for field in ("date", "entry_type", "title", "text")
                if field in arguments.model_fields_set
            }
            if "recipe_token" in arguments.model_fields_set:
                changes |= {"recipe_id": recipe_id, "recipe": None}
            updated = replace(current, **changes)
            if updated.recipe_id is None and not updated.title.strip():
                from mato_loop_backend.tools.contracts import ToolResult

                return ToolResult.error("tool.invalidArguments", field="title", params={"reason": "missing", "hint": "Provide a non-blank title when the meal plan entry has no recipe."})
            entry = await client.update_meal_plan(updated)
        try:
            return _result(
                {"entry": _meal_plan_payload(entry, arguments.integration_id, revision)}
            )
        except (MealieError, ValueError):
            from mato_loop_backend.tools.contracts import ToolResult

            return ToolResult.error("integration.outcomeUnknown")
    except ValueError:
        field = (
            "recipe_token"
            if "recipe_token" in arguments.model_fields_set
            else "meal_plan_token"
        )
        return _invalid(field)
    except MealieError as exc:
        return _error(exc)


async def meal_plan_delete(arguments: MealPlanDeleteArguments, context: Any) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _MealPlanToken,
            arguments.meal_plan_token,
            arguments.integration_id,
            revision,
        )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            current = await client.get_meal_plan(token.meal_plan_id)
            if not _entry_matches(token, current):
                return _remote_changed()
            await client.delete_meal_plan(token.meal_plan_id)
        return _result({"deleted": True})
    except ValueError:
        return _invalid("meal_plan_token")
    except MealieError as exc:
        return _error(exc)


def _display_text(value: str | None, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    return f"{text[:limit]}..." if len(text) > limit else text


def _permission(
    capability: str,
    operation_key: str,
    arguments: MealieIntegrationArguments,
    context: Any,
    *,
    title: str,
    summary: str,
    display_items: tuple[Any, ...],
    resource_key: str,
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import integration_operation_permission

    instance, revision = _binding(context, arguments.integration_id)
    return (
        integration_operation_permission(
            capability=capability,
            integration_id=arguments.integration_id,
            binding_revision=revision,
            operation_key=operation_key,
            grant_resource_key=resource_key,
            resource_display=f'{"Rezepte" if resource_key == "recipes" else "Essensplan"} · Mealie-Konto "{_display_text(instance.name)}"',
            title=title,
            summary=summary,
            display_items=display_items,
        ),
    )


def _accounts_permission(
    _arguments: MealieListAccountsArguments, _context: Any
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem, PermissionRequest

    return (
        PermissionRequest(
            capability="mealie.accounts.read",
            resource_type="network_service",
            resource_scope="mealie_accounts",
            action_kind="mealie_list_accounts",
            risk_level="medium",
            title="Mealie-Konten auflisten?",
            summary="Der Agent möchte die konfigurierten Mealie-Konten lesen.",
            resource_display="Konfigurierte Mealie-Konten",
            display_items=(PermissionDisplayItem("Aktion", "Mealie-Konten auflisten"),),
        ),
    )


def _search_permission(
    arguments: RecipesSearchArguments, context: Any
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem

    query = _display_text(arguments.query) or "Alle Rezepte"
    return _permission(
        "recipes.read",
        "recipes-search",
        arguments,
        context,
        title="Rezepte suchen?",
        summary=f'Der Agent möchte nach "{query}" suchen.',
        display_items=(
            PermissionDisplayItem("Suche", query),
            PermissionDisplayItem("Anzahl", f"Bis zu {arguments.limit}"),
            PermissionDisplayItem("Seite", str(arguments.page)),
            *(
                PermissionDisplayItem(label, tuple(values), format="list")
                for label, values in (("Tags", arguments.tags), ("Kategorien", arguments.categories), ("Zutaten", arguments.ingredients))
                if values
            ),
        ),
        resource_key="recipes",
    )


def _recipe_get_permission(
    arguments: RecipeGetArguments, context: Any
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem

    name = "Ausgewähltes Rezept"
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        name = _display_text(
            _decode_token(
                _RecipeToken,
                arguments.recipe_token,
                arguments.integration_id,
                revision,
            ).name
        )
    except ValueError:
        pass
    return _permission(
        "recipes.read",
        "recipe-get",
        arguments,
        context,
        title="Rezept lesen?",
        summary=f'Der Agent möchte das Rezept "{name}" lesen.',
        display_items=(PermissionDisplayItem("Rezept", name),),
        resource_key="recipes",
    )


def _recipe_mutation_permission(
    arguments: RecipeCreateArguments | RecipeImportArguments | RecipeUpdateArguments | RecipeDeleteArguments | RecipeSetImageArguments,
    context: Any,
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem

    name = "Ausgewähltes Rezept"
    if isinstance(arguments, RecipeCreateArguments):
        operation, verb, name = "recipe-create", "erstellen", arguments.name
    elif isinstance(arguments, RecipeImportArguments):
        operation, verb = "recipe-import-url", "importieren"
    else:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(_RecipeToken, arguments.recipe_token, arguments.integration_id, revision)
        name = token.name
        if isinstance(arguments, RecipeSetImageArguments):
            operation, verb = "recipe-set-image", "bebildern"
        else:
            operation, verb = ("recipe-delete", "löschen") if isinstance(arguments, RecipeDeleteArguments) else ("recipe-update", "ändern")
    items = [PermissionDisplayItem("Rezept", _display_text(name))]
    if isinstance(arguments, RecipeSetImageArguments):
        items.extend((
            PermissionDisplayItem("Bildquelle", "Computer" if arguments.image.root == "workspace" else "Session-Ablage"),
            PermissionDisplayItem("Bilddatei", arguments.image.path, format="code"),
            PermissionDisplayItem("Änderung", "Das bisherige Hauptbild ersetzen."),
        ))
    if isinstance(arguments, RecipeImportArguments):
        items.append(PermissionDisplayItem("Quell-URL", str(arguments.url)))
    if isinstance(arguments, RecipeFields):
        for field, label in (
            ("name", "Name"), ("description", "Beschreibung"), ("servings", "Portionen"),
            ("yield_quantity", "Ertragsmenge"), ("yield_text", "Ertrag"),
            ("total_time", "Gesamtzeit"), ("prep_time", "Vorbereitung"),
            ("cook_time", "Kochzeit"), ("perform_time", "Zubereitungszeit"),
        ):
            if field in arguments.model_fields_set:
                items.append(PermissionDisplayItem(label, _display_text(str(getattr(arguments, field)))))
        if "ingredients" in arguments.model_fields_set:
            if any(part.food or part.unit for part in arguments.ingredients or []):
                items.append(PermissionDisplayItem("Lebensmittel und Einheiten", "Vorhandene verwenden, fehlende in Mealie anlegen."))
            items.append(PermissionDisplayItem(
                "Zutaten (ersetzen)", tuple(
                    _display_text(" ".join(str(value) for value in (part.quantity, part.unit, part.food, part.note, part.title, part.original_text) if value is not None))
                    for part in arguments.ingredients or []
                ) or ("Liste leeren",), format="list",
            ))
        if "instructions" in arguments.model_fields_set:
            items.append(PermissionDisplayItem(
                "Zubereitung (ersetzen)", tuple(_display_text(step.text) for step in arguments.instructions or []) or ("Liste leeren",), format="list",
            ))
    return _permission(
        "recipes.delete" if isinstance(arguments, RecipeDeleteArguments) else "recipes.write",
        _operation_key(operation, arguments), arguments, context,
        title=f"Rezept {verb}?",
        summary=f'Der Agent möchte ein Rezept {verb}.',
        display_items=tuple(items), resource_key="recipes",
    )


def _meal_plan_get_permission(
    arguments: MealPlanGetArguments, context: Any
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem

    period = f"{arguments.start_date:%d.%m.%Y} bis {arguments.end_date:%d.%m.%Y}"
    return _permission(
        "meal_plan.read",
        "meal-plan-get",
        arguments,
        context,
        title="Essensplan lesen?",
        summary=f"Der Agent möchte den Essensplan vom {period} lesen.",
        display_items=(
            PermissionDisplayItem("Zeitraum", period),
            PermissionDisplayItem("Anzahl", f"Bis zu {arguments.limit}"),
        ),
        resource_key="meal-plan",
    )


def _meal_plan_add_permission(
    arguments: MealPlanAddArguments, context: Any
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem

    label = _display_text(arguments.title or arguments.text) or "Rezept"
    return _permission(
        "meal_plan.write",
        _operation_key("meal-plan-add", arguments),
        arguments,
        context,
        title="Essensplan-Eintrag erstellen?",
        summary=f'Der Agent möchte am {arguments.date:%d.%m.%Y} "{label}" eintragen.',
        display_items=(
            PermissionDisplayItem("Datum", f"{arguments.date:%d.%m.%Y}"),
            PermissionDisplayItem("Eintrag", label),
            PermissionDisplayItem("Typ", arguments.entry_type),
        ),
        resource_key="meal-plan",
    )


def _meal_plan_for_permission(
    arguments: MealPlanTokenArguments, context: Any
) -> _MealPlanToken:
    _instance, revision = _binding(context, arguments.integration_id)
    return _decode_token(
        _MealPlanToken,
        arguments.meal_plan_token,
        arguments.integration_id,
        revision,
    )


def _meal_plan_update_permission(
    arguments: MealPlanUpdateArguments, context: Any
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem

    changes = []
    for field, label in (
        ("date", "Datum"),
        ("entry_type", "Typ"),
        ("title", "Titel"),
        ("text", "Text"),
        ("recipe_token", "Rezept"),
    ):
        if field in arguments.model_fields_set:
            value = getattr(arguments, field)
            if field == "recipe_token":
                value = "entfernen" if value is None else "ändern"
            elif isinstance(value, Date):
                value = f"{value:%d.%m.%Y}"
            changes.append(f"{label}: {_display_text(str(value))}")
    try:
        token = _meal_plan_for_permission(arguments, context)
        entry = _display_text(token.title or token.text or token.recipe_name) or "Eintrag"
    except ValueError:
        entry = "Essensplan-Eintrag"
    return _permission(
        "meal_plan.write",
        _operation_key("meal-plan-update", arguments),
        arguments,
        context,
        title="Essensplan-Eintrag ändern?",
        summary=f'Der Agent möchte "{entry}" ändern.',
        display_items=(
            PermissionDisplayItem("Eintrag", entry),
            PermissionDisplayItem("Änderungen", tuple(changes), format="list"),
        ),
        resource_key="meal-plan",
    )


def _meal_plan_delete_permission(
    arguments: MealPlanDeleteArguments, context: Any
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem

    try:
        token = _meal_plan_for_permission(arguments, context)
        entry = _display_text(token.title or token.text or token.recipe_name) or "Eintrag"
        day = f"{token.date:%d.%m.%Y}"
    except ValueError:
        entry, day = "Essensplan-Eintrag", "Unbekannt"
    return _permission(
        "meal_plan.delete",
        _operation_key("meal-plan-delete", arguments),
        arguments,
        context,
        title="Essensplan-Eintrag löschen?",
        summary=f'Der Agent möchte "{entry}" löschen.',
        display_items=(
            PermissionDisplayItem("Eintrag", entry),
            PermissionDisplayItem("Datum", day),
        ),
        resource_key="meal-plan",
    )


MEALIE_SHARED_TOOLS = (
    ToolContribution(
        name="mealie_list_accounts",
        snapshot_policy=ToolSnapshotPolicy("accounts", ("integrationId", "name"), max_age_seconds=None),
        description="List active Mealie accounts available to this tool binding.",
        arguments_model=MealieListAccountsArguments,
        handler=mealie_list_accounts,
        label="Mealie-Konten auflisten",
        icon="restaurant",
        actions=(_ACCOUNTS_READ,),
        instance_argument=None,
        authorization_factory=_accounts_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="recipes_search",
        description="Search recipes in one Mealie account. Filter by exact tag, category, and structured food names. Follow nextPage with the same query, filters, and limit to read more results. Return recipe tokens for recipe_get.",
        arguments_model=RecipesSearchArguments,
        handler=recipes_search,
        label="Rezepte suchen",
        icon="search",
        actions=(_RECIPES_READ,),
        instance_argument="integration_id",
        authorization_factory=_search_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="recipe_get",
        description="Read a recipe using a recipeToken from a recipe tool. Return ingredients, instructions, and a fresh recipeToken required for update or delete.",
        arguments_model=RecipeGetArguments,
        handler=recipe_get,
        label="Rezept lesen",
        icon="menu_book",
        actions=(_RECIPES_READ,),
        instance_argument="integration_id",
        authorization_factory=_recipe_get_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="recipe_create",
        description="Create a Mealie recipe with a name and optional ingredients, instructions, servings, yield, description, and times. Reuse matching foods and units; create missing ones. Return the saved recipe and recipeToken. Do not retry an unknown outcome; search for the recipe first.",
        arguments_model=RecipeCreateArguments,
        handler=recipe_create,
        label="Rezept erstellen",
        icon="post_add",
        actions=(_RECIPES_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_recipe_mutation_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="recipe_import_url",
        description="Mealie fetches and saves a recipe from an HTTP(S) URL. Returns the saved recipe and recipeToken. Check by search before retrying an unknown outcome.",
        arguments_model=RecipeImportArguments,
        handler=recipe_import_url,
        label="Rezept aus URL importieren",
        icon="download",
        actions=(_RECIPES_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_recipe_mutation_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="recipe_update",
        description="Update with the latest recipeToken from recipe_get or a recipe write. Omit unchanged fields; supplied lists replace whole lists. Reuse foods/units or create missing ones. Read again if changed or the outcome is unknown.",
        arguments_model=RecipeUpdateArguments,
        handler=recipe_update,
        label="Rezept ändern",
        icon="edit_note",
        actions=(_RECIPES_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_recipe_mutation_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="recipe_delete",
        description="Delete a recipe using the latest recipeToken from recipe_get or a recipe write. A changed recipe requires a fresh read. Do not retry an unknown outcome without checking whether the recipe still exists.",
        arguments_model=RecipeDeleteArguments,
        handler=recipe_delete,
        label="Rezept löschen",
        icon="delete",
        actions=(_RECIPES_DELETE,),
        instance_argument="integration_id",
        authorization_factory=_recipe_mutation_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="recipe_set_image",
        description="Replace a recipe's main image using a fresh recipeToken from recipe_get or a recipe write and an image file reference. Use the stored path for generated images, attachments, or downloads, or a selected-computer file. Requires source-file read and recipe-write permission. Return the saved recipe and a fresh recipeToken. If the outcome is unknown, inspect the recipe in Mealie before retrying.",
        arguments_model=RecipeSetImageArguments,
        handler=recipe_set_image,
        label="Rezeptbild setzen",
        icon="image",
        actions=(_RECIPES_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_recipe_mutation_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="meal_plan_get",
        description="Read meal-plan entries in an inclusive date range.",
        arguments_model=MealPlanGetArguments,
        handler=meal_plan_get,
        label="Essensplan lesen",
        icon="calendar_month",
        actions=(_MEAL_PLAN_READ,),
        instance_argument="integration_id",
        authorization_factory=_meal_plan_get_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="meal_plan_add",
        description=(
            "Add a meal-plan entry, optionally using a recipe token returned by a recipe tool."
        ),
        arguments_model=MealPlanAddArguments,
        handler=meal_plan_add,
        label="Essensplan-Eintrag erstellen",
        icon="event_available",
        actions=(_MEAL_PLAN_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_meal_plan_add_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="meal_plan_update",
        description="Update fields of a meal-plan entry using its latest mealPlanToken.",
        arguments_model=MealPlanUpdateArguments,
        handler=meal_plan_update,
        label="Essensplan-Eintrag ändern",
        icon="edit_calendar",
        actions=(_MEAL_PLAN_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_meal_plan_update_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="meal_plan_delete",
        description="Delete a meal-plan entry using its latest mealPlanToken.",
        arguments_model=MealPlanDeleteArguments,
        handler=meal_plan_delete,
        label="Essensplan-Eintrag löschen",
        icon="event_busy",
        actions=(_MEAL_PLAN_DELETE,),
        instance_argument="integration_id",
        authorization_factory=_meal_plan_delete_permission,
        releases_private_data=True,
    ),
)


MEALIE_ADAPTER = IntegrationAdapterDefinition(
    integration_type="recipes",
    adapter_type="mealie",
    config_model=MealieConfig,
    contribution_factory=lambda _instance: IntegrationContributions(),
    resource_actions=(
        _ACCOUNTS_READ,
        _RECIPES_READ,
        _RECIPES_WRITE,
        _RECIPES_DELETE,
        _MEAL_PLAN_READ,
        _MEAL_PLAN_WRITE,
        _MEAL_PLAN_DELETE,
    ),
    shared_tools=MEALIE_SHARED_TOOLS,
)

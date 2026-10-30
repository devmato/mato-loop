from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

from mato_loop_backend.integrations.bookstack.client import (
    MAX_ATTACHMENT_BYTES,
    MAX_MARKDOWN_CHARS,
    BookStackAttachment,
    BookStackBook,
    BookStackChapter,
    BookStackClient,
    BookStackError,
    BookStackPage,
    BookStackSearchResult,
    BookStackShelf,
)
from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    IntegrationResource,
    ToolContribution,
)
from mato_loop_backend.integrations.service import remember_resource_labels
from mato_loop_backend.models import Credential
from mato_loop_backend.security import CredentialCipher, load_credential, read_credential
from mato_loop_backend.settings import (
    IntegrationInstance,
    ToolExecutionPolicy,
)
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.snapshots import ToolSnapshotPolicy

BOOKSTACK_CREDENTIAL_KIND = "bookstack.token"
_BOOK_KEY_PATTERN = r"^book:[1-9][0-9]{0,17}$"
_SHELF_KEY_PATTERN = r"^shelf:[1-9][0-9]{0,17}$"
_BOOK_KEY_RE = re.compile(_BOOK_KEY_PATTERN)
_SHELF_KEY_RE = re.compile(_SHELF_KEY_PATTERN)
MAX_SHELF_BOOKS = 200
MAX_BOOK_SHELVES = 50
MAX_SEARCH_QUERIES = 5


def _validate_book_keys(book_keys: list[str] | None) -> None:
    if book_keys is None:
        return
    if any(not _BOOK_KEY_RE.match(key) for key in book_keys):
        raise ValueError("invalid book key")
    if len(set(book_keys)) != len(book_keys):
        raise ValueError("duplicate book key")


def _validate_shelf_keys(shelf_keys: list[str] | None) -> None:
    if shelf_keys is None:
        return
    if any(not _SHELF_KEY_RE.match(key) for key in shelf_keys):
        raise ValueError("invalid shelf key")
    if len(set(shelf_keys)) != len(shelf_keys):
        raise ValueError("duplicate shelf key")


_READ = ToolAction(
    "knowledge.read",
    "Wissensdatenbank lesen",
    category="read",
)
_WRITE = ToolAction(
    "knowledge.write",
    "Wissensdatenbank ändern",
    category="write",
    mutates=True,
)
_DELETE = ToolAction(
    "knowledge.delete",
    "Wissenseinträge löschen",
    category="delete",
    mutates=True,
)


class BookStackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: HttpUrl

    @model_validator(mode="after")
    def require_secure_url(self) -> BookStackConfig:
        if (
            self.base_url.scheme != "https"
            or self.base_url.username is not None
            or self.base_url.password is not None
            or self.base_url.query is not None
            or self.base_url.fragment is not None
        ):
            raise ValueError(
                "BookStack URL must be a secure endpoint without credentials"
            )
        return self


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeListAccountsArguments(_Arguments):
    pass


class KnowledgeIntegrationArguments(_Arguments):
    integration_id: UUID


class KnowledgeBookArguments(KnowledgeIntegrationArguments):
    book_key: str = Field(pattern=_BOOK_KEY_PATTERN)


class KnowledgeSearchArguments(KnowledgeIntegrationArguments):
    queries: list[
        Annotated[str, StringConstraints(min_length=1, max_length=400)]
    ] = Field(min_length=1, max_length=MAX_SEARCH_QUERIES)
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="before")
    @classmethod
    def accept_single_query(cls, data: Any) -> Any:
        # Models trained on the previous single-query schema keep emitting
        # "query"; fold it in instead of failing the whole tool call.
        if isinstance(data, dict) and "queries" not in data and "query" in data:
            data = {
                key: value for key, value in data.items() if key != "query"
            } | {"queries": [data["query"]]}
        return data

    @model_validator(mode="after")
    def normalize_queries(self) -> KnowledgeSearchArguments:
        unique: list[str] = []
        seen: set[str] = set()
        for query in self.queries:
            cleaned = " ".join(query.split())
            marker = cleaned.casefold()
            if not cleaned or marker in seen:
                continue
            seen.add(marker)
            unique.append(cleaned)
        if not unique:
            raise ValueError("at least one query is required")
        self.queries = unique
        return self


class KnowledgeReadPageArguments(KnowledgeBookArguments):
    page_token: str = Field(min_length=1, max_length=50_000)


class KnowledgeCreatePageArguments(KnowledgeBookArguments):
    name: str = Field(min_length=1, max_length=255)
    markdown: str = Field(min_length=1, max_length=MAX_MARKDOWN_CHARS)
    chapter_token: str | None = Field(default=None, max_length=50_000)


class KnowledgeUpdatePageArguments(KnowledgeBookArguments):
    page_token: str = Field(min_length=1, max_length=50_000)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    markdown: str | None = Field(default=None, max_length=MAX_MARKDOWN_CHARS)

    @model_validator(mode="after")
    def require_patch(self) -> KnowledgeUpdatePageArguments:
        if self.name is None and self.markdown is None:
            raise ValueError("at least one page field is required")
        return self


class KnowledgeDeletePageArguments(KnowledgeBookArguments):
    page_token: str = Field(min_length=1, max_length=50_000)


class KnowledgeListAttachmentsArguments(KnowledgeBookArguments):
    page_token: str = Field(min_length=1, max_length=50_000)


class KnowledgeCreateAttachmentArguments(KnowledgeBookArguments):
    page_token: str = Field(min_length=1, max_length=50_000)
    name: str = Field(min_length=1, max_length=255)
    url: str | None = Field(default=None, min_length=1, max_length=2_000)
    link: str | None = Field(default=None, min_length=1, max_length=2_000)
    root: Literal["session", "workspace"] | None = None
    path: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def require_single_source(self) -> KnowledgeCreateAttachmentArguments:
        has_file = self.root is not None or self.path is not None
        if has_file and (self.root is None or self.path is None):
            raise ValueError("root and path must be provided together")
        sources = [self.url is not None, self.link is not None, has_file]
        if sum(sources) != 1:
            raise ValueError("exactly one of url, link, or root with path is required")
        return self


class KnowledgeDeleteAttachmentArguments(KnowledgeBookArguments):
    attachment_token: str = Field(min_length=1, max_length=50_000)


class KnowledgeCreateBookArguments(KnowledgeIntegrationArguments):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    shelf_keys: list[str] | None = Field(default=None, max_length=MAX_BOOK_SHELVES)

    @model_validator(mode="after")
    def check_shelf_keys(self) -> KnowledgeCreateBookArguments:
        _validate_shelf_keys(self.shelf_keys)
        return self


class KnowledgeUpdateBookArguments(KnowledgeBookArguments):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def require_change(self) -> KnowledgeUpdateBookArguments:
        if self.name is None and self.description is None:
            raise ValueError("name or description is required")
        return self


class KnowledgeCreateChapterArguments(KnowledgeBookArguments):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)


class KnowledgeUpdateChapterArguments(KnowledgeBookArguments):
    chapter_token: str = Field(min_length=1, max_length=50_000)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def require_change(self) -> KnowledgeUpdateChapterArguments:
        if self.name is None and self.description is None:
            raise ValueError("name or description is required")
        return self


class KnowledgeDeleteChapterArguments(KnowledgeBookArguments):
    chapter_token: str = Field(min_length=1, max_length=50_000)


class KnowledgeShelfArguments(KnowledgeIntegrationArguments):
    shelf_key: str = Field(pattern=_SHELF_KEY_PATTERN)


class KnowledgeCreateShelfArguments(KnowledgeIntegrationArguments):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    book_keys: list[str] | None = Field(
        default=None, max_length=MAX_SHELF_BOOKS
    )

    @model_validator(mode="after")
    def check_book_keys(self) -> KnowledgeCreateShelfArguments:
        _validate_book_keys(self.book_keys)
        return self


class KnowledgeUpdateShelfArguments(KnowledgeShelfArguments):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    book_keys: list[str] | None = Field(
        default=None, max_length=MAX_SHELF_BOOKS
    )
    add_book_keys: list[str] | None = Field(
        default=None, max_length=MAX_SHELF_BOOKS
    )
    remove_book_keys: list[str] | None = Field(
        default=None, max_length=MAX_SHELF_BOOKS
    )

    @model_validator(mode="after")
    def require_patch(self) -> KnowledgeUpdateShelfArguments:
        if (
            self.name is None
            and self.description is None
            and self.book_keys is None
            and self.add_book_keys is None
            and self.remove_book_keys is None
        ):
            raise ValueError("at least one shelf field is required")
        if self.book_keys is not None and (
            self.add_book_keys is not None or self.remove_book_keys is not None
        ):
            raise ValueError("book_keys cannot be combined with add or remove")
        _validate_book_keys(self.book_keys)
        _validate_book_keys(self.add_book_keys)
        _validate_book_keys(self.remove_book_keys)
        overlap = set(self.add_book_keys or ()) & set(self.remove_book_keys or ())
        if overlap:
            raise ValueError("book key cannot be added and removed at once")
        return self


class _PageToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration_id: UUID = Field(alias="i")
    binding_revision: str = Field(alias="r", max_length=64)
    book_key: str = Field(alias="b", pattern=_BOOK_KEY_PATTERN)
    page_id: int = Field(alias="p", ge=1)
    name: str | None = Field(alias="n", default=None, max_length=255)
    chapter_id: int | None = Field(alias="c", default=None, ge=1)
    # Revision the agent observed. Carried in the token so conflict detection
    # does not depend on the model passing it back.
    updated_at: str | None = Field(alias="u", default=None, max_length=64)


class _ChapterToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration_id: UUID = Field(alias="i")
    binding_revision: str = Field(alias="r", max_length=64)
    book_key: str = Field(alias="b", pattern=_BOOK_KEY_PATTERN)
    chapter_id: int = Field(alias="c", ge=1)
    name: str | None = Field(alias="n", default=None, max_length=255)


class _AttachmentToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration_id: UUID = Field(alias="i")
    binding_revision: str = Field(alias="r", max_length=64)
    book_key: str = Field(alias="b", pattern=_BOOK_KEY_PATTERN)
    attachment_id: int = Field(alias="a", ge=1)
    name: str | None = Field(alias="n", default=None, max_length=255)


_BOOKSTACK_ERROR_CODES = {
    "bookstack.attachmentTooLarge": "integration.attachmentTooLarge",
    "bookstack.authenticationFailed": "integration.authenticationFailed",
    "bookstack.crossOrigin": "integration.invalidResponse",
    "bookstack.invalidLimit": "integration.invalidConfiguration",
    "bookstack.invalidPage": "tool.invalidArguments",
    "bookstack.invalidQuery": "tool.invalidArguments",
    "bookstack.invalidRequest": "tool.invalidArguments",
    "bookstack.invalidUrl": "integration.invalidConfiguration",
    "bookstack.malformedResponse": "integration.invalidResponse",
    "bookstack.notFound": "integration.remoteNotFound",
    "bookstack.outcomeUnknown": "integration.outcomeUnknown",
    "bookstack.pageTooLarge": "integration.responseTooLarge",
    "bookstack.permissionDenied": "integration.permissionDenied",
    "bookstack.rateLimited": "integration.rateLimited",
    "bookstack.redirectRejected": "integration.unavailable",
    "bookstack.responseTooLarge": "integration.responseTooLarge",
    "bookstack.unavailable": "integration.unavailable",
    "bookstack.unexpectedStatus": "integration.unavailable",
}


def bookstack_error_code(error: BookStackError) -> str:
    return _BOOKSTACK_ERROR_CODES.get(error.code, "integration.unavailable")


def _disabled_book_keys(context: Any, instance: IntegrationInstance) -> set[str]:
    """Collect books whose read policy is disabled for this integration.

    Book policies are enforced through the resource key of a permission
    request. Tools that operate across a whole knowledge base carry no such key,
    so they have to drop disabled books themselves. Otherwise a book the user
    switched off would still leak names, previews and reusable tokens.
    """
    from mato_loop_backend.permissions.resolution import resolve_tool_execution_policy
    from mato_loop_backend.tools.catalog import tool_registry

    tool_settings = getattr(context, "tool_settings", None)
    if tool_settings is None:
        return set()
    action = next(
        (
            item
            for item in tool_registry.integration_resource_actions(instance)
            if item.capability == "knowledge.read"
        ),
        None,
    )
    if action is None:
        return set()
    disabled = set()
    for resource_key in _resource_keys(context, instance):
        resolution = resolve_tool_execution_policy(
            tool_settings,
            "knowledge.read",
            action.default_policy(tool_settings),
            integration_id=instance.id,
            resource_key=resource_key,
        )
        if resolution.policy == ToolExecutionPolicy.DISABLED:
            disabled.add(resource_key)
    return disabled


def _resource_keys(context: Any, instance: IntegrationInstance) -> set[str]:
    tool_settings = getattr(context, "tool_settings", None)
    profile_permissions = getattr(tool_settings, "_agent_permissions", None)
    baseline = tool_settings.permissions.resource_policies.get(
        str(instance.id), {}
    )
    overrides = (
        profile_permissions.resource_policies.get(str(instance.id), {})
        if profile_permissions is not None
        else {}
    )
    availability = tool_settings.permissions.resource_availability.get(str(instance.id), {})
    return {key for key in baseline | overrides | availability if _BOOK_KEY_RE.match(key)}


def _refuse_blocked_books(
    keys: list[str] | None, blocked: set[str], field: str
) -> Any:
    """Reject shelf changes that name a book the user switched off."""
    if not keys or not blocked:
        return None
    if any(key in blocked for key in keys):
        from mato_loop_backend.tools.contracts import ToolResult

        return ToolResult.error("permission.denied", field=field)
    return None


def _bound_ids(context: Any) -> set[str]:
    value = context.bound_options.get("integration_ids")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return set()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        return set()
    return set(value)


def _binding(context: Any, integration_id: UUID) -> tuple[IntegrationInstance, str]:
    selected_id = context.bound_options.get("integration_id")
    revision = context.bound_options.get("binding_revision")
    if selected_id != str(integration_id) or not isinstance(revision, str):
        raise ValueError("missing selected integration binding")
    instance = next(
        (
            item
            for item in context.integration_settings.instances
            if str(item.id) == selected_id
            and item.enabled
            and item.integration_type == "knowledge"
            and item.adapter_type == "bookstack"
        ),
        None,
    )
    if instance is None or json.dumps(
        instance.binding_data(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ) != revision:
        raise ValueError("stale selected integration binding")
    return instance, revision


def _credential_payload(record: Credential | None) -> dict[str, Any]:
    if record is None or record.kind != BOOKSTACK_CREDENTIAL_KIND:
        raise BookStackError("bookstack.authenticationFailed")
    try:
        payload = read_credential(record)
    except Exception as exc:
        raise BookStackError("bookstack.authenticationFailed") from exc
    if (
        set(payload) != {"token_id", "token_secret"}
        or not all(isinstance(value, str) and value for value in payload.values())
    ):
        raise BookStackError("bookstack.authenticationFailed")
    return payload


@asynccontextmanager
async def _selected_client(
    integration_id: UUID, context: Any
) -> AsyncIterator[tuple[BookStackClient, str]]:
    instance, revision = _binding(context, integration_id)
    if instance.credential_id is None:
        raise BookStackError("bookstack.authenticationFailed")
    record = await load_credential(
        context.db, instance.credential_id, owner_id=instance.owner_id
    )
    credential = _credential_payload(record)
    config = BookStackConfig.model_validate(instance.config)
    async with BookStackClient(
        str(config.base_url), credential["token_id"], credential["token_secret"]
    ) as client:
        yield client, revision


def _book_id(book_key: str) -> int:
    return int(book_key.removeprefix("book:"))


def _shelf_id(shelf_key: str) -> int:
    return int(shelf_key.removeprefix("shelf:"))


def _book_ids(book_keys: list[str] | None) -> tuple[int, ...] | None:
    if book_keys is None:
        return None
    return tuple(_book_id(key) for key in book_keys)


async def _shelf_memberships(client: BookStackClient) -> dict[int, list[str]] | None:
    """Map book ids to the shelf keys they belong to.

    BookStack exposes shelf membership only from the shelf side, so this scans
    every shelf. Returns None when shelves are unreadable, which lets callers
    omit the field instead of failing an otherwise successful read.
    """
    try:
        shelves, _truncated = await client.list_shelves()
        memberships: dict[int, list[str]] = {}
        for shelf in shelves:
            contents = await client.read_shelf(shelf.id)
            for book in contents.books:
                memberships.setdefault(book.id, []).append(f"shelf:{shelf.id}")
        return memberships
    except BookStackError:
        return None


def _book_shelf_keys(
    memberships: dict[int, list[str]] | None, book_id: int
) -> list[str] | None:
    if memberships is None:
        return None
    return memberships.get(book_id, [])


async def _apply_shelf_membership(
    client: BookStackClient, book_id: int, shelf_keys: list[str]
) -> None:
    for shelf_key in shelf_keys:
        shelf_id = _shelf_id(shelf_key)
        contents = await client.read_shelf(shelf_id)
        current = [book.id for book in contents.books]
        if book_id in current:
            continue
        await client.update_shelf(shelf_id, book_ids=(*current, book_id))


def _label(value: str) -> str:
    return (value.strip() or "Buch")[:160]


def _bounded(value: str | None, limit: int) -> str | None:
    return value[:limit] if value is not None else None


def _revision_fingerprint(revision: str) -> str:
    return hashlib.sha256(revision.encode()).hexdigest()[:32]


def _page_token(
    page: BookStackPage, integration_id: UUID, revision: str, book_key: str
) -> str:
    payload = _PageToken(
        i=integration_id,
        r=_revision_fingerprint(revision),
        b=book_key,
        p=page.id,
        n=page.name[:255],
        c=page.chapter_id,
        u=page.updated_at,
    )
    return (
        CredentialCipher()
        .encrypt(payload.model_dump(mode="json", by_alias=True, exclude_none=True))
        .decode("ascii")
    )


def _chapter_token(
    chapter: BookStackChapter, integration_id: UUID, revision: str, book_key: str
) -> str:
    payload = _ChapterToken(
        i=integration_id,
        r=_revision_fingerprint(revision),
        b=book_key,
        c=chapter.id,
        n=chapter.name[:255],
    )
    return (
        CredentialCipher()
        .encrypt(payload.model_dump(mode="json", by_alias=True))
        .decode("ascii")
    )


def _attachment_token(
    attachment: BookStackAttachment,
    integration_id: UUID,
    revision: str,
    book_key: str,
) -> str:
    payload = _AttachmentToken(
        i=integration_id,
        r=_revision_fingerprint(revision),
        b=book_key,
        a=attachment.id,
        n=attachment.name[:255],
    )
    return (
        CredentialCipher()
        .encrypt(payload.model_dump(mode="json", by_alias=True))
        .decode("ascii")
    )


def _decode_token(
    model: type[BaseModel],
    value: str,
    integration_id: UUID,
    revision: str,
    book_key: str,
) -> Any:
    try:
        payload = model.model_validate(
            CredentialCipher().decrypt(value.encode("ascii"))
        )
    except Exception as exc:
        raise ValueError("invalid token") from exc
    if (
        payload.integration_id != integration_id
        or payload.binding_revision != _revision_fingerprint(revision)
        or payload.book_key != book_key
    ):
        raise ValueError("token binding mismatch")
    return payload


def _result(value: dict[str, Any]) -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.json({**value, "contentIsUntrusted": True})


def _error(error: BookStackError) -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.error(bookstack_error_code(error))


def _invalid(field: str) -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.error("integration.staleReference", field=field)


def _book_payload(
    book: BookStackBook, shelf_keys: list[str] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "bookKey": f"book:{book.id}",
        "name": _bounded(book.name, 255),
        "description": _bounded(book.description, 2_000),
    }
    if shelf_keys is not None:
        payload["shelfKeys"] = shelf_keys
    return payload


def _shelf_payload(shelf: BookStackShelf) -> dict[str, Any]:
    return {
        "shelfKey": f"shelf:{shelf.id}",
        "name": _bounded(shelf.name, 255),
        "description": _bounded(shelf.description, 2_000),
    }


async def _remember_shelves(arguments: KnowledgeIntegrationArguments, context: Any, shelves: tuple[BookStackShelf, ...]) -> None:
    instance, revision = _binding(context, arguments.integration_id)
    await remember_resource_labels(context, instance, {
        f"{_revision_fingerprint(revision)}:shelf:{shelf.id}": _display_text(shelf.name)
        for shelf in shelves
    })


def _attachment_summary(
    attachment: BookStackAttachment,
    integration_id: UUID,
    revision: str,
    book_key: str,
) -> dict[str, Any]:
    return {
        "attachmentToken": _attachment_token(
            attachment, integration_id, revision, book_key
        ),
        "name": _bounded(attachment.name, 255),
        "kind": "link" if attachment.external else "file",
        "extension": _bounded(attachment.extension, 20),
    }


def _page_summary(
    page: BookStackPage, integration_id: UUID, revision: str, book_key: str
) -> dict[str, Any]:
    return {
        "pageToken": _page_token(page, integration_id, revision, book_key),
        "name": _bounded(page.name, 255),
        "draft": page.draft,
        "updatedAt": page.updated_at,
    }


def _chapter_summary(
    chapter: BookStackChapter, integration_id: UUID, revision: str, book_key: str
) -> dict[str, Any]:
    return {
        "chapterToken": _chapter_token(chapter, integration_id, revision, book_key),
        "name": _bounded(chapter.name, 255),
        "description": _bounded(chapter.description, 2_000),
    }


async def knowledge_list_accounts(
    _arguments: KnowledgeListAccountsArguments, context: Any
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
        and instance.integration_type == "knowledge"
        and instance.adapter_type == "bookstack"
    ]
    return _result({"accounts": accounts})


async def knowledge_list_books(
    arguments: KnowledgeIntegrationArguments, context: Any
) -> Any:
    try:
        instance, _revision_value = _binding(context, arguments.integration_id)
        blocked = _disabled_book_keys(context, instance)
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            books, truncated = await client.list_books()
            memberships = await _shelf_memberships(client)
        payload: dict[str, Any] = {
            "books": [
                _book_payload(book, _book_shelf_keys(memberships, book.id))
                for book in books
                if f"book:{book.id}" not in blocked
            ]
        }
        if truncated:
            payload["moreBooksExist"] = True
        return _result(payload)
    except ValueError:
        return _invalid("integration_id")
    except BookStackError as exc:
        return _error(exc)


async def _merged_search(
    client: Any, queries: list[str], limit: int
) -> list[BookStackSearchResult]:
    """Run every query and interleave the batches so each query contributes its
    strongest hits before lower ranked results of any other query."""
    outcomes = await asyncio.gather(
        *(client.search(query, limit) for query in queries), return_exceptions=True
    )
    batches: list[tuple[BookStackSearchResult, ...]] = []
    failure: BaseException | None = None
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            failure = failure or outcome
            continue
        batches.append(outcome)
    if failure is not None:
        raise failure
    merged: list[BookStackSearchResult] = []
    seen: set[tuple[str, int]] = set()
    for rank in range(limit):
        for batch in batches:
            if rank >= len(batch):
                continue
            result = batch[rank]
            marker = (result.type, result.id)
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(result)
    return merged


async def knowledge_search(arguments: KnowledgeSearchArguments, context: Any) -> Any:
    try:
        instance, _revision_value = _binding(context, arguments.integration_id)
        blocked = _disabled_book_keys(context, instance)
        async with _selected_client(arguments.integration_id, context) as (
            client,
            revision,
        ):
            results = await _merged_search(
                client, arguments.queries, arguments.limit
            )
        payload = []
        for result in results:
            if len(payload) >= arguments.limit:
                break
            if result.book_id is None and result.type != "book":
                continue
            book_key = (
                f"book:{result.book_id}"
                if result.type != "book"
                else f"book:{result.id}"
            )
            if book_key in blocked:
                continue
            item: dict[str, Any] = {
                "type": result.type,
                "name": _bounded(result.name, 255),
                "bookKey": book_key,
                "preview": _bounded(result.preview, 1_000),
            }
            if result.type == "page":
                item["pageToken"] = _page_token(
                    BookStackPage(
                        id=result.id,
                        book_id=result.book_id or 0,
                        chapter_id=result.chapter_id,
                        name=result.name,
                        slug="",
                    ),
                    arguments.integration_id,
                    revision,
                    book_key,
                )
            elif result.type == "chapter":
                item["chapterToken"] = _chapter_token(
                    BookStackChapter(
                        id=result.id,
                        book_id=result.book_id or 0,
                        name=result.name,
                        slug="",
                    ),
                    arguments.integration_id,
                    revision,
                    book_key,
                )
            payload.append(item)
        return _result({"results": payload})
    except BookStackError as exc:
        return _error(exc)


async def knowledge_read_book(arguments: KnowledgeBookArguments, context: Any) -> Any:
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            revision,
        ):
            contents = await client.read_book(_book_id(arguments.book_key))
            memberships = await _shelf_memberships(client)
        return _result(
            {
                "book": _book_payload(
                    contents.book, _book_shelf_keys(memberships, contents.book.id)
                ),
                "chapters": [
                    _chapter_summary(
                        chapter,
                        arguments.integration_id,
                        revision,
                        arguments.book_key,
                    )
                    for chapter in contents.chapters
                ],
                "pages": [
                    _page_summary(
                        page, arguments.integration_id, revision, arguments.book_key
                    )
                    for page in contents.pages
                ],
            }
        )
    except BookStackError as exc:
        return _error(exc)


async def knowledge_read_page(
    arguments: KnowledgeReadPageArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _PageToken,
            arguments.page_token,
            arguments.integration_id,
            revision,
            arguments.book_key,
        )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            page = await client.read_page(token.page_id)
        if page.book_id != _book_id(arguments.book_key):
            return _invalid("page_token")
        return _result(
            {
                "page": {
                    **_page_summary(
                        page, arguments.integration_id, revision, arguments.book_key
                    ),
                    "markdown": page.markdown,
                }
            }
        )
    except ValueError:
        return _invalid("page_token")
    except BookStackError as exc:
        return _error(exc)


async def knowledge_create_page(
    arguments: KnowledgeCreatePageArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        chapter_id = None
        if arguments.chapter_token is not None:
            chapter_id = _decode_token(
                _ChapterToken,
                arguments.chapter_token,
                arguments.integration_id,
                revision,
                arguments.book_key,
            ).chapter_id
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            if chapter_id is not None and await _chapter_outside_book(
                client, chapter_id, arguments.book_key
            ):
                return _invalid("chapter_token")
            page = await client.create_page(
                book_id=_book_id(arguments.book_key),
                chapter_id=chapter_id,
                name=arguments.name,
                markdown=arguments.markdown,
            )
        return _result(
            {
                "page": _page_summary(
                    page, arguments.integration_id, revision, arguments.book_key
                )
            }
        )
    except ValueError:
        return _invalid("chapter_token")
    except BookStackError as exc:
        return _error(exc)


async def _page_outside_book(client: Any, page_id: int, book_key: str) -> bool:
    """Check the live book of a page instead of trusting the token.

    Tokens record the book a resource belonged to when it was listed. Moving a
    page in BookStack would otherwise let an approval for one book act on
    content that now lives in another.
    """
    location = await client.read_page_location(page_id)
    return location.book_id != _book_id(book_key)


async def _chapter_outside_book(client: Any, chapter_id: int, book_key: str) -> bool:
    chapter = await client.read_chapter(chapter_id)
    return chapter.book_id != _book_id(book_key)


async def _attachment_outside_book(
    client: Any, attachment_id: int, book_key: str
) -> bool:
    attachment = await client.read_attachment_location(attachment_id)
    return await _page_outside_book(client, attachment.page_id, book_key)


def _page_write_conflict(
    token: Any, existing: BookStackPage, markdown: str | None
) -> Any:
    """Reject content replacements that are based on an outdated page.

    Replacing markdown overwrites the whole page, so it is only safe when the
    agent has seen the current version. The observed revision travels inside the
    page token, which means the check cannot be skipped by the model.
    """
    if markdown is None:
        return None
    from mato_loop_backend.tools.contracts import ToolResult

    observed = getattr(token, "updated_at", None)
    if observed is None:
        return ToolResult.error("integration.staleReference", field="page_token")
    if existing.updated_at is None or existing.updated_at != observed:
        return ToolResult.error("integration.staleReference", field="page_token")
    return None


async def knowledge_update_page(
    arguments: KnowledgeUpdatePageArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _PageToken,
            arguments.page_token,
            arguments.integration_id,
            revision,
            arguments.book_key,
        )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            existing = await client.read_page_location(token.page_id)
            if existing.book_id != _book_id(arguments.book_key):
                return _invalid("page_token")
            conflict = _page_write_conflict(token, existing, arguments.markdown)
            if conflict is not None:
                return conflict
            await client.update_page(
                token.page_id,
                name=arguments.name,
                markdown=arguments.markdown,
            )
        return _result({"updated": True})
    except ValueError:
        return _invalid("page_token")
    except BookStackError as exc:
        return _error(exc)


async def knowledge_delete_page(
    arguments: KnowledgeDeletePageArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _PageToken,
            arguments.page_token,
            arguments.integration_id,
            revision,
            arguments.book_key,
        )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            if await _page_outside_book(client, token.page_id, arguments.book_key):
                return _invalid("page_token")
            await client.delete_page(token.page_id)
        return _result({"deleted": True})
    except ValueError:
        return _invalid("page_token")
    except BookStackError as exc:
        return _error(exc)


async def knowledge_create_book(
    arguments: KnowledgeCreateBookArguments, context: Any
) -> Any:
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            book = await client.create_book(
                name=arguments.name, description=arguments.description
            )
            if arguments.shelf_keys:
                await _apply_shelf_membership(client, book.id, arguments.shelf_keys)
        return _result({"book": _book_payload(book, arguments.shelf_keys or [])})
    except BookStackError as exc:
        return _error(exc)


async def knowledge_update_book(
    arguments: KnowledgeUpdateBookArguments, context: Any
) -> Any:
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            book = await client.update_book(
                _book_id(arguments.book_key),
                name=arguments.name,
                description=arguments.description,
            )
        return _result({"book": _book_payload(book)})
    except BookStackError as exc:
        return _error(exc)


async def knowledge_delete_book(arguments: KnowledgeBookArguments, context: Any) -> Any:
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            await client.delete_book(_book_id(arguments.book_key))
        return _result({"deleted": True})
    except BookStackError as exc:
        return _error(exc)


async def knowledge_create_chapter(
    arguments: KnowledgeCreateChapterArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            chapter = await client.create_chapter(
                book_id=_book_id(arguments.book_key),
                name=arguments.name,
                description=arguments.description,
            )
        return _result(
            {
                "chapter": _chapter_summary(
                    chapter, arguments.integration_id, revision, arguments.book_key
                )
            }
        )
    except BookStackError as exc:
        return _error(exc)


async def knowledge_update_chapter(
    arguments: KnowledgeUpdateChapterArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _ChapterToken,
            arguments.chapter_token,
            arguments.integration_id,
            revision,
            arguments.book_key,
        )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            if await _chapter_outside_book(
                client, token.chapter_id, arguments.book_key
            ):
                return _invalid("chapter_token")
            chapter = await client.update_chapter(
                token.chapter_id,
                name=arguments.name,
                description=arguments.description,
            )
        return _result(
            {
                "chapter": _chapter_summary(
                    chapter, arguments.integration_id, revision, arguments.book_key
                )
            }
        )
    except ValueError:
        return _invalid("chapter_token")
    except BookStackError as exc:
        return _error(exc)


async def knowledge_delete_chapter(
    arguments: KnowledgeDeleteChapterArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _ChapterToken,
            arguments.chapter_token,
            arguments.integration_id,
            revision,
            arguments.book_key,
        )
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            if await _chapter_outside_book(
                client, token.chapter_id, arguments.book_key
            ):
                return _invalid("chapter_token")
            await client.delete_chapter(token.chapter_id)
        return _result({"deleted": True})
    except ValueError:
        return _invalid("chapter_token")
    except BookStackError as exc:
        return _error(exc)


async def knowledge_list_shelves(
    arguments: KnowledgeIntegrationArguments, context: Any
) -> Any:
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            shelves, truncated = await client.list_shelves()
        await _remember_shelves(arguments, context, shelves)
        payload: dict[str, Any] = {
            "shelves": [_shelf_payload(shelf) for shelf in shelves]
        }
        if truncated:
            payload["moreShelvesExist"] = True
        return _result(payload)
    except BookStackError as exc:
        return _error(exc)


async def knowledge_read_shelf(arguments: KnowledgeShelfArguments, context: Any) -> Any:
    try:
        instance, _revision_value = _binding(context, arguments.integration_id)
        blocked = _disabled_book_keys(context, instance)
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            contents = await client.read_shelf(_shelf_id(arguments.shelf_key))
        await _remember_shelves(arguments, context, (contents.shelf,))
        return _result(
            {
                "shelf": _shelf_payload(contents.shelf),
                "books": [
                    _book_payload(book)
                    for book in contents.books
                    if f"book:{book.id}" not in blocked
                ],
            }
        )
    except ValueError:
        return _invalid("integration_id")
    except BookStackError as exc:
        return _error(exc)


async def knowledge_create_shelf(
    arguments: KnowledgeCreateShelfArguments, context: Any
) -> Any:
    try:
        instance, revision = _binding(context, arguments.integration_id)
        blocked = _disabled_book_keys(context, instance)
        refused = _refuse_blocked_books(arguments.book_keys, blocked, "book_keys")
        if refused is not None:
            return refused
        denied = await _authorize_book_writes(
            context,
            instance,
            revision,
            set(arguments.book_keys or ()),
            _operation_key("create-shelf-membership", arguments),
        )
        if denied is not None:
            return denied
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            shelf = await client.create_shelf(
                name=arguments.name,
                description=arguments.description,
                book_ids=_book_ids(arguments.book_keys),
            )
        await _remember_shelves(arguments, context, (shelf,))
        return _result({"shelf": _shelf_payload(shelf)})
    except ValueError:
        return _invalid("integration_id")
    except BookStackError as exc:
        return _error(exc)


async def knowledge_update_shelf(
    arguments: KnowledgeUpdateShelfArguments, context: Any
) -> Any:
    try:
        instance, revision = _binding(context, arguments.integration_id)
        blocked = _disabled_book_keys(context, instance)
        for keys, field in (
            (arguments.book_keys, "book_keys"),
            (arguments.add_book_keys, "add_book_keys"),
            (arguments.remove_book_keys, "remove_book_keys"),
        ):
            refused = _refuse_blocked_books(keys, blocked, field)
            if refused is not None:
                return refused
        shelf_id = _shelf_id(arguments.shelf_key)
        book_ids = _book_ids(arguments.book_keys)
        membership_changes = any(
            keys is not None
            for keys in (
                arguments.book_keys,
                arguments.add_book_keys,
                arguments.remove_book_keys,
            )
        )
        current_ids: tuple[int, ...] = ()
        if membership_changes:
            async with _selected_client(arguments.integration_id, context) as (
                client,
                _revision,
            ):
                contents = await client.read_shelf(shelf_id)
            current_ids = tuple(book.id for book in contents.books)
            if arguments.add_book_keys is not None or arguments.remove_book_keys is not None:
                removed = set(_book_ids(arguments.remove_book_keys) or ())
                merged = [book_id for book_id in current_ids if book_id not in removed]
                for book_id in _book_ids(arguments.add_book_keys) or ():
                    if book_id not in merged:
                        merged.append(book_id)
                book_ids = tuple(merged)
            elif book_ids is not None and blocked:
                # A full replacement would silently drop books the agent is not
                # allowed to see, so keep their membership untouched.
                retained = [
                    book_id
                    for book_id in current_ids
                    if f"book:{book_id}" in blocked
                ]
                book_ids = tuple(
                    list(book_ids) + [b for b in retained if b not in book_ids]
                )
            changed = set(current_ids) ^ set(book_ids or ())
            denied = await _authorize_book_writes(
                context,
                instance,
                revision,
                {f"book:{book_id}" for book_id in changed},
                _operation_key("update-shelf-membership", arguments),
            )
            if denied is not None:
                return denied
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            shelf = await client.update_shelf(
                shelf_id,
                name=arguments.name,
                description=arguments.description,
                book_ids=book_ids,
            )
        await _remember_shelves(arguments, context, (shelf,))
        return _result({"shelf": _shelf_payload(shelf)})
    except ValueError:
        return _invalid("integration_id")
    except BookStackError as exc:
        return _error(exc)


async def knowledge_list_attachments(
    arguments: KnowledgeListAttachmentsArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _PageToken,
            arguments.page_token,
            arguments.integration_id,
            revision,
            arguments.book_key,
        )
    except ValueError:
        return _invalid("page_token")
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            if await _page_outside_book(client, token.page_id, arguments.book_key):
                return _invalid("page_token")
            attachments = await client.list_attachments(token.page_id)
        return _result(
            {
                "attachments": [
                    _attachment_summary(
                        attachment,
                        arguments.integration_id,
                        revision,
                        arguments.book_key,
                    )
                    for attachment in attachments
                ]
            }
        )
    except BookStackError as exc:
        return _error(exc)


async def _download_attachment_source(
    url: str, context: Any
) -> tuple[str, bytes, str] | Any:
    from mato_loop_backend.tools.contracts import ToolResult
    from mato_loop_backend.web.access import (
        authorize_webfetch_target,
        ensure_webfetch_binding_is_current,
    )
    from mato_loop_backend.web.fetch import WebFetchError, download_file

    try:
        await ensure_webfetch_binding_is_current(context)
    except WebFetchError:
        return ToolResult.error("tool.unavailable")
    settings = context.tool_settings.web_access
    public_addresses_only = settings.public_addresses_only

    async def authorize_url(target: str) -> None:
        await authorize_webfetch_target(context, target)
        if context.tool_settings.web_access.public_addresses_only != public_addresses_only:
            raise WebFetchError("tool.unavailable")

    try:
        downloaded = await download_file(
            url,
            public_addresses_only=public_addresses_only,
            authorize_url=authorize_url,
            max_response_bytes=min(
                settings.max_response_mib * 1024 * 1024, MAX_ATTACHMENT_BYTES
            ),
            user_agent=settings.user_agent,
        )
    except WebFetchError as exc:
        if exc.code == "permission.denied":
            return ToolResult.error("permission.denied", field="url")
        return ToolResult.error(exc.code, params=exc.params, field="url")
    return downloaded.filename, downloaded.data, downloaded.content_type


async def _local_attachment_source(
    root: str, path: str, context: Any
) -> tuple[str, bytes, str] | Any:
    import mimetypes
    from pathlib import PurePosixPath

    from mato_loop_backend.execution import ExecutionClientError
    from mato_loop_backend.files.access import (
        delegated_session_files,
        file_path,
        filesystem_permission,
        read_bytes,
    )
    from mato_loop_backend.files.service import RootFileError
    from mato_loop_backend.tools.access import allow
    from mato_loop_backend.tools.contracts import ToolResult

    if delegated_session_files(context, root):
        return ToolResult.error("tool.unavailable")
    try:
        normalized = file_path(context, root, path)
    except (RootFileError, ValueError, OSError):
        return ToolResult.error("file.fileNotFound", field="path")
    try:
        request = filesystem_permission(
            context,
            root=root,
            path=normalized,
            capability="filesystem.read",
            action_kind="read",
            action_label="Datei lesen",
        )
    except (RootFileError, ValueError, OSError):
        return ToolResult.error("file.fileNotFound", field="path")
    if not await allow(context, request):
        return ToolResult.error("permission.denied", field="path")
    try:
        data = await read_bytes(
            context, root, normalized, limit=MAX_ATTACHMENT_BYTES,
        )
    except (RootFileError, ExecutionClientError) as exc:
        return ToolResult.error(
            "integration.attachmentTooLarge" if exc.code == "file.tooLarge" else exc.code,
            field="path",
        )
    except (ValueError, OSError):
        return ToolResult.error("file.fileNotFound", field="path")
    if not data:
        return ToolResult.error("file.fileNotFound", field="path")
    filename = PurePosixPath(normalized).name or "attachment"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return filename, data, content_type


async def knowledge_create_attachment(
    arguments: KnowledgeCreateAttachmentArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _PageToken,
            arguments.page_token,
            arguments.integration_id,
            revision,
            arguments.book_key,
        )
    except ValueError:
        return _invalid("page_token")
    source: tuple[str, bytes, str] | None = None
    if arguments.url is not None:
        resolved = await _download_attachment_source(arguments.url, context)
        if not isinstance(resolved, tuple):
            return resolved
        source = resolved
    elif arguments.root is not None and arguments.path is not None:
        resolved = await _local_attachment_source(
            arguments.root, arguments.path, context
        )
        if not isinstance(resolved, tuple):
            return resolved
        source = resolved
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            if await _page_outside_book(client, token.page_id, arguments.book_key):
                return _invalid("page_token")
            if source is None:
                attachment = await client.create_link_attachment(
                    page_id=token.page_id,
                    name=arguments.name,
                    link=str(arguments.link),
                )
            else:
                filename, data, content_type = source
                attachment = await client.create_file_attachment(
                    page_id=token.page_id,
                    name=arguments.name,
                    filename=filename,
                    data=data,
                    content_type=content_type,
                )
        return _result(
            {
                "attachment": _attachment_summary(
                    attachment, arguments.integration_id, revision, arguments.book_key
                )
            }
        )
    except BookStackError as exc:
        return _error(exc)


async def knowledge_delete_attachment(
    arguments: KnowledgeDeleteAttachmentArguments, context: Any
) -> Any:
    try:
        _instance, revision = _binding(context, arguments.integration_id)
        token = _decode_token(
            _AttachmentToken,
            arguments.attachment_token,
            arguments.integration_id,
            revision,
            arguments.book_key,
        )
    except ValueError:
        return _invalid("attachment_token")
    try:
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            if await _attachment_outside_book(
                client, token.attachment_id, arguments.book_key
            ):
                return _invalid("attachment_token")
            await client.delete_attachment(token.attachment_id)
        return _result({"deleted": True})
    except BookStackError as exc:
        return _error(exc)


async def knowledge_delete_shelf(
    arguments: KnowledgeShelfArguments, context: Any
) -> Any:
    try:
        instance, revision = _binding(context, arguments.integration_id)
        shelf_id = _shelf_id(arguments.shelf_key)
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            contents = await client.read_shelf(shelf_id)
        book_keys = {f"book:{book.id}" for book in contents.books}
        refused = _refuse_blocked_books(
            list(book_keys), _disabled_book_keys(context, instance), "shelf_key"
        )
        if refused is not None:
            return refused
        denied = await _authorize_book_writes(
            context,
            instance,
            revision,
            book_keys,
            _operation_key("delete-shelf-membership", arguments),
        )
        if denied is not None:
            return denied
        async with _selected_client(arguments.integration_id, context) as (
            client,
            _revision,
        ):
            await client.delete_shelf(shelf_id)
        return _result({"deleted": True})
    except BookStackError as exc:
        return _error(exc)


async def load_bookstack_resources(
    instance: IntegrationInstance, credential: dict[str, Any] | None
) -> tuple[IntegrationResource, ...]:
    config = BookStackConfig.model_validate(instance.config)
    if (
        credential is None
        or set(credential) != {"token_id", "token_secret"}
        or not all(isinstance(value, str) and value for value in credential.values())
    ):
        raise BookStackError("bookstack.authenticationFailed")
    async with BookStackClient(
        str(config.base_url), credential["token_id"], credential["token_secret"]
    ) as client:
        books, truncated = await client.list_books()
    if truncated:
        raise BookStackError("bookstack.responseTooLarge")
    return tuple(
        IntegrationResource(
            key=f"book:{book.id}",
            label=_label(book.name),
            binding=book,
        )
        for book in books
    )


def _operation_key(name: str, arguments: BaseModel) -> str:
    material = json.dumps(
        arguments.model_dump(mode="json", exclude_unset=True),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{name}:{hashlib.sha256(material.encode()).hexdigest()}"


def _display_text(value: str | None, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    return f"{text[:limit]}..." if len(text) > limit else text


def _resource_display(
    instance: IntegrationInstance,
    integration_id: UUID,
    resource_key: str | None,
    context: Any,
) -> str:
    account = f'Wissensdatenbank "{_display_text(instance.name)}"'
    if resource_key is None:
        return account
    labels = getattr(getattr(context, "tool_settings", None), "resource_labels", {})
    label = labels.get(str(integration_id), {}).get(resource_key)
    book = f'"{_display_text(label)}"' if label else resource_key
    return f"{book} · {account}"


async def _authorize_book_writes(
    context: Any,
    instance: IntegrationInstance,
    revision: str,
    book_keys: set[str],
    operation_key: str,
) -> Any:
    from mato_loop_backend.permissions.policy import (
        PermissionDecision,
        integration_operation_permission,
    )
    from mato_loop_backend.tools.contracts import ToolResult

    for book_key in sorted(book_keys):
        request = integration_operation_permission(
            capability="knowledge.write",
            integration_id=instance.id,
            binding_revision=revision,
            operation_key=operation_key,
            resource_key=book_key,
            resource_display=_resource_display(
                instance, instance.id, book_key, context
            ),
            title="Buchzuordnung ändern?",
            summary="Der Agent möchte die Regalzuordnung dieses Buchs ändern.",
        )
        if await context.ask(request) != PermissionDecision.ALLOW:
            return ToolResult.error("permission.denied")
    return None


def _permission(
    capability: str,
    operation_key: str,
    arguments: KnowledgeIntegrationArguments,
    context: Any,
    *,
    title: str,
    summary: str,
    resource: bool = False,
    deletion_scope: str | None = None,
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import (
        PermissionDisplayItem,
        integration_operation_permission,
    )

    instance, revision = _binding(context, arguments.integration_id)
    resource_key = getattr(arguments, "book_key", None) if resource else None
    items = []
    for field, model, label, id_field in (
        ("page_token", _PageToken, "Seite", "page_id"),
        ("chapter_token", _ChapterToken, "Kapitel", "chapter_id"),
        ("attachment_token", _AttachmentToken, "Anhang", "attachment_id"),
    ):
        value = getattr(arguments, field, None)
        if value:
            try:
                token = _decode_token(model, value, arguments.integration_id, revision, arguments.book_key)
            except ValueError:
                continue
            name = _display_text(token.name) or label
            items.append(PermissionDisplayItem(label, f"{name} (ID {getattr(token, id_field)})"))
    if isinstance(arguments, KnowledgeShelfArguments):
        labels = getattr(getattr(context, "tool_settings", None), "resource_labels", {})
        name = labels.get(str(instance.id), {}).get(f"{_revision_fingerprint(revision)}:{arguments.shelf_key}")
        shelf = f"{name} ({arguments.shelf_key})" if name else arguments.shelf_key
        items.append(PermissionDisplayItem("Regal", shelf))
    if items:
        for field, label, format in (
            ("name", "Name", "text"), ("description", "Beschreibung", "text"),
            ("markdown", "Inhalt", "code"), ("url", "Quell-URL", "text"),
            ("link", "Link", "text"), ("path", "Quelldatei", "code"), ("root", "Dateibereich", "text"),
        ):
            value = getattr(arguments, field, None)
            if value is not None:
                items.append(PermissionDisplayItem(label, value, format))
        for field, label in (
            ("book_keys", "Bücher (ersetzen)"), ("add_book_keys", "Bücher hinzufügen"),
            ("remove_book_keys", "Bücher entfernen"),
        ):
            keys = getattr(arguments, field, None)
            if keys is not None:
                labels = getattr(getattr(context, "tool_settings", None), "resource_labels", {}).get(str(instance.id), {})
                items.append(PermissionDisplayItem(label, tuple(
                    f"{labels[key]} ({key})" if labels.get(key) else key for key in keys
                ) or ("Liste leeren",), "list"))
    if deletion_scope:
        items.append(PermissionDisplayItem("Löschumfang", deletion_scope))
    return (
        integration_operation_permission(
            capability=capability,
            integration_id=arguments.integration_id,
            binding_revision=revision,
            operation_key=operation_key,
            resource_key=resource_key,
            resource_display=_resource_display(
                instance, arguments.integration_id, resource_key, context
            ),
            title=title,
            summary=summary,
            display_items=tuple(items),
        ),
    )


def _list_books_permission(
    arguments: KnowledgeIntegrationArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.read",
        "list-books",
        arguments,
        context,
        title="Bücher auflisten?",
        summary="Der Agent möchte die verfügbaren Bücher dieser Wissensdatenbank lesen.",
    )


def _search_permission(
    arguments: KnowledgeSearchArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.read",
        _operation_key("search", arguments),
        arguments,
        context,
        title="Wissensdatenbank durchsuchen?",
        summary="Der Agent möchte diese Wissensdatenbank durchsuchen.",
    )


def _read_book_permission(
    arguments: KnowledgeBookArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.read",
        "read-book",
        arguments,
        context,
        title="Buchinhalt lesen?",
        summary="Der Agent möchte Kapitel und Seiten dieses Buchs lesen.",
        resource=True,
    )


def _read_page_permission(
    arguments: KnowledgeReadPageArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.read",
        "read-page",
        arguments,
        context,
        title="Seite lesen?",
        summary="Der Agent möchte eine Seite aus diesem Buch lesen.",
        resource=True,
    )


def _create_page_permission(
    arguments: KnowledgeCreatePageArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.write",
        _operation_key("create-page", arguments),
        arguments,
        context,
        title="Seite erstellen?",
        summary="Der Agent möchte eine Seite in diesem Buch erstellen.",
        resource=True,
    )


def _update_page_permission(
    arguments: KnowledgeUpdatePageArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.write",
        _operation_key("update-page", arguments),
        arguments,
        context,
        title="Seite ändern?",
        summary="Der Agent möchte eine Seite in diesem Buch ändern.",
        resource=True,
    )


def _delete_page_permission(
    arguments: KnowledgeDeletePageArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.delete",
        _operation_key("delete-page", arguments),
        arguments,
        context,
        title="Seite löschen?",
        summary="Der Agent möchte eine Seite aus diesem Buch löschen.",
        resource=True,
    )


def _create_book_permission(
    arguments: KnowledgeCreateBookArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.write",
        _operation_key("create-book", arguments),
        arguments,
        context,
        title="Buch erstellen?",
        summary="Der Agent möchte ein neues Buch in dieser Wissensdatenbank erstellen.",
    )


def _update_book_permission(
    arguments: KnowledgeUpdateBookArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.write",
        _operation_key("update-book", arguments),
        arguments,
        context,
        title="Buch ändern?",
        summary="Der Agent möchte Name oder Beschreibung dieses Buchs ändern.",
        resource=True,
    )


def _delete_book_permission(
    arguments: KnowledgeBookArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.delete",
        _operation_key("delete-book", arguments),
        arguments,
        context,
        title="Buch löschen?",
        summary="Der Agent möchte dieses Buch mit allen Inhalten löschen.",
        resource=True,
        deletion_scope="Buch und alle enthaltenen Inhalte",
    )


def _create_chapter_permission(
    arguments: KnowledgeCreateChapterArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.write",
        _operation_key("create-chapter", arguments),
        arguments,
        context,
        title="Kapitel erstellen?",
        summary="Der Agent möchte ein Kapitel in diesem Buch erstellen.",
        resource=True,
    )


def _update_chapter_permission(
    arguments: KnowledgeUpdateChapterArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.write",
        _operation_key("update-chapter", arguments),
        arguments,
        context,
        title="Kapitel ändern?",
        summary="Der Agent möchte Name oder Beschreibung dieses Kapitels ändern.",
        resource=True,
    )


def _delete_chapter_permission(
    arguments: KnowledgeDeleteChapterArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.delete",
        _operation_key("delete-chapter", arguments),
        arguments,
        context,
        title="Kapitel löschen?",
        summary="Der Agent möchte ein Kapitel mit allen Seiten aus diesem Buch löschen.",
        resource=True,
        deletion_scope="Kapitel und alle enthaltenen Seiten",
    )


def _list_shelves_permission(
    arguments: KnowledgeIntegrationArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.read",
        "list-shelves",
        arguments,
        context,
        title="Regale auflisten?",
        summary="Der Agent möchte die Regale dieser Wissensdatenbank lesen.",
    )


def _read_shelf_permission(
    arguments: KnowledgeShelfArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.read",
        _operation_key("read-shelf", arguments),
        arguments,
        context,
        title="Regalinhalt lesen?",
        summary="Der Agent möchte die Bücher dieses Regals lesen.",
    )


def _create_shelf_permission(
    arguments: KnowledgeCreateShelfArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.write",
        _operation_key("create-shelf", arguments),
        arguments,
        context,
        title="Regal erstellen?",
        summary="Der Agent möchte ein neues Regal in dieser Wissensdatenbank erstellen.",
    )


def _update_shelf_permission(
    arguments: KnowledgeUpdateShelfArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.write",
        _operation_key("update-shelf", arguments),
        arguments,
        context,
        title="Regal ändern?",
        summary=(
            "Der Agent möchte Name, Beschreibung oder die Buchzuordnung "
            "dieses Regals ändern."
        ),
    )


def _list_attachments_permission(
    arguments: KnowledgeListAttachmentsArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.read",
        "list-attachments",
        arguments,
        context,
        title="Anhänge lesen?",
        summary="Der Agent möchte die Anhänge dieser Seite auflisten.",
        resource=True,
    )


def _create_attachment_permission(
    arguments: KnowledgeCreateAttachmentArguments, context: Any
) -> tuple[Any, ...]:
    if arguments.link is not None:
        summary = "Der Agent möchte einen Link als Anhang an diese Seite hängen."
    elif arguments.url is not None:
        summary = (
            "Der Agent möchte eine Datei aus dem Internet herunterladen und "
            "als Anhang an diese Seite hängen."
        )
    else:
        summary = (
            "Der Agent möchte eine lokale Datei als Anhang an diese Seite hängen."
        )
    return _permission(
        "knowledge.write",
        _operation_key("create-attachment", arguments),
        arguments,
        context,
        title="Anhang hinzufügen?",
        summary=summary,
        resource=True,
    )


def _delete_attachment_permission(
    arguments: KnowledgeDeleteAttachmentArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.delete",
        _operation_key("delete-attachment", arguments),
        arguments,
        context,
        title="Anhang löschen?",
        summary="Der Agent möchte diesen Anhang dauerhaft löschen.",
        resource=True,
    )


def _delete_shelf_permission(
    arguments: KnowledgeShelfArguments, context: Any
) -> tuple[Any, ...]:
    return _permission(
        "knowledge.delete",
        _operation_key("delete-shelf", arguments),
        arguments,
        context,
        title="Regal löschen?",
        summary=(
            "Der Agent möchte dieses Regal löschen. "
            "Die enthaltenen Bücher bleiben erhalten."
        ),
    )


def _accounts_permission(    _arguments: KnowledgeListAccountsArguments, _context: Any
) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionRequest

    return (
        PermissionRequest(
            capability="knowledge.read",
            resource_type="network_service",
            resource_scope="knowledge_accounts",
            action_kind="knowledge_list_accounts",
            risk_level="medium",
            title="Wissensdatenbanken auflisten?",
            summary="Der Agent möchte die konfigurierten Wissensdatenbanken lesen.",
            resource_display="Konfigurierte Wissensdatenbanken",
        ),
    )


BOOKSTACK_AGGREGATE_TOOLS = (
    ToolContribution(
        name="knowledge_list_accounts",
        snapshot_policy=ToolSnapshotPolicy("accounts", ("integrationId", "name"), max_age_seconds=None),
        description="List the active BookStack knowledge bases available to this tool binding.",
        arguments_model=KnowledgeListAccountsArguments,
        handler=knowledge_list_accounts,
        label="Wissensdatenbanken auflisten",
        icon="menu_book",
        actions=(_READ,),
        instance_argument=None,
        authorization_factory=_accounts_permission,
        releases_private_data=True,
    ),
)

BOOKSTACK_SHARED_TOOLS = (
    ToolContribution(
        name="knowledge_list_books",
        description=(
            "List the books of one BookStack knowledge base. "
            "Each book reports shelfKeys, its shelf membership; an empty list "
            "means the book is not on any shelf."
        ),
        arguments_model=KnowledgeIntegrationArguments,
        handler=knowledge_list_books,
        label="Bücher auflisten",
        icon="library_books",
        actions=(_READ,),
        instance_argument="integration_id",
        authorization_factory=_list_books_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_search",
        description=(
            "Search books, chapters and pages in one BookStack knowledge base. "
            f"Pass up to {MAX_SEARCH_QUERIES} alternative queries in one call and "
            "cover synonyms, singular and plural forms, abbreviations and both "
            "German and English wording of the topic, because a single query "
            "often misses the relevant pages. Results of all queries are merged "
            "and deduplicated. Returns tokens required by the other knowledge "
            "tools."
        ),
        arguments_model=KnowledgeSearchArguments,
        handler=knowledge_search,
        label="Wissensdatenbank durchsuchen",
        icon="search",
        actions=(_READ,),
        instance_argument="integration_id",
        authorization_factory=_search_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_read_book",
        description=(
            "List the chapters and pages of one book. "
            "The book also reports shelfKeys, its shelf membership; an empty list "
            "means the book is not on any shelf."
        ),
        arguments_model=KnowledgeBookArguments,
        handler=knowledge_read_book,
        label="Buchinhalt lesen",
        icon="menu_book",
        actions=(_READ,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_read_book_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_read_page",
        description=(
            "Read the Markdown content of a previously returned page. "
            "Always call this before knowledge_update_page to get the current content."
        ),
        arguments_model=KnowledgeReadPageArguments,
        handler=knowledge_read_page,
        label="Seite lesen",
        icon="article",
        actions=(_READ,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_read_page_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_create_page",
        description="Create one Markdown page in a book or in one of its chapters.",
        arguments_model=KnowledgeCreatePageArguments,
        handler=knowledge_create_page,
        label="Seite erstellen",
        icon="note_add",
        actions=(_WRITE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_create_page_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_update_page",
        description=(
            "Update title or Markdown content of a previously returned page. "
            "Provided Markdown replaces the whole page content, so it must contain the "
            "complete page, not only the changed part. Call knowledge_read_page "
            "immediately before this tool and edit the Markdown it returns. Never "
            "reconstruct page content from memory or from an earlier tool result. "
            "The response returns the stored Markdown; compare it against the intended "
            "change before reporting success."
        ),
        arguments_model=KnowledgeUpdatePageArguments,
        handler=knowledge_update_page,
        label="Seite bearbeiten",
        icon="edit_note",
        actions=(_WRITE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_update_page_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_delete_page",
        description="Delete a previously returned page.",
        arguments_model=KnowledgeDeletePageArguments,
        handler=knowledge_delete_page,
        label="Seite löschen",
        icon="delete",
        actions=(_DELETE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_delete_page_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_list_attachments",
        description=(
            "List the attachments of one page. Returns the attachmentToken that "
            "knowledge_delete_attachment requires."
        ),
        arguments_model=KnowledgeListAttachmentsArguments,
        handler=knowledge_list_attachments,
        label="Anhänge auflisten",
        icon="attachment",
        actions=(_READ,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_list_attachments_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_create_attachment",
        description=(
            "Attach a file or a link to one page. Provide exactly one source: url "
            "downloads the document from the internet and uploads it to BookStack, "
            "root with path uploads an existing file from files or computer "
            "storage, and link stores the URL as a reference without downloading "
            "anything. Use url for datasheets and manuals the user wants stored "
            "permanently, and link when a reference is enough. Some sites block "
            "automated downloads. If a url download fails, do not retry it "
            "unchanged; attach the same URL as a link instead, and tell the user "
            "why the file itself could not be stored."
        ),
        arguments_model=KnowledgeCreateAttachmentArguments,
        handler=knowledge_create_attachment,
        label="Anhang hinzufügen",
        icon="attach_file",
        actions=(_WRITE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_create_attachment_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_delete_attachment",
        description="Delete a previously returned attachment from its page.",
        arguments_model=KnowledgeDeleteAttachmentArguments,
        handler=knowledge_delete_attachment,
        label="Anhang löschen",
        icon="delete_outline",
        actions=(_DELETE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_delete_attachment_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_create_book",
        description=(
            "Create one new book in a BookStack knowledge base. "
            "Books live inside shelves. Call knowledge_list_shelves first and pass "
            "shelf_keys so the new book is filed where it belongs; a book without "
            "shelf_keys stays unshelved and is hard to find."
        ),
        arguments_model=KnowledgeCreateBookArguments,
        handler=knowledge_create_book,
        label="Buch erstellen",
        icon="library_add",
        actions=(_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_create_book_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_update_book",
        description=(
            "Update name or description of one book. "
            "Use knowledge_update_shelf to change which shelves a book belongs to."
        ),
        arguments_model=KnowledgeUpdateBookArguments,
        handler=knowledge_update_book,
        label="Buch ändern",
        icon="edit_note",
        actions=(_WRITE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_update_book_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_delete_book",
        description="Delete one book including all of its chapters and pages.",
        arguments_model=KnowledgeBookArguments,
        handler=knowledge_delete_book,
        label="Buch löschen",
        icon="delete_forever",
        actions=(_DELETE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_delete_book_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_create_chapter",
        description="Create one chapter in a book.",
        arguments_model=KnowledgeCreateChapterArguments,
        handler=knowledge_create_chapter,
        label="Kapitel erstellen",
        icon="bookmark_add",
        actions=(_WRITE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_create_chapter_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_update_chapter",
        description="Update name or description of a previously returned chapter.",
        arguments_model=KnowledgeUpdateChapterArguments,
        handler=knowledge_update_chapter,
        label="Kapitel ändern",
        icon="edit_note",
        actions=(_WRITE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_update_chapter_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_delete_chapter",
        description="Delete a previously returned chapter including all of its pages.",
        arguments_model=KnowledgeDeleteChapterArguments,
        handler=knowledge_delete_chapter,
        label="Kapitel löschen",
        icon="delete_sweep",
        actions=(_DELETE,),
        instance_argument="integration_id",
        resource_argument="book_key",
        authorization_factory=_delete_chapter_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_list_shelves",
        description="List the shelves of one BookStack knowledge base.",
        arguments_model=KnowledgeIntegrationArguments,
        handler=knowledge_list_shelves,
        label="Regale auflisten",
        icon="shelves",
        actions=(_READ,),
        instance_argument="integration_id",
        authorization_factory=_list_shelves_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_read_shelf",
        description="List the books assigned to one shelf.",
        arguments_model=KnowledgeShelfArguments,
        handler=knowledge_read_shelf,
        label="Regalinhalt lesen",
        icon="shelves",
        actions=(_READ,),
        instance_argument="integration_id",
        authorization_factory=_read_shelf_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_create_shelf",
        description=(
            "Create one shelf. Optionally assign existing books by their book keys."
        ),
        arguments_model=KnowledgeCreateShelfArguments,
        handler=knowledge_create_shelf,
        label="Regal erstellen",
        icon="add_to_photos",
        actions=(_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_create_shelf_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_update_shelf",
        description=(
            "Update name, description or book assignment of one shelf. "
            "Use add_book_keys and remove_book_keys to change single books while "
            "keeping the rest of the shelf untouched. Use book_keys only to replace "
            "the whole assignment; it drops every book you do not list. "
            "book_keys cannot be combined with add_book_keys or remove_book_keys."
        ),
        arguments_model=KnowledgeUpdateShelfArguments,
        handler=knowledge_update_shelf,
        label="Regal bearbeiten",
        icon="edit",
        actions=(_WRITE,),
        instance_argument="integration_id",
        authorization_factory=_update_shelf_permission,
        releases_private_data=True,
    ),
    ToolContribution(
        name="knowledge_delete_shelf",
        description="Delete one shelf. The books it contains are not deleted.",
        arguments_model=KnowledgeShelfArguments,
        handler=knowledge_delete_shelf,
        label="Regal löschen",
        icon="delete_outline",
        actions=(_DELETE,),
        instance_argument="integration_id",
        authorization_factory=_delete_shelf_permission,
        releases_private_data=True,
    ),
)

BOOKSTACK_ADAPTER = IntegrationAdapterDefinition(
    integration_type="knowledge",
    adapter_type="bookstack",
    config_model=BookStackConfig,
    contribution_factory=lambda _instance: IntegrationContributions(),
    resource_actions=(_READ, _WRITE, _DELETE),
    resource_loader=load_bookstack_resources,
    shared_tools=BOOKSTACK_AGGREGATE_TOOLS + BOOKSTACK_SHARED_TOOLS,
)

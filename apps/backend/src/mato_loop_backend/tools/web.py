from __future__ import annotations

import asyncio
import json
from io import BytesIO

import mato_loop_backend.files.access as files_access
import mato_loop_backend.permissions.resolution as permissions_resolution
import mato_loop_backend.tools.access as tools_access
import mato_loop_backend.web.access as web_access
from mato_loop_backend.files.service import FileRoot
from mato_loop_backend.integrations.search import search_web
from mato_loop_backend.integrations.youtube_transcript.client import (
    TranscriptError,
    canonicalize_youtube_video_url,
    fetch_youtube_transcript,
)
from mato_loop_backend.permissions.policy import (
    transcript_permission,
    web_search_permission,
)
from mato_loop_backend.settings import IntegrationSettings, ToolExecutionPolicy
from mato_loop_backend.settings.language import preferred_language
from mato_loop_backend.tools.actions import (
    _SESSION_FILES_WRITE,
    _TRANSCRIPT_ROUTING_INSTRUCTION,
    _WEB_FETCH,
    _WEB_SEARCH,
)
from mato_loop_backend.tools.arguments import (
    DownloadFileArguments,
    FetchTranscriptArguments,
    JinaWebFetchArguments,
    WebFetchArguments,
    WebSearchArguments,
)
from mato_loop_backend.tools.context import ToolContext as ToolContext
from mato_loop_backend.tools.contracts import ToolAttachment as ToolAttachment
from mato_loop_backend.tools.contracts import ToolDefinition as ToolDefinition
from mato_loop_backend.tools.contracts import ToolResult as ToolResult
from mato_loop_backend.tools.registry import _provider_revision as _provider_revision
from mato_loop_backend.web.evidence import evidence_source_id
from mato_loop_backend.web.fetch import (
    WebFetchError,
    canonicalize_url,
    download_file,
    fetch_web,
    normalize_source_title,
)


async def _web_search(arguments: WebSearchArguments, context: ToolContext) -> ToolResult:
    from mato_loop_backend.integrations.registry import agent_profile_search_available

    def binding_matches(
        primary_provider: str,
        integrations: IntegrationSettings,
    ) -> bool:
        return not context.bound_options or (
            primary_provider == context.bound_options.get("provider")
            and _provider_revision(primary_provider, integrations)
            == context.bound_options.get("provider_revision")
        )

    current_integrations = await context.current_integrations()
    current_tools = await tools_access.profile_tool_settings(context)
    if not agent_profile_search_available(current_integrations, current_tools):
        return ToolResult.error("tool.unavailable")
    primary_provider = current_tools.web_search.primary_provider
    if not binding_matches(primary_provider, current_integrations):
        return ToolResult.error("tool.unavailable")
    if not await tools_access.allow(context, web_search_permission()):
        return ToolResult.error("permission.denied")
    current_integrations = await context.current_integrations()
    current_tools = await tools_access.profile_tool_settings(context)
    if not agent_profile_search_available(current_integrations, current_tools):
        return ToolResult.error("tool.unavailable")
    primary_provider = current_tools.web_search.primary_provider
    if not binding_matches(primary_provider, current_integrations):
        return ToolResult.error("tool.unavailable")
    content = await search_web(
        current_integrations,
        arguments.query,
        search_type=arguments.search_type,
        provider=primary_provider,
    )
    try:
        items = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return ToolResult.error("integration.invalidResponse")
    if not isinstance(items, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("sourceId"), str)
        or not isinstance(item.get("url"), str)
        for item in items
    ):
        return ToolResult.error("integration.invalidResponse")
    sources = []
    for item in items:
        source = {
            "sourceId": str(item["sourceId"]),
            "url": str(item["url"]),
            "kind": "search",
        }
        title = normalize_source_title(item.get("title"))
        if title is not None:
            source["title"] = title
        sources.append(source)
    evidence_urls = [str(source["url"]) for source in sources]
    return ToolResult(
        content,
        {
            "resultCount": len(evidence_urls),
            "evidenceUrls": evidence_urls,
            "sources": sources,
        },
    )


async def _webfetch(arguments: WebFetchArguments, context: ToolContext) -> ToolResult:
    from mato_loop_backend.integrations.jina import client as jina_client
    from mato_loop_backend.integrations.jina import service as jina_service
    from mato_loop_backend.integrations.jina.adapter import (
        JinaConfig,
        provider_available,
        provider_key,
        selected_provider,
    )
    from mato_loop_backend.integrations.service import IntegrationSettingsError

    await web_access.ensure_webfetch_binding_is_current(context)
    access = context.tool_settings.web_access.model_copy(deep=True)
    integrations = await context.current_integrations()
    primary = provider_key(integrations)
    revision = _provider_revision(primary, integrations)
    if not provider_available(integrations) or (context.bound_options and (
        context.bound_options.get("provider") != primary
        or context.bound_options.get("provider_revision") != revision
    )):
        raise WebFetchError("tool.unavailable")

    async def authorize_url(url: str) -> None:
        await web_access.authorize_webfetch_target(context, url)
        if context.tool_settings.web_access != access:
            raise WebFetchError("tool.unavailable")
        current = await context.current_integrations()
        if (provider_key(current) != primary
                or _provider_revision(primary, current) != revision
                or not provider_available(current)):
            raise WebFetchError("tool.unavailable")

    provider = None if arguments.pdf_pages else selected_provider(integrations)
    if provider is not None:
        url = canonicalize_url(arguments.url, preserve_fragment=True)
        await authorize_url(url)
        try:
            secrets = await jina_service.credentials(context.db, provider)
        except IntegrationSettingsError as exc:
            raise WebFetchError(exc.code) from exc
        options = arguments.model_dump(exclude=WebFetchArguments.model_fields.keys()) if isinstance(arguments, JinaWebFetchArguments) else {}
        result = await jina_client.fetch(
            JinaConfig.model_validate(provider.config), url, api_key=secrets.api_key,
            cookies=secrets.cookies, locale=await preferred_language(context.db, context.session.user_id),
            format=arguments.format, timeout=arguments.timeout,
            max_response_bytes=access.max_response_mib * 1024 * 1024,
            user_agent=access.user_agent, **options,
        )
    else:
        result = await fetch_web(
            arguments.url,
            format=arguments.format,
            timeout=arguments.timeout or 30,
            public_addresses_only=access.public_addresses_only,
            authorize_url=authorize_url,
            max_response_bytes=access.max_response_mib * 1024 * 1024,
            pdf_vision_handler=context.pdf_vision_handler,
            pdf_pages=arguments.pdf_pages,
            user_agent=access.user_agent,
        )
    if not result.content.strip() and not result.images:
        raise WebFetchError("webfetch.emptyResponse", params={"statusCode": result.status_code})
    canonical_source_url = canonicalize_url(result.final_url, preserve_fragment=True)
    source = {
        "sourceId": evidence_source_id(canonical_source_url),
        "url": canonical_source_url,
        "kind": "webfetch",
        "citationEligible": True,
    }
    title = normalize_source_title(result.title)
    if title is not None:
        source["title"] = title
    attachments = tuple(
        ToolAttachment(
            data=image.data,
            mime_type=image.mime_type,
            filename=image.filename,
            category="downloaded",
            presentation="internal",
            metadata=(
                {"pageNumber": image.page_number}
                if image.page_number is not None
                else {}
            ),
        )
        for image in result.images
    )
    payload = {
        **source,
        "statusCode": result.status_code,
        "contentType": result.content_type,
        "outputFormat": result.output_format,
        "content": result.content,
        **({"links": dict(result.links)} if result.links else {}),
        **({"imageLinks": dict(result.image_links)} if result.image_links else {}),
        "attachments": [
            {
                "filename": image.filename,
                **(
                    {"pageNumber": image.page_number}
                    if image.page_number is not None
                    else {}
                ),
            }
            for image in result.images
        ],
    }
    if result.page_count is not None:
        payload["pageCount"] = result.page_count
        payload["renderedPages"] = [
            image.page_number
            for image in result.images
            if image.page_number is not None
        ]
    if result.unavailable_pages:
        payload["unavailablePages"] = list(result.unavailable_pages)
    return ToolResult(
        json.dumps(payload, ensure_ascii=False),
        {"sources": [source]},
        attachments=attachments,
    )


async def _download_file(
    arguments: DownloadFileArguments, context: ToolContext
) -> ToolResult:
    if files_access.delegated_session_files(context, "session"):
        return ToolResult.error("tool.unavailable")
    await web_access.ensure_webfetch_binding_is_current(context)
    if not context.tool_settings.tool_availability.get("download_file", True):
        return ToolResult.error("tool.unavailable")

    async def authorize_path(path: str) -> bool:
        return await tools_access.allow(context, files_access.filesystem_permission(
            context,
            root="session",
            path=path,
            capability="session.files.write",
            action_kind="download_file",
            action_label="Datei herunterladen",
        ))

    path = (
        context.root_files.normalize_relative_path(arguments.path)
        if arguments.path is not None else None
    )
    if path is not None and await files_access.active_file_is_dirty(context, "session", path):
        return ToolResult.error("file.activeDirty", field="path")
    if path is not None and not await authorize_path(path):
        return ToolResult.error("permission.denied")

    settings = context.tool_settings.web_access
    public_addresses_only = settings.public_addresses_only

    async def authorize_url(url: str) -> None:
        await web_access.authorize_webfetch_target(context, url)
        if (context.tool_settings.web_access.public_addresses_only != public_addresses_only
                or not context.tool_settings.tool_availability.get("download_file", True)):
            raise WebFetchError("tool.unavailable")

    downloaded = await download_file(
        arguments.url,
        public_addresses_only=public_addresses_only,
        authorize_url=authorize_url,
        max_response_bytes=settings.max_response_mib * 1024 * 1024,
        user_agent=settings.user_agent,
    )
    if not context.tool_settings.tool_availability.get("download_file", True):
        return ToolResult.error("tool.unavailable")
    path = path or context.root_files.normalize_relative_path(downloaded.filename)
    if await files_access.active_file_is_dirty(context, "session", path):
        return ToolResult.error("file.activeDirty", field="path")
    if arguments.path is None and not await authorize_path(path):
        return ToolResult.error("permission.denied")
    created = await asyncio.to_thread(
        context.root_files.create_file,
        FileRoot.SESSION,
        path,
        BytesIO(downloaded.data),
        max_bytes=context.root_files.MAX_OPERATION_BYTES,
    )
    return ToolResult.json(
        {"root": "files", "path": created.path, "sizeBytes": created.size},
        file_actions=files_access.regular_file_action(context, "write", "session", created.path),
    )


async def _fetch_transcript(
    arguments: FetchTranscriptArguments, context: ToolContext
) -> ToolResult:
    try:
        source_url = canonicalize_youtube_video_url(arguments.url)
        approved = False

        async def authorize_transcript() -> None:
            nonlocal approved
            await web_access.ensure_webfetch_capability_is_current(context)
            if not approved and permissions_resolution.tool_execution_policy(
                context.tool_settings,
                "network.fetch",
            ) == ToolExecutionPolicy.ASK:
                if not await tools_access.allow(context, transcript_permission(source_url)):
                    raise WebFetchError("permission.denied")
                approved = True
                await web_access.ensure_webfetch_capability_is_current(context)

        await authorize_transcript()
        settings = context.tool_settings.web_access
        public_addresses_only = settings.public_addresses_only

        async def before_request(_url: str) -> None:
            await authorize_transcript()
            if context.tool_settings.web_access.public_addresses_only != public_addresses_only:
                raise WebFetchError("tool.unavailable")

        result = await fetch_youtube_transcript(
            source_url,
            language=arguments.language,
            max_response_bytes=settings.max_response_mib
            * 1024
            * 1024,
            user_agent=settings.user_agent,
            public_addresses_only=public_addresses_only,
            before_request=before_request,
        )
    except (TranscriptError, WebFetchError) as exc:
        return ToolResult.error(exc.code, params=exc.params)

    source = {
        "sourceId": evidence_source_id(result.source_url),
        "url": result.source_url,
        "kind": "transcript",
        "citationEligible": True,
    }
    title = normalize_source_title(result.title)
    if title is not None:
        source["title"] = title
    return ToolResult.json(
        {
            **source,
            "sourceType": "youtube",
            "sourceUrl": result.source_url,
            "language": result.language,
            "languageFallback": result.language_fallback,
            "trackType": result.track_type,
            "outputFormat": "text",
            "content": result.content,
        },
        metadata={"sources": [source]},
    )


WEB_SEARCH = ToolDefinition(
            "web_search",
            "Search the web after resolving any clear catalog skill match. Fetch imageUrl from image "
            "results before use.",
            WebSearchArguments,
            _web_search,
            requires_search=True,
            group="web",
            label="Im Web suchen",
            icon="travel_explore",
            actions=(_WEB_SEARCH,),
        )

WEBFETCH = ToolDefinition(
            "webfetch",
            "Fetch pages/PDFs, validated image attachments or JSON/CSV/XML/RSS/Atom/NDJSON "
            "(verbatim text or escaped HTML). No audio/video/captions.",
            WebFetchArguments,
            _webfetch,
            requires_webfetch=True,
            group="web",
            label="Webseite abrufen",
            icon="language",
            actions=(_WEB_FETCH,),
        )

DOWNLOAD_FILE = ToolDefinition(
            "download_file",
            "Download original document bytes from a URL into session files, without a computer. "
            "Optional path is relative to root=files; otherwise use the server filename. Never overwrite.",
            DownloadFileArguments,
            _download_file,
            requires_webfetch=True,
            group="web",
            label="Datei herunterladen",
            icon="download",
            actions=(_WEB_FETCH, _SESSION_FILES_WRITE),
        )

FETCH_TRANSCRIPT = ToolDefinition(
            "fetch_transcript",
            "Fetch published captions for one supported remote media URL. Use this for transcript "
            "or spoken-content requests about a YouTube URL. Call it only when published captions "
            "can answer the request, never for camera work, visible UI, objects, actions, or other "
            "visual details. It does not inspect video visuals, download media, or transcribe session "
            "or workspace files. Initially supports public YouTube video URLs.",
            FetchTranscriptArguments,
            _fetch_transcript,
            requires_webfetch=True,
            group="web",
            label="Untertitel abrufen",
            icon="subtitles",
            actions=(_WEB_FETCH,),
            prompt_fragments=(_TRANSCRIPT_ROUTING_INSTRUCTION,),
        )

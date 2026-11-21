from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.integration_access import _integration
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.execution.client import ExecutionClientError
from mato_loop_backend.integrations.mcp.adapter import McpConfig
from mato_loop_backend.integrations.mcp.client import McpClientError
from mato_loop_backend.integrations.mcp.oauth import (
    CALLBACK_PATH,
    OAUTH_CREDENTIAL_KIND,
    LoginAttempt,
    OAuthConfig,
    oauth_logins,
    oauth_status,
    validate_oauth_url,
)
from mato_loop_backend.integrations.mcp.service import McpIntegrationService
from mato_loop_backend.integrations.mcp.stdio_service import (
    McpStdioRequest,
    close_session,
    delete_stdio,
    managed_sessions,
    owned_agent_states,
    refresh_stdio,
    save_stdio,
    stdio_payload,
)
from mato_loop_backend.integrations.service import IntegrationSettingsError
from mato_loop_backend.models import UserAccount
from mato_loop_backend.public_url import resolve_public_base_url
from mato_loop_backend.security import (
    hash_token,
    load_credential,
    read_credential,
    require_admin,
    require_user,
)
from mato_loop_backend.settings import (
    GeneralSettings,
    IntegrationAdapterAvailability,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    UserIntegrationSettings,
)

router = APIRouter(prefix="/mcp", dependencies=[Depends(require_user)])
callback_router = APIRouter(prefix="/mcp")
logger = logging.getLogger(__name__)
SettingsApiScope = Literal["instance", "personal"]


class McpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=160)
    endpoint: HttpUrl
    bearer_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=10_000,
        validation_alias=AliasChoices("bearerToken", "bearer_token"),
    )
    remove_bearer_token: bool = Field(
        default=False,
        validation_alias=AliasChoices("removeBearerToken", "remove_bearer_token"),
    )
    enabled: bool = True
    oauth: OAuthConfig | None = None

    @model_validator(mode="after")
    def validate_connection(self) -> McpServerRequest:
        if (
            self.endpoint.username is not None
            or self.endpoint.password is not None
            or self.endpoint.query is not None
            or self.endpoint.fragment is not None
            or (self.bearer_token is not None and self.remove_bearer_token)
            or (
                self.oauth is not None
                and (self.bearer_token is not None or self.remove_bearer_token)
            )
        ):
            raise ValueError("Invalid MCP connection settings")
        return self


class McpServerCreateRequest(McpServerRequest):
    remove_bearer_token: Literal[False] = False


class McpOAuthStartRequest(McpServerCreateRequest):
    oauth: OAuthConfig
    integration_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("integrationId", "integration_id")
    )
    client_secret: str | None = Field(
        default=None,
        min_length=1,
        max_length=10_000,
        validation_alias=AliasChoices("clientSecret", "client_secret"),
    )

    @model_validator(mode="after")
    def validate_oauth(self):
        try:
            validate_oauth_url(str(self.endpoint))
        except McpClientError as exc:
            raise ValueError("Invalid HTTP(S) OAuth endpoint") from exc
        if self.client_secret is not None and self.oauth.token_endpoint_auth_method == "none":
            raise ValueError("An OAuth public client cannot have a client secret")
        return self


class McpSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    personal_allowed: bool = Field(
        validation_alias=AliasChoices("personalAllowed", "personal_allowed")
    )


async def _owner_id(db: AsyncSession, scope: SettingsApiScope, user: UserAccount) -> UUID | None:
    if scope == "instance":
        if not user.is_admin:
            raise ApiError(403, "auth.adminRequired")
        return None
    global_settings = await SettingsStore.load(db, IntegrationSettings)
    availability = global_settings.availability_for("mcp_streamable_http").value
    if availability == "disabled" or (availability != "full" and not user.is_admin):
        raise ApiError(409, "integration.personalAdapterDisabled")
    return user.id


async def _settings(db: AsyncSession, owner_id: UUID | None):
    settings_type = UserIntegrationSettings if owner_id is not None else IntegrationSettings
    settings = await SettingsStore.load(db, settings_type, user_id=owner_id)
    for instance in settings.instances:
        instance.owner_id = owner_id
    return settings


async def _connection_payload(db, instance: IntegrationInstance, *, deleted_computer=False) -> dict:
    if instance.adapter_type == "mcp_stdio":
        return {
            **stdio_payload(instance),
            "deletedComputer": deleted_computer,
            "available": not deleted_computer,
        }
    config = McpConfig.model_validate(instance.config)
    result = {
        "id": str(instance.id),
        "name": instance.name,
        "endpoint": str(config.endpoint),
        "enabled": instance.enabled,
        "hasBearerToken": instance.credential_id is not None and config.oauth is None,
        "oauth": config.oauth.model_dump(mode="json") if config.oauth else None,
        "tools": [tool.api_payload() for tool in config.tools],
    }

    if result["oauth"] is not None:
        result.update(authStatus="authorization_required", hasClientSecret=False)
        if instance.credential_id:
            try:
                credential = await load_credential(
                    db, instance.credential_id, owner_id=instance.owner_id, populate_existing=True
                )
                if credential and credential.kind == OAUTH_CREDENTIAL_KIND:
                    payload = read_credential(credential)
                    result["authStatus"] = oauth_status(payload)
                    result["hasClientSecret"] = bool(
                        payload.get("client_info", {}).get("client_secret")
                    )
            except Exception as exc:
                logger.warning(
                    "Invalid MCP OAuth credential for integration %s: %s",
                    instance.id,
                    type(exc).__name__,
                )
    return result


def _mcp_instance(settings, integration_id: UUID) -> IntegrationInstance:
    instance = _integration(settings, integration_id)
    if instance is None or instance.integration_type != "mcp":
        raise ApiError(404, "mcp.notFound")
    return instance


def _api_error(exc: Exception) -> ApiError:
    code = getattr(exc, "code", "mcp.unavailable")
    status_code = (
        404
        if code in {"mcp.notFound", "mcp.oauthExpired"}
        else 409
        if code in {"integration.changed", "mcp.authorizationRequired", "mcp.oauthBusy"}
        else 413
        if code == "executionAgent.requestTooLarge"
        else 422
        if code
        in {
            "mcp.configurationInvalid",
            "mcp.oauthConfigurationInvalid",
            "mcp.callbackUrlInvalid",
        }
        else 502
    )
    return ApiError(status_code, code)


@router.get("/settings")
async def get_mcp_settings(
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    settings = await SettingsStore.load(db, IntegrationSettings)
    return {
        "personalAllowed": settings.availability_for("mcp_streamable_http")
        == IntegrationAdapterAvailability.FULL
    }


@router.put("/settings")
async def update_mcp_settings(
    payload: McpSettingsRequest,
    _admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    settings = await SettingsStore.load(db, IntegrationSettings, for_update=True)
    settings.adapter_availability["mcp_streamable_http"] = (
        IntegrationAdapterAvailability.FULL
        if payload.personal_allowed
        else IntegrationAdapterAvailability.INSTANCE_ONLY
    )
    await SettingsStore.save(db, settings)
    await db.commit()
    return {"personalAllowed": payload.personal_allowed}


@router.get("/servers")
async def list_mcp_servers(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    scope: SettingsApiScope = "instance",
    user: Annotated[UserAccount, Depends(require_user)] = None,
) -> dict:
    owner_id = await _owner_id(db, scope, user)
    settings = await _settings(db, owner_id)
    owned_states = await owned_agent_states(db, user.id) if any(item.adapter_type == "mcp_stdio" for item in settings.instances) else {}
    return {
        "servers": [
            await _connection_payload(db, instance, deleted_computer=owned_states.get(instance.execution_agent_id, False))
            for instance in settings.instances
            if instance.integration_type == "mcp" and instance.adapter_type in {"mcp_streamable_http", "mcp_stdio"}
            and (instance.execution_agent_id is None or instance.execution_agent_id in owned_states)
        ]
    }


@router.get("/servers/visible")
async def list_visible_mcp_servers(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: Annotated[UserAccount, Depends(require_user)],
) -> dict:
    instance = await SettingsStore.load(db, IntegrationSettings)
    personal = await SettingsStore.load_optional(db, UserIntegrationSettings, user_id=user.id)
    owned_states = await owned_agent_states(db, user.id) if any(item.adapter_type == "mcp_stdio" for settings in (instance, personal) if settings for item in settings.instances) else {}
    for owner_id, settings in ((None, instance), (user.id, personal)):
        if settings:
            for item in settings.instances:
                item.owner_id = owner_id
    return {
        "servers": [
            {
                **await _connection_payload(db, item, deleted_computer=owned_states.get(item.execution_agent_id, False)),
                "scope": scope,
                "available": not owned_states.get(item.execution_agent_id, False) and (
                    instance.availability_for("mcp_streamable_http")
                    != IntegrationAdapterAvailability.DISABLED and (
                        instance.availability_for("mcp_streamable_http") == IntegrationAdapterAvailability.FULL or user.is_admin
                    )
                    if scope == "personal"
                    else instance.availability_for("mcp_streamable_http")
                    != IntegrationAdapterAvailability.DISABLED
                ),
            }
            for scope, settings in (("instance", instance), ("personal", personal))
            if settings is not None
            for item in settings.instances
            if item.integration_type == "mcp" and item.adapter_type in {"mcp_streamable_http", "mcp_stdio"}
            and (item.execution_agent_id is None or item.execution_agent_id in owned_states)
        ]
    }


@router.post("/servers", status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    payload: McpServerCreateRequest | McpStdioRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    scope: SettingsApiScope = "instance",
    user: Annotated[UserAccount, Depends(require_user)] = None,
) -> dict:
    owner_id = await _owner_id(db, scope, user)
    if isinstance(payload, McpStdioRequest):
        try:
            return await _connection_payload(db, await save_stdio(db, payload, owner_id=owner_id, user_id=user.id))
        except (McpClientError, IntegrationSettingsError, ExecutionClientError) as exc:
            await db.rollback()
            raise _api_error(exc) from exc
    if payload.oauth is not None:
        raise ApiError(409, "mcp.authorizationRequired")
    try:
        instance = await McpIntegrationService.create(
            db,
            name=payload.name,
            endpoint=str(payload.endpoint),
            token=payload.bearer_token,
            enabled=payload.enabled,
            owner_id=owner_id,
        )
    except (McpClientError, IntegrationSettingsError) as exc:
        await db.rollback()
        logger.warning("MCP server creation failed with %s", type(exc).__name__)
        raise _api_error(exc) from exc
    return await _connection_payload(db, instance)


@router.put("/servers/{integration_id}")
async def update_mcp_server(
    integration_id: UUID,
    payload: McpServerRequest | McpStdioRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    scope: SettingsApiScope = "instance",
    user: Annotated[UserAccount, Depends(require_user)] = None,
) -> dict:
    owner_id = await _owner_id(db, scope, user)
    current = _mcp_instance(await _settings(db, owner_id), integration_id)
    if isinstance(payload, McpStdioRequest):
        if current.adapter_type != "mcp_stdio":
            raise ApiError(422, "mcp.configurationInvalid")
        try:
            return await _connection_payload(db, await save_stdio(db, payload, owner_id=owner_id, user_id=user.id, integration_id=integration_id))
        except (McpClientError, IntegrationSettingsError, ExecutionClientError) as exc:
            await db.rollback()
            raise _api_error(exc) from exc
    if current.adapter_type != "mcp_streamable_http":
        raise ApiError(422, "mcp.configurationInvalid")
    if payload.oauth is not None:
        if McpConfig.model_validate(current.config).oauth != payload.oauth:
            raise ApiError(409, "mcp.authorizationRequired")
    try:
        instance = await McpIntegrationService.update(
            db,
            integration_id,
            name=payload.name,
            endpoint=str(payload.endpoint),
            token=payload.bearer_token,
            remove_token=payload.remove_bearer_token,
            enabled=payload.enabled,
            owner_id=owner_id,
            remove_oauth="oauth" in payload.model_fields_set and payload.oauth is None,
        )
    except (McpClientError, IntegrationSettingsError) as exc:
        await db.rollback()
        logger.warning("MCP server update failed with %s", type(exc).__name__)
        raise _api_error(exc) from exc
    return await _connection_payload(db, instance)


@router.post("/servers/{integration_id}/refresh")
async def refresh_mcp_server(
    integration_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    scope: SettingsApiScope = "instance",
    user: Annotated[UserAccount, Depends(require_user)] = None,
) -> dict:
    owner_id = await _owner_id(db, scope, user)
    try:
        current = _mcp_instance(await _settings(db, owner_id), integration_id)
        if current.adapter_type == "mcp_stdio":
            return await _connection_payload(db, await refresh_stdio(db, current, user.id))
        return await _connection_payload(
            db, await McpIntegrationService.refresh(db, integration_id, owner_id=owner_id)
        )
    except (McpClientError, IntegrationSettingsError, ExecutionClientError) as exc:
        await db.rollback()
        logger.warning("MCP server refresh failed with %s", type(exc).__name__)
        raise _api_error(exc) from exc


@router.delete("/servers/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    integration_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    scope: SettingsApiScope = "instance",
    user: Annotated[UserAccount, Depends(require_user)] = None,
) -> None:
    owner_id = await _owner_id(db, scope, user)
    try:
        current = _mcp_instance(await _settings(db, owner_id), integration_id)
        if current.adapter_type == "mcp_stdio":
            await delete_stdio(db, current, user.id)
            return
        await McpIntegrationService.delete(db, integration_id, owner_id=owner_id)
    except (IntegrationSettingsError, McpClientError) as exc:
        await db.rollback()
        raise _api_error(exc) from exc


@router.get("/servers/{integration_id}/sessions")
async def list_mcp_sessions(
    integration_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: Annotated[UserAccount, Depends(require_user)],
    scope: SettingsApiScope = "personal",
):
    owner_id = await _owner_id(db, scope, user)
    instance = _mcp_instance(await _settings(db, owner_id), integration_id)
    if instance.adapter_type != "mcp_stdio":
        raise ApiError(422, "mcp.configurationInvalid")
    try:
        return {"sessions": [item.model_dump(mode="json", by_alias=True) for item in await managed_sessions(db, instance, user.id)]}
    except (McpClientError, ExecutionClientError) as exc:
        raise _api_error(exc) from exc


class McpCloseSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process_id: UUID = Field(alias="processId")


@router.post("/servers/{integration_id}/sessions/{session_id}/close")
async def close_mcp_session(
    integration_id: UUID,
    session_id: UUID,
    payload: McpCloseSessionRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: Annotated[UserAccount, Depends(require_user)],
    scope: SettingsApiScope = "personal",
):
    owner_id = await _owner_id(db, scope, user)
    instance = _mcp_instance(await _settings(db, owner_id), integration_id)
    if instance.adapter_type != "mcp_stdio":
        raise ApiError(422, "mcp.configurationInvalid")
    try:
        return await close_session(db, instance, session_id, payload.process_id, user.id)
    except (McpClientError, ExecutionClientError) as exc:
        raise _api_error(exc) from exc


def _session_hash(request: Request) -> str:
    return hash_token(request.cookies.get(get_runtime_config().session_cookie_name, ""))


async def _callback_uri(db: AsyncSession, request: Request) -> str:
    settings = await SettingsStore.load(db, GeneralSettings)
    try:
        base, _source = resolve_public_base_url(settings.public_base_url, request)
        return base + CALLBACK_PATH
    except ValueError as exc:
        raise McpClientError("mcp.callbackUrlInvalid") from exc


@router.post("/oauth/attempts", status_code=status.HTTP_201_CREATED)
async def start_mcp_oauth(
    payload: McpOAuthStartRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: Annotated[UserAccount, Depends(require_user)],
    scope: SettingsApiScope = "instance",
):
    owner_id = await _owner_id(db, scope, user)
    try:
        if (await SettingsStore.load(db, IntegrationSettings)).availability_for(
            "mcp_streamable_http"
        ) == IntegrationAdapterAvailability.DISABLED:
            raise ApiError(409, "integration.adapterDisabled")
        revision = None
        client_info = None
        if payload.integration_id:
            current = _mcp_instance(await _settings(db, owner_id), payload.integration_id)
            if current.adapter_type != "mcp_streamable_http":
                raise McpClientError("mcp.configurationInvalid")
            revision = current.model_dump(mode="json")
            config = McpConfig.model_validate(current.config)
            if (
                current.credential_id
                and str(config.endpoint) == str(payload.endpoint)
                and config.oauth == payload.oauth
                and payload.client_secret is None
            ):
                credential = await load_credential(db, current.credential_id, owner_id=owner_id)
                if credential and credential.kind == OAUTH_CREDENTIAL_KIND:
                    client_info = read_credential(credential).get("client_info")
        if (
            payload.oauth.token_endpoint_auth_method != "none"
            and payload.client_secret is None
            and not (client_info and client_info.get("client_secret"))
        ):
            raise McpClientError("mcp.oauthConfigurationInvalid")
        redirect_uri = await _callback_uri(db, request)
        attempt = LoginAttempt(
            user.id,
            _session_hash(request),
            owner_id,
            {
                "name": payload.name,
                "endpoint": str(payload.endpoint),
                "enabled": payload.enabled,
                "oauth": payload.oauth.model_dump(mode="json"),
            },
            payload.integration_id,
            revision,
        )
        # Do not retain a database transaction while waiting for a browser login.
        await db.rollback()
        await oauth_logins.start(attempt, payload.client_secret, redirect_uri, client_info)
        return {"id": attempt.id, "authorizationUrl": attempt.authorization_url, "expiresIn": 300}
    except McpClientError as exc:
        raise _api_error(exc) from exc


@callback_router.get("/oauth/callback")
async def mcp_oauth_callback(
    state: str = Query(default="", max_length=256),
    code: str | None = Query(default=None, max_length=10_000),
    error: str | None = Query(default=None, max_length=256),
):
    try:
        attempt_id = oauth_logins.receive_callback(state, code, error)
        target = "/settings/mcp?oauthAttempt=" + attempt_id
    except McpClientError:
        target = "/settings/mcp?oauthError=mcp.oauthExpired"
    return RedirectResponse(
        target,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@callback_router.get("/oauth/client-metadata")
async def mcp_oauth_client_metadata(
    request: Request, db: Annotated[AsyncSession, Depends(get_db_session)]
):
    try:
        redirect_uri = await _callback_uri(db, request)
    except McpClientError as exc:
        raise _api_error(exc) from exc
    base = redirect_uri.removesuffix(CALLBACK_PATH)
    return {
        "client_id": base + "/api/mcp/oauth/client-metadata",
        "client_name": "MatoLoop",
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


@router.get("/oauth/attempts/{attempt_id}")
async def mcp_oauth_attempt(
    attempt_id: str, request: Request, user: Annotated[UserAccount, Depends(require_user)]
):
    try:
        attempt = oauth_logins.get(attempt_id, user.id, _session_hash(request))
        if attempt.error_code:
            raise McpClientError(attempt.error_code)
        return {"status": "ready" if attempt.payload is not None else "pending"}
    except McpClientError as exc:
        raise _api_error(exc) from exc


@router.post("/oauth/attempts/{attempt_id}/finish")
async def finish_mcp_oauth(
    attempt_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: Annotated[UserAccount, Depends(require_user)],
):
    attempt = None
    claimed = False
    try:
        attempt = oauth_logins.get(attempt_id, user.id, _session_hash(request))
        await _owner_id(db, "personal" if attempt.owner_id is not None else "instance", user)
        if (await SettingsStore.load(db, IntegrationSettings)).availability_for(
            "mcp_streamable_http"
        ) == IntegrationAdapterAvailability.DISABLED:
            raise ApiError(409, "integration.adapterDisabled")
        if attempt.finishing or attempt.payload is None:
            raise McpClientError(attempt.error_code or "mcp.oauthBusy")
        attempt.finishing = True
        claimed = True
        if attempt.timer:
            attempt.timer.cancel()
        instance = await McpIntegrationService.complete_oauth(db, attempt)
        return await _connection_payload(db, instance)
    except (McpClientError, IntegrationSettingsError) as exc:
        await db.rollback()
        raise _api_error(exc) from exc
    finally:
        if claimed:
            oauth_logins.cancel(attempt_id)


@router.delete("/oauth/attempts/{attempt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_mcp_oauth(
    attempt_id: str, request: Request, user: Annotated[UserAccount, Depends(require_user)]
):
    try:
        attempt = oauth_logins.get(attempt_id, user.id, _session_hash(request))
        if attempt.finishing:
            raise McpClientError("mcp.oauthBusy")
        oauth_logins.cancel(attempt_id)
    except McpClientError as exc:
        raise _api_error(exc) from exc


@router.delete("/servers/{integration_id}/oauth", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_mcp_oauth(
    integration_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: Annotated[UserAccount, Depends(require_user)],
    scope: SettingsApiScope = "instance",
):
    owner_id = await _owner_id(db, scope, user)
    try:
        await McpIntegrationService.disconnect_oauth(db, integration_id, owner_id=owner_id)
    except IntegrationSettingsError as exc:
        await db.rollback()
        raise _api_error(exc) from exc

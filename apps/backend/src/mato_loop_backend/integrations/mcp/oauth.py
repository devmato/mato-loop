from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.utils import issuers_match
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken
from mcp.shared.auth_utils import check_resource_allowed
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.integrations.mcp.client import (
    MAX_MCP_CATALOG_BYTES,
    McpClient,
    McpClientError,
    _BoundedTransport,
)
from mato_loop_backend.models import Credential
from mato_loop_backend.security import (
    load_credential,
    read_credential,
    scoped_credentials,
    store_credential,
)
from mato_loop_backend.settings import IntegrationInstance

OAUTH_CREDENTIAL_KIND = "mcp.oauth"
OAUTH_CLIENT_CREDENTIAL_KIND = "mcp.oauth.client"
LOGIN_TTL = 300
MAX_LOGIN_ATTEMPTS = 32
CALLBACK_PATH = "/api/mcp/oauth/callback"
logger = logging.getLogger(__name__)


class OAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str | None = Field(default=None, min_length=1, max_length=1024)
    token_endpoint_auth_method: Literal["none", "client_secret_basic", "client_secret_post"] = (
        "none"
    )
    scope: str | None = Field(
        default=None, min_length=1, max_length=1024, pattern=r"^[\x21\x23-\x5b\x5d-\x7e ]+$"
    )
    user_scoped: bool = False

    @model_validator(mode="after")
    def validate_client(self):
        if not self.client_id and self.token_endpoint_auth_method != "none":
            raise ValueError("A confidential OAuth client requires a client ID")
        if self.user_scoped and not self.client_id:
            raise ValueError("User-scoped OAuth requires a registered client ID")
        return self


def _is_microsoft_entra_v2_issuer(value: str) -> bool:
    try:
        issuer = httpx.URL(value)
    except (httpx.InvalidURL, TypeError, ValueError):
        return False
    return (
        issuer.host
        in {
            "login.chinacloudapi.cn",
            "login.microsoftonline.com",
            "login.microsoftonline.us",
            "login.partner.microsoftonline.cn",
        }
        and issuer.path.rstrip("/").endswith("/v2.0")
    )


def validate_oauth_url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        raise McpClientError("mcp.oauthConfigurationInvalid") from exc
    if (
        url.scheme not in {"https", "http"}
        or not url.host
        or url.userinfo
        or url.fragment
    ):
        raise McpClientError("mcp.oauthConfigurationInvalid")
    return str(url)


def _redact_callback_log(record):
    if isinstance(record.args, tuple) and len(record.args) == 5:
        address, method, path, version, status = record.args
        if isinstance(path, str) and unquote(path.split("?", 1)[0]) == CALLBACK_PATH:
            record.args = (address, method, CALLBACK_PATH, version, status)
    return True


logging.getLogger("uvicorn.access").addFilter(_redact_callback_log)
# The SDK logs raw token errors and callback state in exception tracebacks.
logging.getLogger("mcp.client.auth.oauth2").addFilter(lambda _record: False)


class _OAuthTransport(_BoundedTransport):
    interactive_auth = True

    def __init__(self, scope: str | None = None, transport=None):
        super().__init__(transport, max_response_bytes=MAX_MCP_CATALOG_BYTES)
        self.scope = scope

    async def handle_async_request(self, request):
        validate_oauth_url(str(request.url))
        response = await super().handle_async_request(request)
        if self.scope and response.status_code == 401:
            # The SDK chooses scopes from the challenge before registration.
            response.headers["www-authenticate"] = (
                f'Bearer scope="{self.scope}", ' + response.headers.get("www-authenticate", "")
            )
        return response


class _LoginProvider(OAuthClientProvider):
    # SDK metadata/token parsers read their own bodies; the MCP probe may stay open as SSE.
    requires_response_body = False
    expected_client_id: str | None = None

    async def _perform_authorization_code_grant(self):
        context = self.context
        metadata = context.oauth_metadata
        if metadata is None or context.client_info is None:
            raise McpClientError("mcp.oauthConfigurationInvalid")
        if self.expected_client_id and context.client_info.client_id != self.expected_client_id:
            raise McpClientError("mcp.oauthConfigurationInvalid")
        for value in (metadata.issuer, metadata.authorization_endpoint, metadata.token_endpoint):
            validate_oauth_url(str(value))
        if (
            metadata.code_challenge_methods_supported is not None
            and "S256" not in metadata.code_challenge_methods_supported
        ):
            raise McpClientError("mcp.oauthConfigurationInvalid")
        method = context.client_info.token_endpoint_auth_method or "none"
        if method not in {"none", "client_secret_basic", "client_secret_post"} or (
            metadata.token_endpoint_auth_methods_supported is not None
            and method not in metadata.token_endpoint_auth_methods_supported
        ):
            raise McpClientError("mcp.oauthConfigurationInvalid")
        if method != "none" and not context.client_info.client_secret:
            raise McpClientError("mcp.oauthConfigurationInvalid")
        if _is_microsoft_entra_v2_issuer(str(metadata.issuer)):
            context.should_include_resource_param = lambda _protocol_version=None: False
            scopes = list(dict.fromkeys((context.client_metadata.scope or "").split()))
            if "offline_access" not in scopes:
                scopes.append("offline_access")
            context.client_metadata.scope = " ".join(scopes)
        context.client_info.issuer = str(metadata.issuer)
        return await super()._perform_authorization_code_grant()


@dataclass
class LoginAttempt:
    user_id: UUID
    session_hash: str
    owner_id: UUID | None
    draft: dict[str, Any]
    integration_id: UUID | None
    revision: dict[str, Any] | None
    bootstrap: bool = False
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    created_at: float = field(default_factory=time.monotonic)
    authorization_url: str | None = None
    state: str | None = None
    error_code: str | None = None
    payload: dict[str, Any] | None = None
    tools: list[dict] | None = None
    task: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    callback: asyncio.Future | None = None
    finishing: bool = False
    timer: asyncio.TimerHandle | None = None
    tokens: OAuthToken | None = None
    expires_at: float | None = None
    client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        if not tokens.access_token or (tokens.expires_in is not None and tokens.expires_in <= 0):
            raise McpClientError("mcp.oauthFailed")
        self.tokens = tokens
        self.expires_at = time.time() + tokens.expires_in if tokens.expires_in is not None else None

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


class OAuthLoginManager:
    def __init__(self):
        # ponytail: process-local login attempts match the single-worker harness.
        self.attempts: dict[str, LoginAttempt] = {}

    def _cleanup(self):
        for key, attempt in list(self.attempts.items()):
            if not attempt.finishing and time.monotonic() - attempt.created_at >= LOGIN_TTL:
                self.cancel(key)

    async def start(
        self,
        attempt: LoginAttempt,
        secret: str | None,
        redirect_uri: str,
        client_info: dict | None = None,
    ):
        self._cleanup()
        for key, previous in list(self.attempts.items()):
            if (
                previous.session_hash == attempt.session_hash
                and previous.user_id == attempt.user_id
            ):
                if previous.finishing:
                    raise McpClientError("mcp.oauthBusy")
                self.cancel(key)
        if len(self.attempts) >= MAX_LOGIN_ATTEMPTS:
            raise McpClientError("mcp.oauthBusy")
        self.attempts[attempt.id] = attempt
        attempt.timer = asyncio.get_running_loop().call_later(LOGIN_TTL, self.cancel, attempt.id)
        attempt.callback = asyncio.get_running_loop().create_future()
        attempt.task = asyncio.create_task(self._login(attempt, secret, redirect_uri, client_info))
        await attempt.ready.wait()
        if attempt.error_code:
            self.cancel(attempt.id)
            raise McpClientError(attempt.error_code)
        if attempt.id not in self.attempts or attempt.authorization_url is None:
            self.cancel(attempt.id)
            raise McpClientError("mcp.oauthExpired")
        return attempt

    def get(self, key: str, user_id: UUID, session_hash: str):
        self._cleanup()
        attempt = self.attempts.get(key)
        if (
            attempt is None
            or attempt.user_id != user_id
            or not secrets.compare_digest(attempt.session_hash, session_hash)
        ):
            raise McpClientError("mcp.oauthExpired")
        return attempt

    def receive_callback(self, state: str, code: str | None, error: str | None):
        self._cleanup()
        attempt = next(
            (
                item
                for item in self.attempts.values()
                if item.state and secrets.compare_digest(item.state.encode(), state.encode())
            ),
            None,
        )
        if (
            attempt is None
            or attempt.callback is None
            or attempt.callback.done()
            or attempt.finishing
        ):
            raise McpClientError("mcp.oauthExpired")
        if error or not code:
            attempt.error_code = "mcp.oauthDenied"
            attempt.callback.set_exception(McpClientError(attempt.error_code))
        else:
            attempt.callback.set_result((code, state))
        return attempt.id

    def cancel(self, key):
        attempt = self.attempts.pop(key, None)
        if attempt:
            if attempt.timer:
                attempt.timer.cancel()
            if attempt.task and not attempt.task.done():
                attempt.task.cancel()
            if attempt.callback and not attempt.callback.done():
                attempt.callback.cancel()
            attempt.payload = None

    async def shutdown(self):
        tasks = [item.task for item in self.attempts.values() if item.task]
        for key in list(self.attempts):
            self.cancel(key)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _login(self, attempt, secret, redirect_uri, client_info):
        from mato_loop_backend.integrations.mcp.adapter import McpConfig

        phase = "configuration"

        async def redirect(url):
            nonlocal phase
            validate_oauth_url(url)
            attempt.state = parse_qs(urlsplit(url).query).get("state", [None])[0]
            if not attempt.state:
                raise McpClientError("mcp.oauthFailed")
            attempt.authorization_url = url
            phase = "authorization"
            attempt.ready.set()

        async def receive_callback():
            nonlocal phase
            result = await attempt.callback
            phase = "token_exchange"
            return result

        try:
            async with asyncio.timeout(LOGIN_TTL):
                endpoint = validate_oauth_url(attempt.draft["endpoint"])
                config = OAuthConfig.model_validate(attempt.draft["oauth"])
                metadata = OAuthClientInformationFull(
                    client_id=config.client_id,
                    client_secret=secret,
                    token_endpoint_auth_method=config.token_endpoint_auth_method,
                    redirect_uris=[redirect_uri],
                    scope=config.scope,
                    client_name="MatoLoop",
                )
                if client_info is not None and secret is None:
                    metadata = OAuthClientInformationFull.model_validate(client_info)
                    metadata.redirect_uris = [AnyUrl(redirect_uri)]
                attempt.client_info = metadata if metadata.client_id else None
                provider = _LoginProvider(
                    endpoint,
                    metadata,
                    attempt,
                    redirect,
                    receive_callback,
                    client_metadata_url=(
                        redirect_uri.removesuffix(CALLBACK_PATH) + "/api/mcp/oauth/client-metadata"
                    )
                    if not config.client_id and redirect_uri.startswith("https://")
                    else None,
                )
                provider.expected_client_id = config.client_id
                phase = "discovery"

                async def metadata_response(response):
                    if response.request.method != "GET" or response.status_code != 200:
                        return
                    await response.aread()
                    try:
                        metadata = OAuthMetadata.model_validate_json(response.content)
                    except ValueError:
                        return  # Protected-resource metadata is parsed by the SDK.
                    if metadata.client_id_metadata_document_supported and provider.context.client_metadata_url:
                        # Prefer the discovered CIMD path over previously registered client IDs.
                        if (
                            provider.context.client_info
                            and provider.context.client_info.client_id
                            != provider.context.client_metadata_url
                        ):
                            provider.context.client_info = None
                    if (
                        metadata.grant_types_supported is not None
                        and "authorization_code" not in metadata.grant_types_supported
                    ):
                        raise McpClientError("mcp.oauthConfigurationInvalid")
                    provider.context.client_metadata.grant_types = ["authorization_code"]
                    if "refresh_token" in (metadata.grant_types_supported or []):
                        provider.context.client_metadata.grant_types.append("refresh_token")

                async with httpx.AsyncClient(
                    auth=provider,
                    transport=_OAuthTransport(config.scope),
                    timeout=httpx.Timeout(30, connect=10),
                    trust_env=False,
                    event_hooks={"response": [metadata_response]},
                ) as client:
                    # Only read-only discovery uses interactive auth; tools/call uses prepared tokens.
                    probe = McpClient(endpoint, http_client=client)
                    tools = await probe.discover_tools(
                        read_timeout_seconds=LOGIN_TTL
                    )
                    endpoint = probe.endpoint
                    attempt.draft["endpoint"] = endpoint
                if (
                    attempt.tokens is None
                    or attempt.client_info is None
                    or provider.context.oauth_metadata is None
                ):
                    raise McpClientError("mcp.oauthFailed")
                metadata = provider.context.oauth_metadata
                resource = provider.context.get_resource_url()
                phase = "catalog_validation"
                attempt.tools = McpConfig(endpoint=endpoint, tools=tools).model_dump(mode="json")[
                    "tools"
                ]
                attempt.payload = {
                    "endpoint": endpoint,
                    "issuer": str(metadata.issuer),
                    "resource": resource,
                    "token_endpoint": str(metadata.token_endpoint),
                    "client_info": attempt.client_info.model_dump(mode="json"),
                    "tokens": attempt.tokens.model_dump(mode="json"),
                    "expires_at": attempt.expires_at,
                }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt.error_code = exc.code if isinstance(exc, McpClientError) else "mcp.oauthFailed"
            validation = []
            exceptions = []
            pending = [exc]
            for _ in range(20):
                if not pending:
                    break
                error = pending.pop()
                exceptions.append(type(error).__name__)
                if isinstance(error, ValidationError):
                    validation.extend(
                        {"loc": item["loc"], "type": item["type"]}
                        for item in error.errors(
                            include_input=False, include_context=False, include_url=False
                        )[:20 - len(validation)]
                    )
                if isinstance(error, BaseExceptionGroup):
                    pending.extend(error.exceptions[:20])
                cause = error.__cause__ or error.__context__
                if cause:
                    pending.append(cause)
            logger.warning(
                "MCP OAuth login failed (phase=%s, code=%s, exceptions=%s, validation=%s)",
                phase,
                attempt.error_code,
                json.dumps(exceptions),
                json.dumps(validation),
            )
        finally:
            attempt.ready.set()


oauth_logins = OAuthLoginManager()


def oauth_status(payload: dict[str, Any]) -> str:
    if payload.get("authorization_required") or not payload.get("tokens", {}).get("access_token"):
        return "authorization_required"
    expires = payload.get("expires_at")
    if (
        expires is not None
        and expires <= time.time()
        and not payload.get("tokens", {}).get("refresh_token")
    ):
        return "authorization_required"
    return "authorized"


async def refresh_oauth_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if oauth_status(payload) != "authorized":
        raise McpClientError("mcp.authorizationRequired")
    expires = payload.get("expires_at")
    if expires is None or expires > time.time():
        return payload
    client = OAuthClientInformationFull.model_validate(payload["client_info"])
    if (
        not client.client_id
        or not client.issuer
        or not issuers_match(client.issuer, payload["issuer"])
    ):
        raise McpClientError("mcp.oauthConfigurationInvalid")
    method = client.token_endpoint_auth_method or "none"
    if method not in {"none", "client_secret_basic", "client_secret_post"} or (
        method != "none" and not client.client_secret
    ):
        raise McpClientError("mcp.oauthConfigurationInvalid")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": payload["tokens"]["refresh_token"],
        "client_id": client.client_id,
    }
    if not _is_microsoft_entra_v2_issuer(payload["issuer"]):
        data["resource"] = payload["resource"]
    auth = None
    if method == "client_secret_basic":
        auth = httpx.BasicAuth(
            quote(client.client_id, safe=""), quote(client.client_secret, safe="")
        )
    elif method == "client_secret_post":
        data["client_secret"] = client.client_secret
    try:
        async with httpx.AsyncClient(
            transport=_OAuthTransport(), timeout=httpx.Timeout(30, connect=10), trust_env=False
        ) as http:
            response = await http.post(
                validate_oauth_url(payload["token_endpoint"]), data=data, auth=auth
            )
        if response.status_code in {400, 401, 403}:
            raise McpClientError("mcp.authorizationRequired")
        response.raise_for_status()
        tokens = OAuthToken.model_validate_json(response.content)
        if not tokens.access_token or (tokens.expires_in is not None and tokens.expires_in <= 0):
            raise ValueError("Invalid OAuth token lifetime")
        tokens.refresh_token = tokens.refresh_token or payload["tokens"]["refresh_token"]
        return {
            **payload,
            "tokens": tokens.model_dump(mode="json"),
            "expires_at": time.time() + tokens.expires_in
            if tokens.expires_in is not None
            else None,
        }
    except McpClientError:
        raise
    except Exception as exc:
        raise McpClientError("mcp.unavailable") from exc


async def _save_oauth_credential(db, credential, payload):
    if credential.metadata_json.get("integration_id"):
        payload = oauth_payload_without_shared_secret(payload)
    await store_credential(
        db,
        owner_id=credential.user_id,
        kind=credential.kind,
        label=credential.label,
        payload=payload,
        credential=credential,
    )
    await db.commit()


async def rebind_oauth_endpoint(db, instance: IntegrationInstance, endpoint: str) -> None:
    """Stage a validated binding change in the connection's transaction."""
    credential = await db.scalar(
        scoped_credentials(instance.owner_id)
        .where(Credential.id == instance.credential_id)
        .with_for_update()
    )
    if credential is None or credential.kind != OAUTH_CREDENTIAL_KIND:
        raise McpClientError("mcp.authorizationRequired")
    try:
        payload = read_credential(credential)
        endpoint = validate_oauth_url(endpoint)
        if (httpx.URL(payload["endpoint"]) != httpx.URL(instance.config["endpoint"])
            or not check_resource_allowed(requested_resource=endpoint, configured_resource=payload["resource"])):
            raise McpClientError("mcp.oauthConfigurationInvalid")
        if oauth_status(payload) != "authorized":
            raise McpClientError("mcp.authorizationRequired")
    except McpClientError:
        raise
    except Exception as exc:
        raise McpClientError("mcp.oauthConfigurationInvalid") from exc
    await store_credential(
        db, owner_id=credential.user_id, kind=credential.kind, label=credential.label,
        payload={**payload, "endpoint": endpoint}, credential=credential,
    )


def user_scoped_oauth(instance: IntegrationInstance) -> bool:
    config = instance.config.get("oauth")
    return isinstance(config, dict) and config.get("user_scoped") is True


async def find_user_oauth_credential(
    db: AsyncSession, instance: IntegrationInstance, user_id: UUID, *, for_update: bool = False
) -> Credential | None:
    statement = (
        scoped_credentials(user_id)
        .where(
            Credential.kind == OAUTH_CREDENTIAL_KIND,
            Credential.metadata_json["integration_id"].astext == str(instance.id),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update()
    return await db.scalar(statement)


async def lock_user_oauth_credential(
    db: AsyncSession, integration_id: UUID, user_id: UUID
) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"mcp.oauth:{integration_id}:{user_id}"},
    )


def oauth_payload_without_shared_secret(payload: dict[str, Any]) -> dict[str, Any]:
    result = {**payload}
    client_info = dict(payload.get("client_info") or {})
    client_info.pop("client_secret", None)
    result["client_info"] = client_info
    return result


async def delete_user_oauth_credentials(
    db: AsyncSession, integration_id: UUID, *, user_id: UUID | None = None
) -> int:
    statement = select(Credential).where(
        Credential.kind == OAUTH_CREDENTIAL_KIND,
        Credential.metadata_json["integration_id"].astext == str(integration_id),
    )
    if user_id is not None:
        statement = statement.where(Credential.user_id == user_id)
    credentials = list((await db.scalars(statement)).all())
    for credential in credentials:
        await db.delete(credential)
    return len(credentials)


def client_template_catalog_ready(payload: dict[str, Any], instance: IntegrationInstance) -> bool:
    initialized = payload.get("catalog_initialized")
    if isinstance(initialized, bool):
        return initialized
    return bool(instance.config.get("tools"))


async def oauth_token(instance: IntegrationInstance, user_id: UUID | None = None) -> str:
    if instance.credential_id is None:
        raise McpClientError("mcp.authorizationRequired")
    # A separate transaction commits only OAuth state, not a caller's pending work.
    async with AsyncSessionFactory() as db:
        if user_scoped_oauth(instance):
            if user_id is None:
                raise McpClientError("mcp.authorizationRequired")
            credential = await find_user_oauth_credential(
                db, instance, user_id, for_update=True
            )
        else:
            credential = await db.scalar(
                scoped_credentials(instance.owner_id)
                .where(Credential.id == instance.credential_id)
                .with_for_update()
            )
        if credential is None or credential.kind != OAUTH_CREDENTIAL_KIND:
            raise McpClientError("mcp.authorizationRequired")
        try:
            payload = read_credential(credential)
            if user_scoped_oauth(instance):
                template = await load_credential(
                    db, instance.credential_id, owner_id=None, populate_existing=True
                )
                if template is None or template.kind != OAUTH_CLIENT_CREDENTIAL_KIND:
                    raise McpClientError("mcp.oauthConfigurationInvalid")
                client_template = {
                    key: value
                    for key, value in (
                        read_credential(template).get("client_info") or {}
                    ).items()
                    if key != "redirect_uris"
                }
                payload = {
                    **payload,
                    "client_info": {
                        **payload.get("client_info", {}),
                        **client_template,
                    },
                }
            if str(httpx.URL(payload["endpoint"])) != str(
                httpx.URL(instance.config["endpoint"])
            ) or not check_resource_allowed(
                requested_resource=str(httpx.URL(instance.config["endpoint"])),
                configured_resource=payload["resource"],
            ):
                raise McpClientError("mcp.oauthConfigurationInvalid")
            refreshed = await refresh_oauth_payload(payload)
        except McpClientError as exc:
            if exc.code == "mcp.authorizationRequired":
                await _save_oauth_credential(
                    db, credential, {**payload, "authorization_required": True}
                )
            raise
        except Exception as exc:
            raise McpClientError("mcp.oauthConfigurationInvalid") from exc
        if refreshed != payload:
            await _save_oauth_credential(db, credential, refreshed)
        return refreshed["tokens"]["access_token"]


async def mark_authorization_required(
    instance: IntegrationInstance, token: str, user_id: UUID | None = None
):
    async with AsyncSessionFactory() as db:
        if user_scoped_oauth(instance):
            if user_id is None:
                return
            credential = await find_user_oauth_credential(
                db, instance, user_id, for_update=True
            )
        else:
            credential = await db.scalar(
                scoped_credentials(instance.owner_id)
                .where(Credential.id == instance.credential_id)
                .with_for_update()
            )
        if credential is not None and credential.kind == OAUTH_CREDENTIAL_KIND:
            payload = read_credential(credential)
            if payload.get("tokens", {}).get("access_token") != token:
                return
            await _save_oauth_credential(
                db, credential, {**payload, "authorization_required": True}
            )


async def connection_token(
    db, instance: IntegrationInstance, *, user_id: UUID | None = None
) -> str | None:
    if instance.config.get("oauth") is not None:
        return await oauth_token(instance, user_id)
    if instance.credential_id is None:
        return None
    try:
        credential = await load_credential(
            db, instance.credential_id, owner_id=instance.owner_id, populate_existing=True
        )
        if credential is None or credential.kind != "mcp.bearer":
            raise ValueError("Invalid MCP credential")
        token = read_credential(credential).get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("Invalid MCP token")
        return token
    except Exception as exc:
        raise McpClientError("mcp.configurationInvalid") from exc

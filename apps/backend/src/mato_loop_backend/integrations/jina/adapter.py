from __future__ import annotations

from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from mato_loop_backend.integrations.contracts import IntegrationAdapterDefinition
from mato_loop_backend.settings import IntegrationInstance, IntegrationSettings

JINA_CREDENTIAL_KIND = "jina.api_key"


class JinaOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: HttpUrl
    timeout_seconds: int = Field(default=90, ge=1, le=120)
    remove_overlay: bool = False

    @model_validator(mode="after")
    def validate_url(self) -> JinaOptions:
        if (
            self.base_url.username is not None
            or self.base_url.password is not None
            or self.base_url.query is not None
            or self.base_url.fragment is not None
        ):
            raise ValueError("Jina URL must not contain credentials, query, or fragment")
        return self


class JinaConfig(JinaOptions):
    cookie_domains: list[str] = Field(default_factory=list)
    has_api_key: bool = False


class JinaCookie(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9]+(?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
    name: str = Field(min_length=1, max_length=256, pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
    value: str = Field(min_length=1, max_length=8192, pattern=r'^[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]+$')

    @property
    def reader_value_supported(self) -> bool:
        # shortcut: Reader decodes and re-encodes values, relax after byte-preserving cookie support.
        return quote(unquote(self.value), safe="!~*'()") == self.value

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")):
            raise ValueError("Cookie domain must be an exact hostname")
        return value.lower()


class JinaCredentials(BaseModel):
    api_key: str | None = Field(default=None, max_length=10_000, pattern=r"^[\x21-\x7e]+$")
    cookies: list[JinaCookie] = Field(default_factory=list, max_length=50)


def selected_provider(settings: IntegrationSettings, key: str | None = None) -> IntegrationInstance | None:
    key = key or provider_key(settings)
    return next((
        item for item in settings.instances
        if f"jina:{item.id}" == key
        and item.integration_type == "webfetch" and item.adapter_type == "jina"
        and item.enabled and item.owner_id is None
        and settings.availability_for("jina") != "disabled"
    ), None)


def provider_key(settings: IntegrationSettings) -> str:
    return f"jina:{settings.webfetch_integration_id}" if settings.webfetch_integration_id else "builtin"


def provider_available(settings: IntegrationSettings) -> bool:
    return settings.webfetch_integration_id is None or selected_provider(settings) is not None


def provider_options(settings: IntegrationSettings) -> list[dict]:
    return [{"value": "builtin", "name": "Eingebaut"}, *(
        {"value": f"jina:{item.id}", "name": item.name}
        for item in settings.instances
        if selected_provider(settings, f"jina:{item.id}") is not None
    )]


JINA_ADAPTER = IntegrationAdapterDefinition(
    integration_type="webfetch",
    adapter_type="jina",
    config_model=JinaConfig,
    instance_only=True,
)

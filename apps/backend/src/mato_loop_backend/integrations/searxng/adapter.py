from __future__ import annotations

from pydantic import BaseModel, HttpUrl

from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    ToolOptionContribution,
)
from mato_loop_backend.settings.models import IntegrationInstance


class SearxngConfig(BaseModel):
    base_url: HttpUrl


def search_provider_key(instance: IntegrationInstance) -> str:
    return f"{instance.adapter_type}:{instance.id}"


def _contributions(instance: IntegrationInstance) -> IntegrationContributions:
    return IntegrationContributions(
        tool_options=(
            ToolOptionContribution(
                tool_name="web_search",
                option_name="provider",
                value=search_provider_key(instance),
                label=instance.name,
                integration_id=str(instance.id),
            ),
        )
    )


SEARXNG_ADAPTER = IntegrationAdapterDefinition(
    integration_type="search",
    adapter_type="searxng",
    config_model=SearxngConfig,
    contribution_factory=_contributions,
    instance_only=True,
)

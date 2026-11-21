"""Bounded cache-artifact transfers from execution agents."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from pydantic import Field, field_validator

from mato_loop_backend.execution.client import (
    ExecutionClient,
    ExecutionClientError,
    ExecutionResult,
)
from mato_loop_backend.execution.manager import ExecutionAgentError, execution_agent_connections
from mato_loop_backend.execution.protocol import WireModel
from mato_loop_backend.execution.transfers import (
    EXPORT_TIMEOUT_SECONDS,
    MAX_TRANSFER_BYTES,
    transfer_tickets,
)


class ArtifactDescriptor(WireModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    backend_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    path: str
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1, le=MAX_TRANSFER_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=255)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        parsed = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or parsed.is_absolute()
            or parsed.as_posix() != value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
        ):
            raise ValueError("Invalid artifact path")
        return value

    @field_validator("filename")
    @classmethod
    def plain_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
            raise ValueError("Invalid artifact filename")
        return value


class CacheExportResult(ExecutionResult):
    size: int = Field(ge=1, le=MAX_TRANSFER_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


async def export_artifact(
    client: ExecutionClient,
    artifact: ArtifactDescriptor,
    *,
    limit: int,
    timeout: float = EXPORT_TIMEOUT_SECONDS,
    ttl_seconds: float | None = EXPORT_TIMEOUT_SECONDS + 30,
    too_large_code: str = "executionAgent.protocolError",
) -> bytes:
    """Export an artifact after its caller validates the source computer and session."""
    if not 0 < artifact.size <= limit <= MAX_TRANSFER_BYTES:
        raise ExecutionClientError(too_large_code)
    grant, ticket = transfer_tickets.create_upload(
        client.agent_id,
        size=artifact.size,
        sha256=artifact.sha256,
        ttl_seconds=ttl_seconds,
    )
    try:
        try:
            url = execution_agent_connections.transfer_url(client.agent_id, grant.ticket_id)
        except ExecutionAgentError as exc:
            raise ExecutionClientError(exc.code) from exc
        result = await client.request(
            "cache.export",
            {
                "artifact": artifact.model_dump(mode="json", by_alias=True),
                "transfer": {"url": url, "token": grant.token},
            },
            CacheExportResult,
            timeout=timeout,
        )
        if (
            result.size != artifact.size
            or result.sha256 != artifact.sha256
            or ticket.actual_size != artifact.size
            or ticket.actual_sha256 != artifact.sha256
            or ticket.content is None
        ):
            raise ExecutionClientError("executionAgent.protocolError")
        data = ticket.content.read(limit + 1)
        if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ExecutionClientError("executionAgent.protocolError")
        return data
    finally:
        # Claimed upload tickets are removed from the store before receiving bytes.
        if ticket.content is not None:
            ticket.content.close()
        transfer_tickets.revoke(grant.ticket_id)

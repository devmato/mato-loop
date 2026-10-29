from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mato_loop_backend.db.base import Base
from mato_loop_backend.settings.models import GENERAL_PROFILE_ID, TrustLevel

if TYPE_CHECKING:
    from mato_loop_backend.models.project import Project


class WebSearchPolicy(StrEnum):
    REQUIRED = "required"
    AVAILABLE = "available"
    DISABLED = "disabled"


class TitleGenerationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        CheckConstraint(
            "trust_level IS NULL OR trust_level IN ('public', 'private')",
            name="trust_level_valid",
        ),
        CheckConstraint(
            "web_search_policy IN ('required', 'available', 'disabled')",
            name="web_search_policy_valid",
        ),
        CheckConstraint(
            "memory_mode IN ('full', 'read_only', 'disabled')",
            name="memory_mode_valid",
        ),
        CheckConstraint(
            "reasoning_effort IS NULL OR reasoning_effort IN "
            "('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')",
            name="reasoning_effort_valid",
        ),
        CheckConstraint(
            "title_generation_status IN "
            "('pending', 'running', 'completed', 'failed', 'skipped')",
            name="title_generation_status_valid",
        ),
        CheckConstraint(
            "session_type IN ('chat', 'automation')", name="session_type_valid"
        ),
        CheckConstraint(
            "project_id IS NULL OR session_type = 'chat'",
            name="project_chat_sessions_only",
        ),
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.user_id"],
            name="fk_chat_sessions_project_owner",
        ),
        UniqueConstraint("id", "user_id"),
        ForeignKeyConstraint(
            ["parent_session_id", "user_id"],
            ["chat_sessions.id", "chat_sessions.user_id"],
            name="fk_chat_sessions_parent_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(parent_session_id IS NULL AND spawned_by_run_id IS NULL "
            "AND spawned_by_tool_call_id IS NULL AND spawn_index IS NULL) OR "
            "(parent_session_id IS NOT NULL AND spawned_by_run_id IS NOT NULL "
            "AND spawned_by_tool_call_id IS NOT NULL AND spawn_index IS NOT NULL)",
            name="spawn_shape_valid",
        ),
        CheckConstraint("parent_session_id IS NULL OR parent_session_id <> id", name="parent_not_self"),
        CheckConstraint("spawn_index IS NULL OR spawn_index >= 0", name="spawn_index_nonnegative"),
        UniqueConstraint("spawned_by_run_id", "spawned_by_tool_call_id", "spawn_index"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    parent_session_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    spawned_by_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE", use_alter=True), nullable=True
    )
    spawned_by_tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    spawn_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False, default="Neue Session")
    session_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="chat", server_default=text("'chat'")
    )
    title_generation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TitleGenerationStatus.PENDING.value
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    control_center: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {
            "tabs": [
                {"app_id": "session", "pinned": False},
                {"app_id": "files", "pinned": False},
            ],
            "active_app_id": "session",
        },
        server_default=text(
            "jsonb_build_object("
            "'tabs', jsonb_build_array("
            "jsonb_build_object('app_id', 'session', 'pinned', false), "
            "jsonb_build_object('app_id', 'files', 'pinned', false)), "
            "'active_app_id', 'session')"
        ),
    )
    model_config_id: Mapped[UUID | None] = mapped_column(nullable=True)
    trust_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    session_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    agent_profile_id: Mapped[UUID] = mapped_column(
        nullable=False,
        default=GENERAL_PROFILE_ID,
        server_default=text(f"'{GENERAL_PROFILE_ID}'::uuid"),
    )
    reasoning_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
    memory_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="full")
    web_search_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WebSearchPolicy.AVAILABLE.value
    )
    preferred_execution_agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("execution_agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    current_directory: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    workspace_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="local", server_default="local"
    )
    coding_workspace: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    private_integration_data_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    support_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="chat_session", cascade="all, delete-orphan", passive_deletes=True
    )
    context_summaries: Mapped[list[ContextSummary]] = relationship(
        back_populates="chat_session", cascade="all, delete-orphan", passive_deletes=True
    )
    project: Mapped[Project | None] = relationship()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "trust_level IN ('public', 'private')", name="trust_level_valid"
        ),
        CheckConstraint(
            "generation_status IN ('completed', 'interrupted')",
            name="generation_status_valid",
        ),
        CheckConstraint(
            "(error_code IS NULL AND error_params IS NULL AND error_run_id IS NULL) OR "
            "(error_code IS NOT NULL AND role = 'assistant' AND error_run_id IS NOT NULL)",
            name="message_error_valid",
        ),
        CheckConstraint(
            "(role <> 'user' AND source_type IS NULL AND source_provider IS NULL "
            "AND source_endpoint_id IS NULL) OR "
            "(role = 'user' AND source_type IS NULL AND source_provider IS NULL "
            "AND source_endpoint_id IS NULL) OR "
            "(role = 'user' AND source_type = 'app' AND source_provider IS NULL "
            "AND source_endpoint_id IS NULL) OR "
            "(role = 'user' AND source_type = 'endpoint' AND source_provider IS NOT NULL "
            "AND source_endpoint_id IS NOT NULL)",
            name="message_source_valid",
        ),
        UniqueConstraint("error_run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    generation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="completed"
    )
    trust_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TrustLevel.PUBLIC.value, index=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    provider_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source_endpoint_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    public_reasoning: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    chat_session: Mapped[ChatSession] = relationship(back_populates="messages")
    attachments: Mapped[list[MessageAttachment]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="MessageAttachment.position",
    )
    search_chunks: Mapped[list[MessageSearchChunk]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="MessageSearchChunk.chunk_index",
    )


class MessageAttachment(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint(
            "kind IN ('image', 'audio', 'video', 'text', 'document', 'file')",
            name="kind_valid",
        ),
        CheckConstraint(
            "category IN ('user_upload', 'generated', 'downloaded')",
            name="category_valid",
        ),
        CheckConstraint(
            "purpose IN ('attachment', 'artifact')",
            name="purpose_valid",
        ),
        CheckConstraint(
            "processing_status IS NULL OR processing_status IN ('pending', 'ready', 'failed')",
            name="processing_status_valid",
        ),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint("width IS NULL OR width > 0", name="width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="height_positive"),
        CheckConstraint("duration_ms IS NULL OR duration_ms > 0", name="duration_ms_positive"),
        CheckConstraint(
            "file_revision IS NULL OR file_revision > 0",
            name="file_revision_positive",
        ),
        UniqueConstraint("message_id", "position"),
        CheckConstraint(
            "file_root_id IS NOT NULL AND file_id IS NOT NULL "
            "AND file_revision IS NOT NULL AND file_path IS NOT NULL",
            name="message_attachment_file_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="image")
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user_upload", server_default="user_upload"
    )
    purpose: Mapped[str] = mapped_column(
        String(20), nullable=False, default="attachment", server_default="attachment"
    )
    processing_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_root_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    file_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    file_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    message: Mapped[Message] = relationship(back_populates="attachments")


class MessageSearchChunk(Base):
    __tablename__ = "message_search_chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint(
            "(status = 'ready' AND embedding IS NOT NULL) OR "
            "(status <> 'ready' AND embedding IS NULL)",
            name="ready_vector_valid",
        ),
        UniqueConstraint("message_id", "chunk_index"),
        Index("ix_message_search_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_message_search_chunks_queue", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple'::regconfig, coalesce(content, ''))", persisted=True),
        nullable=False,
    )
    profile_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    vector: Mapped[Any | None] = mapped_column("embedding", Vector(384), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    message: Mapped[Message] = relationship(back_populates="search_chunks")


class ContextSummary(Base):
    __tablename__ = "context_summaries"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    covered_through_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    previous_summary_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("context_summaries.id", ondelete="SET NULL"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    model_config_id: Mapped[UUID | None] = mapped_column(nullable=True)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    chat_session: Mapped[ChatSession] = relationship(back_populates="context_summaries")

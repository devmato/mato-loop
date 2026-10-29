from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from mato_loop_backend.db.base import Base


class MailSendOperation(Base):
    __tablename__ = "mail_send_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'sent', 'outcome_unknown')",
            name="status_valid",
        ),
        UniqueConstraint(
            "user_id", "integration_id", "key_hash", name="uq_mail_send_operations_key"
        ),
        UniqueConstraint(
            "user_id",
            "integration_id",
            "draft_hash",
            name="uq_mail_send_operations_draft",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    integration_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    sent_copy_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

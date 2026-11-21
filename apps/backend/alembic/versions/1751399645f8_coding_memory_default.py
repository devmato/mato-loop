"""Coding memory default

Revision ID: 1751399645f8
Revises: a8b6c1afb08c
Create Date: 2026-11-21 21:28:27.275966
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '1751399645f8'
down_revision: str | None = 'a8b6c1afb08c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CODING_ID = "00000000-0000-4000-8000-000000000102"


def _update_templates(data: dict) -> bool:
    for template in data.get("templates", []):
        if template.get("id") == CODING_ID:
            key = "memoryMode" if "memoryMode" in template else "memory_mode"
            if template.get(key, "full") == "full":
                template[key] = "read_only"
                return True
    return False


def upgrade() -> None:
    connection = op.get_bind()
    settings = sa.table("settings", sa.column("id", sa.Uuid()), sa.column("key", sa.String()),
                        sa.column("user_id", sa.Uuid()), sa.column("data", JSONB()))
    for row in connection.execute(sa.select(settings).where(
        settings.c.key == "session-templates", settings.c.user_id.is_(None),
    )).mappings():
        data = row["data"]
        if _update_templates(data):
            connection.execute(settings.update().where(settings.c.id == row["id"]).values(data=data))


def downgrade() -> None:
    # Older versions support read-only mode. Do not re-enable extraction on downgrade.
    pass

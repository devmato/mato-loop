"""mcp oauth integration uniqueness

Revision ID: b5b50a159646
Revises: 1751399645f8
Create Date: 2026-11-24 22:32:23.313837
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b5b50a159646'
down_revision: str | None = '1751399645f8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        'uq_credentials_mcp_oauth_integration',
        'credentials',
        ['user_id', sa.literal_column("(metadata ->> 'integration_id')")],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'mcp.oauth' AND metadata ? 'integration_id'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_credentials_mcp_oauth_integration',
        table_name='credentials',
        postgresql_where=sa.text(
            "kind = 'mcp.oauth' AND metadata ? 'integration_id'"
        ),
    )

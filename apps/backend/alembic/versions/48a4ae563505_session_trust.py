"""session trust

Revision ID: 48a4ae563505
Revises: 35017388e8ff
Create Date: 2026-10-29 20:14:23.913897
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '48a4ae563505'
down_revision: Union[str, None] = '35017388e8ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('chat_sessions', sa.Column('trust_level', sa.String(length=20), nullable=True))
    op.create_check_constraint(
        'trust_level_valid', 'chat_sessions',
        "trust_level IS NULL OR trust_level IN ('public', 'private')",
    )
    op.execute("""
        UPDATE chat_sessions AS session
        SET trust_level = CASE WHEN EXISTS (
            SELECT 1 FROM messages
            WHERE session_id = session.id AND trust_level = 'private'
        ) THEN 'private' ELSE 'public' END
        WHERE EXISTS (SELECT 1 FROM messages WHERE session_id = session.id)
    """)


def downgrade() -> None:
    op.drop_constraint('trust_level_valid', 'chat_sessions', type_='check')
    op.drop_column('chat_sessions', 'trust_level')

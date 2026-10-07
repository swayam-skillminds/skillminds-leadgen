"""phase 6: a stable key on signals, so a re-found signal updates its row

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07 23:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A literal server default: SQLite adds the column in place and old rows read "".
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('key', sa.String(length=300), nullable=False,
                                      server_default=''))
        batch_op.create_index('ix_signals_lookup', ['organization_id', 'lead_type', 'type', 'key'],
                              unique=False)


def downgrade() -> None:
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.drop_index('ix_signals_lookup')
        batch_op.drop_column('key')

"""phase 6b: people carry their lead type, page and published email; inferred contacts name their person

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08 13:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Literal server defaults: SQLite adds the columns in place and old rows read "".
    with op.batch_alter_table('people', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lead_type', sa.String(length=50), nullable=False,
                                      server_default=''))
        batch_op.add_column(sa.Column('source_url', sa.String(length=2000), nullable=True))
        batch_op.add_column(sa.Column('email', sa.String(length=320), nullable=True))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table('contacts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('person', sa.String(length=300), nullable=False,
                                      server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('contacts', schema=None) as batch_op:
        batch_op.drop_column('person')
    with op.batch_alter_table('people', schema=None) as batch_op:
        batch_op.drop_column('updated_at')
        batch_op.drop_column('email')
        batch_op.drop_column('source_url')
        batch_op.drop_column('lead_type')

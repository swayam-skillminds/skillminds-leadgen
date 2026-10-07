"""phase 3: settings, notifications, schedules, run files

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07 10:15:37.278696
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Columns added to existing tables carry a server default so rows written
    # before this revision (workspaces, runs, saved configs, schedules) get one.
    # Plain literal defaults: SQLite then adds the columns in place instead of
    # rebuilding the tables (and init_schema keeps foreign keys off regardless).
    op.create_table('notification_targets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('config', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('events', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_notification_targets_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_targets'))
    )
    with op.batch_alter_table('notification_targets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_targets_workspace_id'), ['workspace_id'], unique=False)

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('retention_days', sa.Integer(), nullable=True))

    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('files', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False, server_default='[]'))

    with op.batch_alter_table('saved_configs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=False, server_default=''))

    with op.batch_alter_table('schedules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('name', sa.String(length=200), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('timezone', sa.String(length=64), nullable=False, server_default='UTC'))
        batch_op.add_column(sa.Column('overrides', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('last_run_id', sa.Integer(), nullable=True))

    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('settings', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False, server_default='{}'))



def downgrade() -> None:
    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.drop_column('settings')

    with op.batch_alter_table('schedules', schema=None) as batch_op:
        batch_op.drop_column('last_run_id')
        batch_op.drop_column('overrides')
        batch_op.drop_column('timezone')
        batch_op.drop_column('name')

    with op.batch_alter_table('saved_configs', schema=None) as batch_op:
        batch_op.drop_column('description')

    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_column('files')

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('retention_days')

    with op.batch_alter_table('notification_targets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_targets_workspace_id'))

    op.drop_table('notification_targets')

"""engine: run lifecycle, events, workers

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06 15:34:05.093169
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Columns added to existing tables carry a server default so rows written
    # before this revision (runs, units, stats) get a valid value.
    op.create_table('workers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('hostname', sa.String(length=200), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('current_run_id', sa.Integer(), nullable=True),
    sa.Column('stop_requested', sa.Boolean(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workers')),
    sa.UniqueConstraint('name', name=op.f('uq_workers_name'))
    )
    op.create_table('run_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_events_run_id_runs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_run_events_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_run_events')),
    sa.UniqueConstraint('run_id', 'seq', name=op.f('uq_run_events_run_id_seq'))
    )
    with op.batch_alter_table('run_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_run_events_workspace_id'), ['workspace_id'], unique=False)

    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column('estimate', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column('checkpoint', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column('worker_id', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('active_seconds', sa.Float(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')))

    with op.batch_alter_table('unit_stats', schema=None) as batch_op:
        batch_op.add_column(sa.Column('total_sq_seconds', sa.Float(), nullable=False, server_default=sa.text('0')))

    with op.batch_alter_table('work_units', schema=None) as batch_op:
        batch_op.add_column(sa.Column('unit_key', sa.String(length=700), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('new', sa.Integer(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('merged', sa.Integer(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('seconds', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('expected_seconds', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('expected_new', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('priority', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('skip_reason', sa.Text(), nullable=False, server_default=''))
        batch_op.create_index('ix_work_units_run_key', ['run_id', 'unit_key'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('work_units', schema=None) as batch_op:
        batch_op.drop_index('ix_work_units_run_key')
        batch_op.drop_column('skip_reason')
        batch_op.drop_column('priority')
        batch_op.drop_column('expected_new')
        batch_op.drop_column('expected_seconds')
        batch_op.drop_column('seconds')
        batch_op.drop_column('merged')
        batch_op.drop_column('new')
        batch_op.drop_column('unit_key')

    with op.batch_alter_table('unit_stats', schema=None) as batch_op:
        batch_op.drop_column('total_sq_seconds')

    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_column('attempts')
        batch_op.drop_column('active_seconds')
        batch_op.drop_column('heartbeat_at')
        batch_op.drop_column('worker_id')
        batch_op.drop_column('checkpoint')
        batch_op.drop_column('estimate')
        batch_op.drop_column('request')

    with op.batch_alter_table('run_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_run_events_workspace_id'))

    op.drop_table('run_events')
    op.drop_table('workers')

"""phase 5: AI attributes, candidate sources, selector sets, ledger columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07 18:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    # New tables, plus three counters on ai_usage with literal server defaults,
    # so SQLite adds them in place (no table rebuild) and old rows read 0.
    op.create_table('candidate_sources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('url', sa.String(length=2000), nullable=False),
    sa.Column('host', sa.String(length=255), nullable=False),
    sa.Column('country', sa.String(length=2), nullable=False),
    sa.Column('place', sa.String(length=200), nullable=False),
    sa.Column('category', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('page_type', sa.String(length=50), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('proposed_by', sa.String(length=160), nullable=False),
    sa.Column('terms_note', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_candidate_sources_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_candidate_sources')),
    sa.UniqueConstraint('workspace_id', 'url', name=op.f('uq_candidate_sources_workspace_id_url'))
    )
    with op.batch_alter_table('candidate_sources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_candidate_sources_host'), ['host'], unique=False)
        batch_op.create_index(batch_op.f('ix_candidate_sources_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_candidate_sources_workspace_id'), ['workspace_id'], unique=False)

    op.create_table('org_attributes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=50), nullable=False),
    sa.Column('value', JSON, nullable=False),
    sa.Column('origin', sa.String(length=20), nullable=False),
    sa.Column('estimated', sa.Boolean(), nullable=False),
    sa.Column('producer', sa.String(length=160), nullable=False),
    sa.Column('lead_type', sa.String(length=50), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_org_attributes_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_org_attributes_run_id_runs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_org_attributes_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_org_attributes')),
    sa.UniqueConstraint('organization_id', 'key', name=op.f('uq_org_attributes_organization_id_key'))
    )
    with op.batch_alter_table('org_attributes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_org_attributes_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_org_attributes_workspace_id'), ['workspace_id'], unique=False)

    op.create_table('selector_sets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('host', sa.String(length=255), nullable=False),
    sa.Column('page_type', sa.String(length=50), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('selectors', JSON, nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('sample_url', sa.String(length=2000), nullable=True),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_selector_sets_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_selector_sets'))
    )
    with op.batch_alter_table('selector_sets', schema=None) as batch_op:
        batch_op.create_index('ix_selector_sets_lookup', ['workspace_id', 'host', 'page_type'], unique=False)

    with op.batch_alter_table('ai_usage', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cache_hits', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('failures', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('searches', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('ai_usage', schema=None) as batch_op:
        batch_op.drop_column('searches')
        batch_op.drop_column('failures')
        batch_op.drop_column('cache_hits')

    with op.batch_alter_table('selector_sets', schema=None) as batch_op:
        batch_op.drop_index('ix_selector_sets_lookup')
    op.drop_table('selector_sets')

    with op.batch_alter_table('org_attributes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_org_attributes_workspace_id'))
        batch_op.drop_index(batch_op.f('ix_org_attributes_organization_id'))
    op.drop_table('org_attributes')

    with op.batch_alter_table('candidate_sources', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_candidate_sources_workspace_id'))
        batch_op.drop_index(batch_op.f('ix_candidate_sources_status'))
        batch_op.drop_index(batch_op.f('ix_candidate_sources_host'))
    op.drop_table('candidate_sources')

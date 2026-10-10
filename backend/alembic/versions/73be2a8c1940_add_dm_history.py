"""Persist bidirectional history and per-peer intent/style analysis."""
from alembic import op
import sqlalchemy as sa
from backend.app.models.direct_message import DmHistory, DmConversation

revision = '73be2a8c1940'
down_revision = '67dd81a9ef01'
branch_labels = None
depends_on = None


def upgrade():
    if 'control_mode' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('xhs_dm_listeners')}:
        op.add_column('xhs_dm_listeners', sa.Column('control_mode', sa.String(24), nullable=False, server_default='auto_approve'))
    for table in (DmHistory.__table__, DmConversation.__table__):
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    for table in (DmConversation.__table__, DmHistory.__table__):
        table.drop(bind=op.get_bind(), checkfirst=True)
    op.drop_column('xhs_dm_listeners', 'control_mode')

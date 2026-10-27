"""Freeze the selected account Agent on DM listeners."""
from alembic import op
import sqlalchemy as sa
revision = 'a6f204d39e12'
down_revision = '95d032f5b136'
branch_labels = depends_on = None

def upgrade():
    if 'agent_snapshot' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('xhs_dm_listeners')}:
        op.add_column('xhs_dm_listeners', sa.Column('agent_snapshot', sa.JSON(), nullable=False, server_default='{}'))

def downgrade():
    op.drop_column('xhs_dm_listeners', 'agent_snapshot')

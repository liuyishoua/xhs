"""Durable incremental synchronization with the shared Channel session store."""
from alembic import op
import sqlalchemy as sa
revision = '95d032f5b136'
down_revision = '84c921e4a025'
branch_labels = depends_on = None

def upgrade():
    if 'session_state' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('xhs_dm_conversations')}:
        op.add_column('xhs_dm_conversations', sa.Column('session_state', sa.JSON(), nullable=False, server_default='{}'))

def downgrade():
    op.drop_column('xhs_dm_conversations', 'session_state')

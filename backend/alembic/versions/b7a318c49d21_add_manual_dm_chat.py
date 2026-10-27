"""Account-scoped manual conversations and durable send intents."""
from alembic import op
import sqlalchemy as sa
revision = 'b7a318c49d21'
down_revision = 'a6f204d39e12'
branch_labels = depends_on = None

def upgrade():
    bind=op.get_bind()
    columns={c['name'] for c in sa.inspect(bind).get_columns('xhs_dm_listeners')}
    for name in ('manual_chat_enabled','chat_refresh_requested'):
        if name not in columns:
            op.add_column('xhs_dm_listeners',sa.Column(name,sa.Boolean(),nullable=False,server_default='0'))
    from backend.app.models.direct_message import DmManualMessage
    DmManualMessage.__table__.create(bind,checkfirst=True)

def downgrade():
    op.drop_table('xhs_dm_manual_messages')
    op.drop_column('xhs_dm_listeners','chat_refresh_requested')
    op.drop_column('xhs_dm_listeners','manual_chat_enabled')

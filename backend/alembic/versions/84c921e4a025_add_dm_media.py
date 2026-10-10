"""Persist private media and typed messages."""
from alembic import op
import sqlalchemy as sa
from backend.app.models.direct_message import DmMedia
revision = '84c921e4a025'
down_revision = '73be2a8c1940'
branch_labels = depends_on = None

def upgrade():
    for name in ('xhs_direct_messages', 'xhs_dm_history'):
        if 'media' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns(name)}:
            op.add_column(name, sa.Column('media', sa.JSON(), nullable=False, server_default='[]'))
    DmMedia.__table__.create(bind=op.get_bind(), checkfirst=True)

def downgrade():
    DmMedia.__table__.drop(bind=op.get_bind(), checkfirst=True)
    for name in ('xhs_direct_messages', 'xhs_dm_history'):
        op.drop_column(name, 'media')

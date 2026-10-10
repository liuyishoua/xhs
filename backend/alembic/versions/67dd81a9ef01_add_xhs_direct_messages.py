"""Durable XHS private-message listener and delivery records."""
from alembic import op
from backend.app.models.direct_message import DmListener, DirectMessage
revision = '67dd81a9ef01'
down_revision = '60cd5c95fde1'
branch_labels = None
depends_on = None


def upgrade():
    for table in (DmListener.__table__, DirectMessage.__table__):
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    for table in (DirectMessage.__table__, DmListener.__table__):
        table.drop(bind=op.get_bind(), checkfirst=True)

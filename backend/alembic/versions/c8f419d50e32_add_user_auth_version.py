"""Revoke a workspace user's existing login credentials on logout."""
from alembic import op
import sqlalchemy as sa

revision = 'c8f419d50e32'
down_revision = 'b7a318c49d21'
branch_labels = depends_on = None


def upgrade():
    if 'auth_version' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('users')}:
        op.add_column('users', sa.Column('auth_version', sa.Integer(), nullable=False, server_default='0'))
    if op.get_bind().dialect.name == 'sqlite':
        definition = op.get_bind().execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='platform_accounts'" )).scalar()
        if definition and 'AUTOINCREMENT' not in definition.upper():
            # A deleted matrix ID must never be assigned to a different user.
            with op.batch_alter_table('platform_accounts', recreate='always',
                                      table_kwargs={'sqlite_autoincrement': True}):
                pass


def downgrade():
    op.drop_column('users', 'auth_version')

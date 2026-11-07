"""Persist shared phone requests and the approvals behind each applied change."""
from alembic import op
import sqlalchemy as sa

revision = '0002_contact_phone_changes'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'contact_phone_changes',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('requester_id', sa.String(32), sa.ForeignKey('clients.id'), nullable=False),
        sa.Column('phone', sa.String(21), nullable=False),
        sa.Column('approvals', sa.JSON(), nullable=False),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    raise RuntimeError('降级会丢失共享号码和批准记录，请从备份恢复。')

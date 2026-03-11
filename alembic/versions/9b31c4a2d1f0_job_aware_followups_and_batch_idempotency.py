"""job aware followups and batch idempotency

Revision ID: 9b31c4a2d1f0
Revises: 6f7b2c1d8a9e
Create Date: 2026-03-05

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9b31c4a2d1f0"
down_revision = "6f7b2c1d8a9e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outreach_tracker", sa.Column("job_run_id", sa.Integer(), nullable=True))
    op.add_column("outreach_tracker", sa.Column("job_code", sa.String(length=255), nullable=True))
    op.add_column("outreach_tracker", sa.Column("job_title", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_outreach_tracker_job_run_id"), "outreach_tracker", ["job_run_id"], unique=False)
    op.create_index(op.f("ix_outreach_tracker_job_code"), "outreach_tracker", ["job_code"], unique=False)

    op.add_column("ceipal_batch_run", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_ceipal_batch_run_request_hash"), "ceipal_batch_run", ["request_hash"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ceipal_batch_run_request_hash"), table_name="ceipal_batch_run")
    op.drop_column("ceipal_batch_run", "request_hash")

    op.drop_index(op.f("ix_outreach_tracker_job_code"), table_name="outreach_tracker")
    op.drop_index(op.f("ix_outreach_tracker_job_run_id"), table_name="outreach_tracker")
    op.drop_column("outreach_tracker", "job_title")
    op.drop_column("outreach_tracker", "job_code")
    op.drop_column("outreach_tracker", "job_run_id")

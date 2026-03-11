"""add certifications_json to ceipal_candidate_run

Revision ID: 6f7b2c1d8a9e
Revises: c02968ee91b4
Create Date: 2026-03-03

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "6f7b2c1d8a9e"
down_revision = "c02968ee91b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ceipal_candidate_run",
        sa.Column("certifications_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ceipal_candidate_run", "certifications_json")

"""store IPIP-NEO facet scores

Revision ID: 5a1b2c3d4e5f
Revises: 8f9e7c45c0a4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5a1b2c3d4e5f"
down_revision: str | Sequence[str] | None = "8f9e7c45c0a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assessments",
        sa.Column(
            "facet_scores",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("assessments", "facet_scores")

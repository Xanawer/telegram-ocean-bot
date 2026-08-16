"""add replication outbox and analytics event identity

Revision ID: 7c8d9e0f1a2b
Revises: 5a1b2c3d4e5f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c8d9e0f1a2b"
down_revision: str | Sequence[str] | None = "5a1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("analytics_events") as batch:
        batch.add_column(sa.Column("event_id", sa.String(length=36), nullable=True))

    op.execute(
        "UPDATE analytics_events "
        "SET event_id = 'legacy-' || CAST(id AS VARCHAR(32)) "
        "WHERE event_id IS NULL"
    )
    with op.batch_alter_table("analytics_events") as batch:
        batch.alter_column("event_id", existing_type=sa.String(length=36), nullable=False)
    op.create_index(
        "uq_analytics_events_event_id",
        "analytics_events",
        ["event_id"],
        unique=True,
    )

    op.create_table(
        "sync_outbox",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("force_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("kind", "entity_id"),
    )
    op.create_index(
        "ix_sync_outbox_available_at",
        "sync_outbox",
        ["available_at"],
        unique=False,
    )
    op.create_table(
        "deletion_tombstones",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_ref", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("telegram_user_id"),
    )


def downgrade() -> None:
    op.drop_table("deletion_tombstones")
    op.drop_index("ix_sync_outbox_available_at", table_name="sync_outbox")
    op.drop_table("sync_outbox")
    op.drop_index("uq_analytics_events_event_id", table_name="analytics_events")
    with op.batch_alter_table("analytics_events") as batch:
        batch.drop_column("event_id")

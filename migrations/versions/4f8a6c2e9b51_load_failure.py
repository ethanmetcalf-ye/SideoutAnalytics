"""load_failure

Revision ID: 4f8a6c2e9b51
Revises: 9d4e2b7a1c03
Create Date: 2026-11-11 14:00:00.000000

docs/schema.md load_failure: games that are failing to parse or load, so a
run fails only on a game's first failure (PR #79 review).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f8a6c2e9b51"
down_revision: str | Sequence[str] | None = "9d4e2b7a1c03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "load_failure",
        sa.Column("source_game_id", sa.Text(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("ingest_run_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ingest_run_id"],
            ["ingest_run.ingest_run_id"],
            name=op.f("fk_load_failure_ingest_run_id_ingest_run"),
        ),
        sa.PrimaryKeyConstraint("source_game_id", name=op.f("pk_load_failure")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("load_failure")

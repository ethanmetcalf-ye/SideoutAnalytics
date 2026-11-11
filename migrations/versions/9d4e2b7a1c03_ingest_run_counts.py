"""ingest_run counts

Revision ID: 9d4e2b7a1c03
Revises: 3c34f46d51e6
Create Date: 2026-11-11 10:00:00.000000

docs/schema.md ingest_run: each run records what it did (W2-07, issue #19).
The counts are nullable, existing runs and runs that stopped before
counting have none.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d4e2b7a1c03"
down_revision: str | Sequence[str] | None = "3c34f46d51e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNTS = ("games_in_index", "games_loaded", "games_unchanged", "games_failed", "requests_failed")


def upgrade() -> None:
    """Upgrade schema."""
    for column in COUNTS:
        op.add_column("ingest_run", sa.Column(column, sa.Integer(), nullable=True))
        op.create_check_constraint(f"{column}_non_negative", "ingest_run", f"{column} >= 0")


def downgrade() -> None:
    """Downgrade schema."""
    for column in COUNTS:
        op.drop_constraint(op.f(f"ck_ingest_run_{column}_non_negative"), "ingest_run", type_="check")
        op.drop_column("ingest_run", column)

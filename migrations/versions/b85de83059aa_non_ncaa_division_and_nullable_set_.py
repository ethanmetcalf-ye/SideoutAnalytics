"""non-NCAA division and nullable set attempts

Revision ID: b85de83059aa
Revises: c2ca01d6d4e8
Create Date: 2026-11-07 22:09:17.378786

docs/schema.md decision 13: team_season.division allows "other" for
non-NCAA opponents, and box_score_line.set_attempts is NULL when a match has
no set attempts recorded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b85de83059aa"
down_revision: str | Sequence[str] | None = "c2ca01d6d4e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("box_score_line", "set_attempts", existing_type=sa.SmallInteger(), nullable=True)

    # the enum is a varchar sized to its longest value plus a CHECK, so both
    # grow: varchar(2) -> varchar(5) and the CHECK gets "other"
    op.drop_constraint(op.f("ck_team_season_division"), "team_season", type_="check")
    op.alter_column(
        "team_season",
        "division",
        existing_type=sa.VARCHAR(length=2),
        type_=sa.VARCHAR(length=5),
        existing_nullable=False,
    )
    op.create_check_constraint("division", "team_season", "division IN ('d1', 'd2', 'd3', 'other')")


def downgrade() -> None:
    """Downgrade schema."""
    # fails if any team_season row is "other" or any set_attempts is NULL,
    # those rows have to be deleted first
    op.drop_constraint(op.f("ck_team_season_division"), "team_season", type_="check")
    op.alter_column(
        "team_season",
        "division",
        existing_type=sa.VARCHAR(length=5),
        type_=sa.VARCHAR(length=2),
        existing_nullable=False,
    )
    op.create_check_constraint("division", "team_season", "division IN ('d1', 'd2', 'd3')")

    op.alter_column("box_score_line", "set_attempts", existing_type=sa.SmallInteger(), nullable=False)

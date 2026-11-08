"""nullable conference for non-NCAA teams

Revision ID: 5af0701018d2
Revises: b85de83059aa
Create Date: 2026-11-08 12:48:40.910690

docs/schema.md decision 13: team_season.conference_id is NULL for non-NCAA
opponents, which the scoreboard lists with no conference.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5af0701018d2"
down_revision: str | Sequence[str] | None = "b85de83059aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("team_season", "conference_id", existing_type=sa.BIGINT(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    # fails if any team_season row has no conference, those rows have to be
    # deleted first
    op.alter_column("team_season", "conference_id", existing_type=sa.BIGINT(), nullable=False)

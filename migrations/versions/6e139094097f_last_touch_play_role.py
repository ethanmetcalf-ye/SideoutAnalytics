"""last_touch play role

Revision ID: 6e139094097f
Revises: 5af0701018d2
Create Date: 2026-11-08 14:19:18.920807

docs/schema.md play_player: play_player.role allows "last_touch", the other
team's player named in a kill's "(from ...)". The varchar(13) already fits
it, only the CHECK changes.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6e139094097f"
down_revision: str | Sequence[str] | None = "5af0701018d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f("ck_play_player_playrole"), "play_player", type_="check")
    op.create_check_constraint(
        "playrole", "play_player", "role IN ('attacker', 'setter', 'blocker', 'aced_receiver', 'last_touch')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # fails if any play_player row is "last_touch", those rows have to be
    # deleted first
    op.drop_constraint(op.f("ck_play_player_playrole"), "play_player", type_="check")
    op.create_check_constraint("playrole", "play_player", "role IN ('attacker', 'setter', 'blocker', 'aced_receiver')")

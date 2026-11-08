"""rename last_touch play role to overpass

Revision ID: 3c34f46d51e6
Revises: 6e139094097f
Create Date: 2026-11-08 15:00:49.935796

docs/schema.md play_player: a kill's "(from ...)" player on the other team
from the attacker has role "overpass" instead of "last_touch" (issue #77).
Existing rows are renamed, the varchar(13) still fits.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c34f46d51e6"
down_revision: str | Sequence[str] | None = "6e139094097f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f("ck_play_player_playrole"), "play_player", type_="check")
    op.execute("UPDATE play_player SET role = 'overpass' WHERE role = 'last_touch'")
    op.create_check_constraint(
        "playrole", "play_player", "role IN ('attacker', 'setter', 'blocker', 'aced_receiver', 'overpass')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_play_player_playrole"), "play_player", type_="check")
    op.execute("UPDATE play_player SET role = 'last_touch' WHERE role = 'overpass'")
    op.create_check_constraint(
        "playrole", "play_player", "role IN ('attacker', 'setter', 'blocker', 'aced_receiver', 'last_touch')"
    )

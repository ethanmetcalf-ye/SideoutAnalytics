"""SQLAlchemy ORM tables. docs/schema.md is the source of truth.

Copying a table from schema.md:
- Python types map to Postgres types through Base.type_annotation_map, so
  most columns need no mapped_column() at all: Mapped[str] is text.
- "Nullable" columns are Mapped[X | None]; everything else is NOT NULL.
- Id, Year and Small cover the column shapes that aren't plain bigint/text.
- Allowed-value lists are StrEnums, stored as text with a CHECK constraint.
- The schema.md "Check constraints" section goes in __table_args__.
- Foreign keys use the database default (no delete while referenced),
  except child rows of a match, which cascade.
"""

import enum
from datetime import date, datetime
from typing import Annotated, Any, ClassVar

from sqlalchemy import (
    ARRAY,
    REAL,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Division(enum.StrEnum):
    D1 = "d1"
    D2 = "d2"
    D3 = "d3"


class MatchState(enum.StrEnum):
    PRE = "pre"
    LIVE = "live"
    FINAL = "final"


class PlayType(enum.StrEnum):
    KILL = "kill"
    ATTACK_ERROR = "attack_error"
    SERVICE_ACE = "service_ace"
    SERVICE_ERROR = "service_error"
    BAD_SET = "bad_set"
    BALL_HANDLING_ERROR = "ball_handling_error"
    OTHER = "other"


class PlayRole(enum.StrEnum):
    ATTACKER = "attacker"
    SETTER = "setter"
    BLOCKER = "blocker"
    ACED_RECEIVER = "aced_receiver"


class IngestStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _text_enum(enum_cls: type[enum.StrEnum]) -> Enum:
    """Store enum values ("d1", not "D1") as text with a CHECK constraint."""
    return Enum(
        enum_cls,
        name=enum_cls.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=max(len(member.value) for member in enum_cls),
        values_callable=lambda cls: [member.value for member in cls],
    )


def non_negative(*columns: str) -> list[CheckConstraint]:
    """One `column >= 0` CHECK per count column."""
    return [
        CheckConstraint(f"{column} >= 0", name=f"{column}_non_negative")
        for column in columns
    ]


# Stable constraint names, so Alembic can find them in later migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        int: BigInteger,
        str: Text,
        float: REAL,
        datetime: DateTime(timezone=True),
        list[str]: ARRAY(Text),
        Division: _text_enum(Division),
        MatchState: _text_enum(MatchState),
        PlayType: _text_enum(PlayType),
        PlayRole: _text_enum(PlayRole),
        IngestStatus: _text_enum(IngestStatus),
    }


Id = Annotated[int, mapped_column(Identity(), primary_key=True)]
Year = Annotated[int, mapped_column(Integer)]
Small = Annotated[int, mapped_column(SmallInteger)]


class Season(Base):
    __tablename__ = "season"
    __table_args__ = (
        CheckConstraint("start_date <= end_date", name="dates_in_order"),
    )

    year: Mapped[Year] = mapped_column(primary_key=True)
    start_date: Mapped[date]
    end_date: Mapped[date]


class Conference(Base):
    __tablename__ = "conference"

    conference_id: Mapped[Id]
    name: Mapped[str] = mapped_column(unique=True)


class Team(Base):
    __tablename__ = "team"

    team_id: Mapped[Id]
    seoname: Mapped[str] = mapped_column(unique=True)
    team_name: Mapped[str]


class TeamAlias(Base):
    __tablename__ = "team_alias"

    alias: Mapped[str] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))


class TeamSeason(Base):
    __tablename__ = "team_season"

    team_id: Mapped[int] = mapped_column(
        ForeignKey("team.team_id"), primary_key=True
    )
    season: Mapped[Year] = mapped_column(
        ForeignKey("season.year"), primary_key=True
    )
    source_team_id: Mapped[str]
    division: Mapped[Division]
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conference.conference_id")
    )
    head_coach: Mapped[str | None]
    assistant_coaches: Mapped[list[str] | None]


class Player(Base):
    __tablename__ = "player"

    player_id: Mapped[Id]
    first_name: Mapped[str]
    last_name: Mapped[str]
    name_key: Mapped[str]


class Roster(Base):
    __tablename__ = "roster"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "season"],
            ["team_season.team_id", "team_season.season"],
        ),
        UniqueConstraint("team_id", "season", "name_key"),
    )

    player_id: Mapped[int] = mapped_column(
        ForeignKey("player.player_id"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(primary_key=True)
    season: Mapped[Year] = mapped_column(primary_key=True)
    name_key: Mapped[str]
    number: Mapped[str | None]
    position: Mapped[str | None]


class IngestRun(Base):
    __tablename__ = "ingest_run"
    __table_args__ = (
        CheckConstraint(
            "finished_at >= started_at", name="finished_after_started"
        ),
    )

    ingest_run_id: Mapped[Id]
    job: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[IngestStatus]
    error: Mapped[str | None]


class Match(Base):
    __tablename__ = "match"

    match_id: Mapped[Id]
    source_game_id: Mapped[str] = mapped_column(unique=True)
    season: Mapped[Year] = mapped_column(ForeignKey("season.year"))
    start_time: Mapped[datetime]
    state: Mapped[MatchState]
    bracket_round: Mapped[str | None]
    venue: Mapped[str | None]
    venue_city: Mapped[str | None]
    venue_state: Mapped[str | None]
    is_neutral_site: Mapped[bool | None]
    playbyplay_available: Mapped[bool]
    pbp_reconciled: Mapped[bool | None]
    pbp_parser_version: Mapped[str | None]
    result_first_seen_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]
    ingest_run_id: Mapped[int] = mapped_column(
        ForeignKey("ingest_run.ingest_run_id")
    )


class MatchTeam(Base):
    __tablename__ = "match_team"
    __table_args__ = (
        CheckConstraint("sets_won BETWEEN 0 AND 3", name="sets_won_range"),
        CheckConstraint("seed > 0", name="seed_positive"),
        CheckConstraint("rank_at_match > 0", name="rank_positive"),
        Index(
            "uq_match_team_one_home",
            "match_id",
            unique=True,
            postgresql_where=text("is_home"),
        ),
        Index(
            "uq_match_team_one_winner",
            "match_id",
            unique=True,
            postgresql_where=text("is_winner"),
        ),
    )

    match_id: Mapped[int] = mapped_column(
        ForeignKey("match.match_id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        ForeignKey("team.team_id"), primary_key=True
    )
    is_home: Mapped[bool]
    is_winner: Mapped[bool | None]
    sets_won: Mapped[Small | None]
    seed: Mapped[Small | None]
    rank_at_match: Mapped[Small | None]


BOX_SCORE_COUNTS = (
    "kills",
    "attack_errors",
    "attack_attempts",
    "assists",
    "set_attempts",
    "set_errors",
    "aces",
    "serve_errors",
    "serve_attempts",
    "digs",
    "reception_attempts",
    "reception_errors",
    "blocks_solo",
    "blocks_assist",
    "blocking_errors",
    "ball_handling_errors",
)


class BoxScoreLine(Base):
    __tablename__ = "box_score_line"
    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id", "team_id"],
            ["match_team.match_id", "match_team.team_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "sets_played BETWEEN 0 AND 5", name="sets_played_range"
        ),
        *non_negative(*BOX_SCORE_COUNTS),
    )

    match_id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("player.player_id"), primary_key=True
    )
    team_id: Mapped[int]
    position: Mapped[str | None]
    started: Mapped[bool]
    sets_played: Mapped[Small]
    kills: Mapped[Small]
    attack_errors: Mapped[Small]
    attack_attempts: Mapped[Small]
    assists: Mapped[Small]
    set_attempts: Mapped[Small]
    set_errors: Mapped[Small]
    aces: Mapped[Small]
    serve_errors: Mapped[Small]
    serve_attempts: Mapped[Small]
    digs: Mapped[Small]
    reception_attempts: Mapped[Small]
    reception_errors: Mapped[Small]
    blocks_solo: Mapped[Small]
    blocks_assist: Mapped[Small]
    blocking_errors: Mapped[Small]
    ball_handling_errors: Mapped[Small]


TEAM_SET_COUNTS = (
    "points",
    "kills",
    "attack_errors",
    "attack_attempts",
    "aces",
    "serve_errors",
    "serve_attempts",
    "block_points",
    "sideouts",
    "sideout_attempts",
    "serve_points",
)


class TeamSetStats(Base):
    __tablename__ = "team_set_stats"
    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id", "team_id"],
            ["match_team.match_id", "match_team.team_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("set_number BETWEEN 1 AND 5", name="set_number_range"),
        *non_negative(*TEAM_SET_COUNTS),
    )

    match_id: Mapped[int] = mapped_column(primary_key=True)
    set_number: Mapped[Small] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(primary_key=True)
    points: Mapped[Small]
    kills: Mapped[Small]
    attack_errors: Mapped[Small]
    attack_attempts: Mapped[Small]
    # NULL when play-by-play did not reconcile with the box score.
    aces: Mapped[Small | None]
    serve_errors: Mapped[Small | None]
    serve_attempts: Mapped[Small | None]
    block_points: Mapped[Small | None]
    sideouts: Mapped[Small | None]
    sideout_attempts: Mapped[Small | None]
    serve_points: Mapped[Small | None]


class Play(Base):
    __tablename__ = "play"
    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id", "scoring_team_id"],
            ["match_team.match_id", "match_team.team_id"],
        ),
        ForeignKeyConstraint(
            ["match_id", "serving_team_id"],
            ["match_team.match_id", "match_team.team_id"],
        ),
        CheckConstraint("set_number BETWEEN 1 AND 5", name="set_number_range"),
        CheckConstraint(
            "home_score >= 0 AND away_score >= 0", name="score_nonnegative"
        ),
        CheckConstraint("sequence >= 1", name="positive_sequence"),
        UniqueConstraint("match_id", "set_number", "sequence"),
    )

    play_id: Mapped[Id]
    match_id: Mapped[int] = mapped_column(
        ForeignKey("match.match_id", ondelete="CASCADE")
    )
    set_number: Mapped[Small]
    sequence: Mapped[int] = mapped_column(Integer)
    play_type: Mapped[PlayType]
    scoring_team_id: Mapped[int]
    serving_team_id: Mapped[int | None]
    play_text: Mapped[str]
    home_score: Mapped[Small]
    away_score: Mapped[Small]


class PlayPlayer(Base):
    __tablename__ = "play_player"

    play_id: Mapped[int] = mapped_column(
        ForeignKey("play.play_id", ondelete="CASCADE"), primary_key=True
    )
    player_id: Mapped[int] = mapped_column(
        ForeignKey("player.player_id"), primary_key=True
    )
    role: Mapped[PlayRole] = mapped_column(primary_key=True)


class TeamRank(Base):
    __tablename__ = "team_rank"
    __table_args__ = (
        CheckConstraint("ranking >= 1", name="ranking_positive"),
        *non_negative("total_points", "first_place_votes"),
    )

    team_id: Mapped[int] = mapped_column(
        ForeignKey("team.team_id"), primary_key=True
    )
    poll: Mapped[str] = mapped_column(primary_key=True)
    poll_date: Mapped[date] = mapped_column(primary_key=True)
    season: Mapped[Year] = mapped_column(ForeignKey("season.year"))
    ranking: Mapped[Small]
    total_points: Mapped[int] = mapped_column(Integer)
    first_place_votes: Mapped[Small]


class RawResponse(Base):
    __tablename__ = "raw_response"

    raw_response_id: Mapped[Id]
    endpoint: Mapped[str]
    file_path: Mapped[str] = mapped_column(unique=True)
    content_hash: Mapped[str]
    match_id: Mapped[int | None] = mapped_column(ForeignKey("match.match_id"))
    fetched_at: Mapped[datetime]
    ingest_run_id: Mapped[int] = mapped_column(
        ForeignKey("ingest_run.ingest_run_id")
    )


class Prediction(Base):
    __tablename__ = "prediction"
    __table_args__ = (
        CheckConstraint(
            "home_win_probability BETWEEN 0 AND 1", name="probability_range"
        ),
    )

    prediction_id: Mapped[Id]
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    model_version: Mapped[str]
    home_win_probability: Mapped[float]
    created_at: Mapped[datetime]

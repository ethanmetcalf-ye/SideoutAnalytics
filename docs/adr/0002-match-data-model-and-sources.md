# ADR-002: Store Match Stats as Rows With the Box Score as Source of Truth

**Status:** Proposed

## Context

The site needs leaderboards, team and player pages, per-set charts, and
match momentum, all built from the unofficial NCAA API. That API provides
three views of each match: a box score with per-player match totals, per-set
attacking stats for each team, and a free-text play-by-play feed. The
play-by-play is the only source for serving, sideout, and rally-level data,
but its quality varies between matches. The API also has no player IDs, so
players can only be identified by name, team, and jersey number.

On a sample match, play-by-play totals matched the box score exactly for
kills, attack errors, aces, service errors, and block points. That makes the
box score a usable check on the play-by-play.

## Decision

We will parse API responses into relational tables (`box_score_line`,
`team_set_stats`, `play`) and keep the raw responses separately, rather than
storing stats as JSON. Only counts are stored; rates and totals are
calculated at query time.

We will treat the box score as the source of truth for match totals. The
play-by-play is used for set-level and rally-level stats only when its totals
reconcile with the box score. Each stat has one defined source at each level,
listed in `docs/schema.md`.

We will identify players in layers: exact names within a match, normalized
names within a team's season roster, and the same name on the same team in
consecutive seasons across seasons. A player who transfers gets a new player
record. Linking players across teams is deferred.

## Consequences

- Stats can be filtered, indexed, and aggregated directly in SQL, and
  re-imports update rows in place instead of duplicating them.
- Every number has a known source, and set-level stats always add up to match
  totals.
- Matches whose play-by-play fails reconciliation have no set-level serving or
  blocking stats. Digs are never available per set.
- The importer must parse each response and run the reconciliation check.
  Parser changes require re-parsing stored raw responses.
- A transferred player's career is split across two player records until
  cross-team linking is built.
- The full table design is in `docs/schema.md`. Schema changes that
  contradict these decisions need a new ADR.

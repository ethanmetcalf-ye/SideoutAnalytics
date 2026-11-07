# ADR-004: Refresh Past Scoreboards Until Every Game on Them Is Final

**Status:** Accepted. Supersedes ADR-003.

## Context

ADR-003 set when cached responses are re-fetched. Its scoreboard rule
refreshed dates from yesterday onward and treated older dates as permanent.

That rule assumes the pipeline runs every day. Until it is deployed (W3), it
runs only when a laptop is on. If a scoreboard is fetched before its games
are played and the next run is more than a day later, the cached copy still
says `pre`, and its date is already past the refresh window. The file is
never fetched again, so the season index reports those games as unplayed
forever, and the game fetcher (W2-02) would re-fetch their details on every
run.

This has already happened: in the existing cache, 1,314 October 2026 games on
past dates are still `pre`, because their scoreboards were last fetched before
the games were played.

## Decision

A scoreboard is refreshed if either:

- its date is yesterday or later, as before, or
- its cached copy contains any game whose `gameState` is not `final`.

The second check reads the cached file, so it decides by what the data says
rather than by the date alone. A scoreboard that has never been fetched is a
cache miss and is requested either way.

The other rules from ADR-003 are unchanged:

- **Unplayed matches** need nothing extra. The client raises
  `NotAvailableYetError`, nothing is written, and the next run asks again.
- **Game endpoints** are refreshed while the match's `gameState` on the
  scoreboard is not `final`.
- **Schedules** (`/schedule-alt`) are refreshed for the current season,
  which is named by the year it starts and includes January to June of the
  next year.
- **Undated endpoints** (rankings, standings, `current` stats) are refreshed
  whenever a job wants a new snapshot.
- `RawStore.get(client, endpoint, refresh=True)` replaces the file
  atomically, and a failed refresh keeps the old file.

## Consequences

- Missed days fix themselves: the next run refreshes every past scoreboard
  that still has unplayed games, whenever that run happens.
- A rerun over dates where every game is final still makes zero requests.
- A match that is cancelled or postponed and stays `pre` makes its date
  refresh on every run. That is a handful of extra requests per run, which
  is acceptable. If it grows, those dates can be listed and skipped.
- Each run reads and parses every cached past scoreboard to make the
  decision. That is a few hundred small local files per season.
- `gameState` values are checked against `MatchState`, so an unknown state
  fails with the game's ID instead of being treated as "not final".
- Files for non-final matches may still be stale on disk. The importer
  should treat only final matches as settled.
- Refreshing overwrites the file, so earlier versions of undated endpoints
  are lost on disk. Weekly poll history lives in the database.

## Amendment, 2026-11-07 (W2-02)

The same problem applies to game details. A `/game/{id}` response cached just
after a match can say `hasBoxscore: false` or `hasPbp: false` before the
stats are posted. Final games never refresh, so without a fix the box score
or play-by-play would never be requested, and nothing would report it.

The rule extends the same way: a final game's details are refreshed while
the cached copy says a box score or play-by-play is missing, or can't be
read. Every other file for a final game comes from the cache.

- A few matches genuinely have no play-by-play, so their details are
  re-fetched on every run. That is one small request per such match, the
  same accepted cost as cancelled matches above.

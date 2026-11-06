# ADR-003: Re-fetch Cached Responses Only While They Can Still Change

**Status:** Superseded by ADR-004

## Context

ADR-002 keeps every raw API response as a file on disk so the database can
be rebuilt from those files. The raw response cache (W1-08) saves each
response to a predictable path per endpoint, and a cached endpoint is
returned from disk without a request. A rerun over past data therefore makes
zero requests.

Some responses change after they are first fetched:

- A match's `gameState` is `pre`, `live` or `final`. Box score and
  play-by-play return 502 until the match is played, but once a match is
  live they return partial stats that keep changing until it is final.
  `/game/{id}` returns 200 for upcoming matches too, with `hasBoxscore:
  false`.
- A scoreboard for today or a future date gains scores and new states as
  games finish. Late matches can finish after midnight Eastern.
- Rankings, standings and `current` stats leaderboards have no date in the
  path. The same endpoint returns new data each week.

If these were cached forever, partial or old data would be served on every
later run.

## Decision

A cached response is never re-fetched unless the caller asks for it.
`RawStore.get(client, endpoint, refresh=True)` skips the cache and
atomically replaces the file. A failed refresh keeps the old file.

The cache does not parse responses, so the fetcher decides when to refresh:

- **Unplayed matches** need nothing extra. The client raises
  `NotAvailableYetError`, nothing is written, and the next run asks again.
- **Game endpoints** (`/game/{id}` and its sub-endpoints) are refreshed while
  the match's `gameState` on the scoreboard is not `final`. Once it is
  final, the cached files are permanent.
- **Scoreboards** are refreshed for dates from yesterday onward. Older dates
  are permanent.
- **Undated endpoints** (rankings, standings, `current` stats) are refreshed
  whenever a job wants a new snapshot.

## Consequences

- A rerun over completed dates and final matches makes zero requests.
- Files for non-final matches and recent scoreboards may be stale on disk.
  The importer should treat only final matches as settled.
- Refreshing overwrites the file, so earlier versions of undated endpoints
  are lost on disk. Weekly poll history lives in the database. If raw
  snapshots are needed to rebuild it, dated snapshot paths need a new ADR.
- The refresh rules run in the fetcher (W2), using the `gameState` it
  already reads from the scoreboard. The cache stays a plain
  endpoint-to-file store.

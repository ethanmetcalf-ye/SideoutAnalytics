# ADR-005: Keep a Dated Raw Copy of Each Week's Poll

**Status:** Accepted. Amends the undated-endpoint rule of ADR-003, as kept by
ADR-004.

## Context

ADR-003 refreshes undated endpoints (rankings, standings, `current` stats)
whenever a job wants a new snapshot, and the refresh overwrites the cached
file. Its consequences note that poll history would then live only in the
database, and that keeping raw snapshots would need dated paths and a new
ADR.

Every other table can be rebuilt from the raw cache (schema.md decision 1).
`team_rank` would be the exception, and the API can't help: it serves only
the current week, and historical weeks (`.../avca-coaches/2025/15`) return
404. A dropped table, a bad migration or a loader bug found later would lose
poll history for good.

There are no older snapshots to import. The prototype never saved the AVCA
poll; its "rankings" are its own power ratings. History starts with the
first `pipeline snapshot` run.

## Decision

`pipeline snapshot` refreshes `/rankings/volleyball-women/d1/avca-coaches`
as ADR-003 says, and also saves the response as a dated copy:

```
data/raw/rankings/volleyball-women/d1/avca-coaches.json               latest response
data/raw/rankings/volleyball-women/d1/avca-coaches/2026-11-08.json    one per week
```

- The date is the poll's own, parsed from `updated` ("Through Games NOV. 8,
  2026"), not the day it was fetched. The copy is written once, the first
  time a run sees that week, and never overwritten.
- A response whose date can't be parsed is saved as
  `avca-coaches/undated-{first 12 hex of its sha256}.json` before the run
  fails, so a format change doesn't lose the week.
- Each run loads every saved file whose week isn't in `team_rank` yet. The
  same loop rebuilds `team_rank` from disk after the table is emptied.
- The dated path doesn't collide with any API endpoint: the API's own
  historical form is `/{year}/{week}`, and it returns 404.

## Consequences

- `team_rank` can be rebuilt from raw files like every other table.
- A week that couldn't load (a school with no team match, or a preseason
  poll before its season has a row) stays on disk and loads on a later run.
- The poll is requested on every run, one small request. A run doesn't know
  whether a new week is out until it asks.
- One file per week, about 20 per season.
- Polls are assumed not to be revised once published: a week already saved
  keeps its first copy. If the AVCA ever corrects a poll in place, the
  correction won't be picked up.
- Other undated endpoints (standings, `current` stats) still overwrite. They
  get dated copies only if a job needs their history, under a new ADR.

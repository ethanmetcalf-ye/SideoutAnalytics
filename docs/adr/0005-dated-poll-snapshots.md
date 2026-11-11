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

The prototype (`~/workspace/ethanmetcalf/Volleyball`, commit `b6d6fe6`,
`snapshot_rankings.py`) did save the poll, in the same response format,
one file per week:

```
data/raw/rankings/avca-coaches/2026-10-04.json
data/raw/rankings/avca-coaches/2026-10-11.json
data/raw/rankings/avca-coaches/2026-10-18.json
data/raw/rankings/avca-coaches/2026-10-25.json
```

The API can't serve those weeks again, so these four files are the only copy.
The Oct. 4 file ends with a row whose fields are all blank; the old script
skipped it with `if row["RANK"]`.

The poll through Nov. 1, 2026 was never saved by either pipeline and is lost.
History has that gap.

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
  `avca-coaches/undated-{first 12 hex of its sha256}.json`, so a format
  change doesn't lose the week. It's then reported like any unreadable file.
- Each run loads every saved file whose week isn't in `team_rank` yet. The
  same loop rebuilds `team_rank` from disk after the table is emptied.
- The dated path doesn't collide with any API endpoint: the API's own
  historical form is `/{year}/{week}`, and it returns 404.
- Each saved file is parsed on its own. A file that can't be parsed is
  reported as an `unreadable` week and fails the run, while every other week
  still loads.

### Importing the prototype's weeks

The old files are already named by date, so they go straight into the dated
folder. Run once, from the repo root, with `DATA_DIRECTORY` as in `.env`
(`./data` by default):

```sh
mkdir -p ./data/raw/rankings/volleyball-women/d1/avca-coaches
cp -n ~/workspace/ethanmetcalf/Volleyball/data/raw/rankings/avca-coaches/2026-10-*.json \
  ./data/raw/rankings/volleyball-women/d1/avca-coaches/
uv run pipeline snapshot
```

`cp -n` never overwrites a week that's already there. The next snapshot run
loads the four weeks along with any newer ones. Until backups exist (W3-06),
keep a copy of these files somewhere other than the laptop.

## Consequences

- `team_rank` can be rebuilt from raw files like every other table.
- A week that couldn't load (a school with no team match, or a preseason
  poll before its season has a row) stays on disk and loads on a later run.
- The poll is requested on every run, one small request. A run doesn't know
  whether a new week is out until it asks.
- One file per week, about 20 per season. 2026 has Oct. 4, 11, 18 and 25
  from the prototype, then Nov. 8 on; Nov. 1 is missing.
- Polls are assumed not to be revised once published: a week already saved
  keeps its first copy. If the AVCA ever corrects a poll in place, the
  correction won't be picked up.
- Other undated endpoints (standings, `current` stats) still overwrite. They
  get dated copies only if a job needs their history, under a new ADR.

# Validation against the source (W2-06)

`pipeline validate` checks the loaded 2026 season against the raw API files
and the NCAA's published stats. Each check is a query that returns the rows
breaking its rule; zero rows passes. This page records the run of
**Nov. 11, 2026** and explains every failing row from its raw file.

The prototype database isn't used: it was never verified, so matching it
proves little.

**Run:** `uv run pipeline poll --reload-all`, then `uv run pipeline validate`.
The reload was needed because this PR changes how play-by-play is ordered
(see [Fixed](#fixed)). The Total Kills leaderboard was through games of
Nov. 9, 2026.

| Check | Failing rows | Result |
| --- | --- | --- |
| Every final game on the cached scoreboards has a final `match` row | 0 | Pass |
| Season index game counts per date match `/schedule-alt` | 13 | Explained (source), cause not in the files |
| Each set's `team_set_stats.points` equals its last play's score | 65 | Explained (source); 197 more were a parser bug, **fixed** |
| Team kills, attack errors and attempts over sets equal `box_score_line` | 6 | Explained: 4 source, 2 loader |
| `sets_won` and `is_winner` agree with the set points | 5 | Explained (source) |
| Top 5 total kills match `/stats/volleyball-women/d1/current/individual/879` | 0 | Pass |

A failure is **source** when the raw files disagree with themselves, so no
loader could make the rule hold without inventing data. The loader stores
what the source says (`docs/schema.md` decision 2), so those rows stay.

## Fixed

**Play-by-play out of order.** Before this PR the last-play check failed on
262 sets in 193 games. In 154 of them the feed has the set's final rally and
then appends stray rows with earlier scores. For example, game `6624442`
set 2 ends 22-25 and is followed by a 1-0 row. The parser kept feed order,
so a stray row became the set's "last" play. Its `sequence`, the inferred
server and the swap check were wrong too, which is why another 45 sets
looked swapped.

`parse_play_by_play` now orders each set's rallies by score total, since
every rally adds exactly one point, and drops a row repeated later in its
set with the same text and score. Rallies with the same total and different
text or score are a scorer's re-entry of one point. There are about 270 in
2026, and both copies are kept in feed order. A policy for choosing between
them is a follow-up. This brought the failures from 262 to 65.

## Season index vs `/schedule-alt`

On 13 dates `/schedule-alt` counts one more game than the scoreboard lists
(two more on Sep. 19). It is never the other way around.

| Date | Schedule | Scoreboard |
| --- | --- | --- |
| 2026-08-29 | 184 | 183 |
| 2026-09-04 | 176 | 175 |
| 2026-09-05 | 160 | 159 |
| 2026-09-11 | 187 | 186 |
| 2026-09-12 | 161 | 160 |
| 2026-09-15 | 32 | 31 |
| 2026-09-19 | 137 | 135 |
| 2026-09-29 | 13 | 12 |
| 2026-10-02 | 122 | 121 |
| 2026-10-06 | 14 | 13 |
| 2026-10-20 | 9 | 8 |
| 2026-10-30 | 128 | 127 |
| 2026-11-14 | 99 | 98 |

What the raw files show:

- No scoreboard lists a game twice, so the scoreboard isn't miscounting.
- The missing games aren't on a neighbouring date's scoreboard. Over the
  season the schedule's total is higher, not just moved around.
- 2025 has the same pattern: 5,139 on the schedule, 5,131 on the
  scoreboards, a gap on 7 dates (`docs/source-data-api-endpoints.md` already
  noted the 8).
- 2026-11-14 hasn't been played yet, so the gap exists before a game can be
  cancelled.

`/schedule-alt` gives only a count per date, no game IDs, so the files can't
say which games these are. Since the season index is built from the
scoreboards, a game the scoreboard never lists can't be loaded. That is 14
games in 2026: the season's schedule total is 4,834, its scoreboards list
4,820. Every game the scoreboards do list is loaded (check 1).

## Set points vs the last play

All 65 remaining sets are feed problems. Each row was checked against its
raw `play-by-play.json` and `/game/{id}.json` linescores.

| Cause | Sets | What the raw feed shows |
| --- | --- | --- |
| Final rally missing | 47 | The feed stops one point short of the linescore, e.g. `6624379` set 1: 24-19, final 25-19 |
| Feed stops early | 8 | Several points missing, e.g. `6624664` set 4: 21-25, final 28-35 |
| Re-entry gives the last point to the other team | 6 | The rally count is right but one point is credited to the wrong team, e.g. `6624529` set 2: 19-24, final 18-25 |
| Feed has a point the linescore doesn't | 2 | `6625184` set 3: 18-25, final 17-25; `6628063` set 1: 25-19, final 25-18 |
| Sets swapped, not the whole match | 3 | `6625786` sets 2 and 3 have home and visitor reversed, set 1 is reversed and a point short. The parser swaps scores back only when every set is reversed |

| Game | Set | Linescore | Last play | Cause |
| --- | --- | --- | --- | --- |
| `6624379` | 1 | 25-19 | 24-19 | final rally missing |
| `6624529` | 2 | 18-25 | 19-24 | re-entry gives the last point to the other team |
| `6624529` | 3 | 25-22 | 24-22 | final rally missing |
| `6624604` | 3 | 20-25 | 20-23 | feed stops early |
| `6624606` | 1 | 24-25 | 23-25 | final rally missing |
| `6624633` | 2 | 20-25 | 19-26 | re-entry gives the last point to the other team |
| `6624657` | 1 | 25-22 | 24-22 | final rally missing |
| `6624664` | 4 | 28-35 | 21-25 | feed stops early |
| `6624678` | 1 | 25-10 | 24-10 | final rally missing |
| `6624728` | 3 | 26-28 | 26-27 | final rally missing |
| `6624738` | 3 | 22-25 | 23-24 | re-entry gives the last point to the other team |
| `6624818` | 4 | 25-20 | 24-20 | final rally missing |
| `6624825` | 3 | 25-21 | 24-21 | final rally missing |
| `6624833` | 2 | 14-25 | 14-24 | final rally missing |
| `6624886` | 2 | 25-17 | 24-17 | final rally missing |
| `6624899` | 1 | 22-25 | 22-24 | final rally missing |
| `6624938` | 4 | 18-10 | 9-5 | feed stops early |
| `6625060` | 2 | 25-15 | 24-15 | final rally missing |
| `6625179` | 4 | 25-22 | 24-22 | final rally missing |
| `6625184` | 3 | 17-25 | 18-25 | feed has a point the linescore doesn't |
| `6625243` | 2 | 24-26 | 24-25 | final rally missing |
| `6625341` | 1 | 22-25 | 22-24 | final rally missing |
| `6625521` | 2 | 25-14 | 24-14 | final rally missing |
| `6625538` | 3 | 25-21 | 25-20 | final rally missing |
| `6625559` | 1 | 32-30 | 31-30 | final rally missing |
| `6625655` | 3 | 25-27 | 25-26 | final rally missing |
| `6625786` | 1 | 22-25 | 24-22 | sets swapped, and the final rally missing |
| `6625786` | 2 | 22-25 | 25-22 | sets swapped, not the whole match |
| `6625786` | 3 | 18-25 | 25-18 | sets swapped, not the whole match |
| `6625799` | 1 | 19-25 | 19-24 | final rally missing |
| `6625840` | 3 | 21-25 | 21-24 | final rally missing |
| `6625879` | 2 | 20-25 | 20-24 | final rally missing |
| `6625908` | 3 | 27-20 | 25-19 | feed stops early |
| `6625916` | 2 | 26-24 | 25-24 | final rally missing |
| `6626070` | 1 | 25-20 | 24-20 | final rally missing |
| `6626495` | 4 | 29-20 | 25-17 | feed stops early |
| `6626620` | 1 | 25-12 | 24-12 | final rally missing |
| `6626646` | 2 | 20-25 | 20-24 | final rally missing |
| `6626668` | 5 | 14-16 | 14-15 | final rally missing |
| `6626688` | 3 | 18-25 | 17-25 | final rally missing |
| `6626793` | 1 | 23-25 | 23-24 | final rally missing |
| `6626836` | 2 | 16-25 | 16-24 | final rally missing |
| `6626860` | 1 | 25-22 | 24-22 | final rally missing |
| `6626977` | 3 | 22-25 | 22-24 | final rally missing |
| `6626981` | 4 | 25-16 | 24-16 | final rally missing |
| `6626991` | 3 | 22-25 | 22-24 | final rally missing |
| `6627525` | 2 | 23-25 | 23-24 | final rally missing |
| `6627680` | 1 | 22-25 | 22-24 | final rally missing |
| `6627765` | 4 | 17-25 | 17-23 | feed stops early |
| `6627821` | 4 | 26-24 | 25-24 | final rally missing |
| `6627904` | 1 | 21-25 | 20-23 | feed stops early |
| `6628063` | 1 | 25-18 | 25-19 | feed has a point the linescore doesn't |
| `6628074` | 3 | 25-19 | 24-20 | re-entry gives the last point to the other team |
| `6628201` | 2 | 23-25 | 23-24 | final rally missing |
| `6628411` | 1 | 25-19 | 24-19 | final rally missing |
| `6628507` | 1 | 25-14 | 24-14 | final rally missing |
| `6628518` | 1 | 25-17 | 24-17 | final rally missing |
| `6628537` | 3 | 25-10 | 24-10 | final rally missing |
| `6628678` | 4 | 25-16 | 24-16 | final rally missing |
| `6637545` | 3 | 25-19 | 25-18 | final rally missing |
| `6637549` | 3 | 18-25 | 18-24 | final rally missing |
| `6637949` | 2 | 25-8 | 22-8 | feed stops early |
| `6638149` | 4 | 19-25 | 20-24 | re-entry gives the last point to the other team |
| `6639652` | 1 | 26-23 | 25-23 | final rally missing |
| `6640142` | 4 | 27-25 | 26-26 | re-entry gives the last point to the other team |

## Set attack totals vs the box score

| Game | Team | Sets (K / E / TA) | Box (K / E / TA) | Cause |
| --- | --- | --- | --- | --- |
| `6624788` | la-lafayette | 59 / 17 / 149 | 58 / 17 / 148 | **Loader.** All 11 box score lines are unnamed. Their raw total is 59 / 17 / 149, matching the sets, but one line (1 kill, 1 attempt) matched no roster jersey and was skipped |
| `6628012` | southern-no | 36 / 31 / 158 | none | **Loader.** All 13 lines are unnamed, and no jersey matched a roster player, so every line was skipped. The raw lines total 36 kills, matching the sets |
| `6627552` | grambling | 41 / 11 / 91 | 40 / 9 / 87 | Source. The raw box score's player lines total 40 / 9 / 87 against its own per-set 41 / 11 / 91 |
| `6628301` | mississippi-st | 34 / 12 / 101 | 22 / 8 / 75 | Source. All 11 raw lines are unnamed and total 22 / 8 / 75; the per-set stats say 34 / 12 / 101 |
| `6640466` | jacksonville-st | 0 / 0 / 0 | 32 / 12 / 84 | Source. Every per-set attack stat is 0 for both teams |
| `6640466` | southern-miss | 0 / 0 / 0 | 38 / 7 / 80 | Source, same game |

The two loader rows come from its documented rule (`db/loader.py`): an
unnamed box score line is matched to the roster by jersey number, and skipped
when no player or more than one has the number. Those games keep no
`raw_response` rows, so every run retries them as more rosters load.

## `sets_won` and `is_winner` vs the set points

| Game | Team | `sets_won` | On points | `is_winner` | On points | Raw `/game/{id}.json` |
| --- | --- | --- | --- | --- | --- | --- |
| `6625819` | baylor | 3 | 3 | false | true | `isWinner` is false for **both** teams; Baylor won the sets 3-2 |
| `6626005` | new-mexico-st | 2 | 3 | false | true | `score` is 2-2 and neither team is the winner; New Mexico St. won set 5 15-8 |
| `6626620` | uconn | 2 | 3 | true | true | `score` is 2-1; the linescores are 3-1 |
| `6628533` | ohio-st | 2 | 3 | true | true | `score` is 0-2; the linescores are 0-3 |
| `6640466` | southern-miss | 3 | 0 | true | false | Every linescore is 0-0; `score` 0-3 is the only result in the file |

In the first four the `score` or `isWinner` fields lag the linescores, which
look like an unfinished last update. In `6640466` the linescores are missing
entirely. The loader keeps the source's values; anything that needs a winner
should use `is_winner` and treat these five as known exceptions.

## Top 5 total kills

Pass. The database's Division I top 5 through Nov. 9, 2026 equals page 1 of
`/stats/volleyball-women/d1/current/individual/879` as fetched on Nov. 11.
The check compares players, not places, because the API lists tied players
(`Rank: "-"`) in its own order. The response is saved in the raw cache,
refreshed on every run. A copy fetched Nov. 11 (updated Nov. 10, through
Nov. 9) is the test fixture.

## Follow-ups

- **Re-entered rallies.** About 270 2026 rallies share a score total with
  another rally in their set. A rule for which copy to keep needs evidence
  this data doesn't give.
- **Partly swapped feeds** like `6625786`. The swap check could work per set
  instead of per match.
- **The schedule surplus.** If the missing games matter, another source with
  game IDs is needed to find them.

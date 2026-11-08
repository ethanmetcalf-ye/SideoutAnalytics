# NCAA API: Volleyball Endpoints

Copied from the prototype repository; last checked against the live API on October 6, 2026.

Unofficial NCAA data API by henrygd. It proxies data from ncaa.com.

- Docs: https://ncaa-api.henrygd.me/openapi
- Source: https://github.com/henrygd/ncaa-api
- Base URL: `https://ncaa-api.henrygd.me`
- Auth: none
- Rate limit: 3 requests/second per IP on the public instance. The limit is not in the API's code, so a self-hosted instance (`docker run --rm -p 3000:3000 henrygd/ncaa-api`) has none.
- HTTP 428: the public instance returned `428 Precondition Required` on game endpoints during bursts of concurrent requests (2026-11-07). The same requests returned 200 when sent one at a time. The API's own code only sends 400, 401 and 404, so the 428 comes from the proxy layers in front of it (Cloudflare, BunnyCDN, Caddy). The client treats 428 as retryable.
- Format: JSON, except `/logo/...` which returns SVG.

All endpoints below were tested against the live API on 2026-10-06 unless marked **untested** or **broken**.

## Sport and division codes

| Sport | `{sport}` | `{division}` |
|---|---|---|
| Women's volleyball | `volleyball-women` | `d1` (`d2`, `d3` are likely but untested) |
| Men's volleyball | `volleyball-men` (**untested**) | untested |

The examples below use `volleyball-women/d1`.

---

## Scoreboard

```
GET /scoreboard/{sport}/{division}                       # most recent game day
GET /scoreboard/{sport}/{division}/{YYYY}/{MM}/{DD}      # every game on that date, e.g. /scoreboard/volleyball-women/d1/2025/10/01
```

Use the full date. `/{YYYY}/{MM}` is accepted but only returns the 1st of that month, and a trailing `/all-conf` changes nothing. A day with no games returns an empty `games` list. The 2025 season ran from 2025-08-22 to 2025-12-21 and the scoreboard returns 5,131 games for it.

The response can list the same `gameID` twice on one day, so dedupe by `gameID`.

`gameState` is `final`, `live` or `pre`. Upcoming matches (`pre`) already carry both teams, the date and start time, each team's conference, and sometimes a national rank. Scores are empty strings. Their `/game/{id}/boxscore` and `/play-by-play` return 502 until the match is played, and `/game/{id}` has `hasBoxscore: false`.

Response:
```
{ inputMD5Sum, instanceId, updated_at, games: [ { game: {...} }, ... ] }
```

Fields on each `game`:
- `gameID`: use this for `/game/{id}` endpoints
- `away`, `home`: each has `score`, `names{char6, short, seo, full}`, `winner`, `seed`, `rank`, `conferences[]`. `rank` is the team's national ranking at the time of the match and is blank for unranked teams and in the tournament, where `seed` is set instead.
- `gameState` (for example `final`), `finalMessage`, `currentPeriod`
- `startTime`, `startTimeEpoch`, `startDate`
- `url`, `network`, `bracketId`, `bracketRound`

## Game details

All take the `gameID` from the scoreboard (for example `6480202`).

```
GET /game/{gameId}                  # metadata
GET /game/{gameId}/boxscore         # player and team stats
GET /game/{gameId}/team-stats       # returned the same shape as boxscore in testing
GET /game/{gameId}/play-by-play
GET /game/{gameId}/scoring-summary  # BROKEN for volleyball: returns an HTML error page
```

### `/game/{id}`
Response: `{ contests: [ {...} ] }`. Keys on each contest:
- `id`, `sportCode` (`WVB`), `sportUrl`, `division`
- `gameState`, `finalMessage`, `startTime`, `startTimeEpoch`, `seasonYear`
- `linescores` (set-by-set scores), `teams`, `winner`, `location` (venue, city, state; often null), `links`, `stats`
- Each team has `teamId`, `record` (W-L after the match), `teamRank` and `seed`
- Availability flags: `hasBoxscore`, `hasPbp`, `hasScoringSummary`, `hasPreview`, `hasRecap`, `hasTeamStats`. In a 120-game sample, box score and play-by-play were always available and preview and recap never were.
- Also `championship` and `championshipGame` (for tournament games), `liveVideos`

### `/game/{id}/boxscore`
Response:
```
{
  contestId, title, description, sportCode, divisionName, period, status,
  teams: [ { isHome, teamId, seoname, name6Char, nameFull, nameShort, teamName, color } ],
  teamBoxscore: [
    {
      teamId,
      playerStats: [ ... one object per player ... ],
      teamStats:   { ... team totals + per-set attack ... }
    }
  ]
}
```

`playerStats` fields (all values are strings):
`firstName, lastName, position, number, gamesPlayed, starter, participated, points, kills, attackErrors, attackAttempts, hittingPercentage, assists, setErrors, setAttempts, serviceAces, serviceErrors, serveAttempts, digs, receptionAttempts, receptionErrors, blockSolos, blockAssists, blockingErrors, totalBlocks, ballHandlingErrors`

`teamStats` has the same totals plus `sets: [ { game, kills, attackErrors, attackAttempts, hittingPercentage } ]` for per-set attacking.

### `/game/{id}/play-by-play`
Response:
```
{
  contestId, title, description, status, period, teams,
  periods: [
    { periodNumber, periodDisplay,
      playbyplayStats: [ { teamId, clock, plays: [ { playText, homeScore, visitorScore, clock } ] } ] }
  ]
}
```

- There is one period per set and one play per entry.
- `teamId` is the team that won the point. On `starters` and `subs` entries it is the team named.
- `playText` is free text: `Kill by X (from Y).`, `Attack error by X (block by A, B).`, `Service ace (X).`, `Service error.`, `Bad set by X.`, `Ball handling error by X.`, `<Team> starters: ...` and `<Team> subs: ...`.
- On `Service ace (X)`, X is the receiver who was aced. The server is not named.
- Quality varies by match. Some feeds repeat plays without a score, and `homeScore`/`visitorScore` are null on lineup entries.
- Checked against 64 cached games in November 2026:
  - **Swapped scores:** in 10 games (all 2026), `homeScore` and `visitorScore` are swapped for the whole match. Every set's last score is the reverse of the linescores, while `isHome` and the point winner's `teamId` are correct.
  - **Repeated rows:** some feeds list most rallies twice back to back, with the same text, score and clock.
  - **Scoreless plays:** `Ball handling error  by X` rows (with a double space) have no score, and the score doesn't change.
  - **Double spaces in names:** the feed writes `Ava  Grevengoed` where the box score has `Ava Grevengoed`.
  - **Truncated or inconsistent feeds:** a few games have a feed that stops before the set ends, or a handful of rallies whose `teamId` doesn't match the side whose score went up.
- `/game/{id}` details write `gameState` as one letter: `F`, `P` or `I` (final, pre, live). The scoreboard spells them out.
- Linescore `period` labels: in 2025, 442 matches use `1st`, `2nd`, `3rd`, `4th` and `5th`, while the rest use `1` to `5`. Every 2026 match uses digits.
- Non-NCAA opponents (NAIA teams such as Tougaloo, Dillard and Lindsey Wilson) have `divisionName` and `division` set to null. That happened in 16 matches in 2025.
- Some box scores leave `firstName` and `lastName` empty for every player, with only the jersey `number` set. This was seen on a match with no play-by-play.
- In 195 matches (2025 and 2026), the box score's `setAttempts` is `""` for every player, while the other counts are filled in and the team total still has a value. No other count has been seen blank.
- Scoreboard `bracketRound` is a number for NCAA tournament matches, from 1 (first round) to 6 (the final), and `""` otherwise.

## Rankings

```
GET /rankings/{sport}/{division}                  # default poll
GET /rankings/volleyball-women/d1/avca-coaches    # same result as default
```

- The default poll is the AVCA Coaches Poll.
- Response: `{ sport, title, updated, page, pages, data: [ { RANK, SCHOOL, "TOTAL POINTS", RECORD, "PREVIOUS RANK" } ] }`
- `SCHOOL` includes first-place votes, for example `"Nebraska (47)"`.
- `updated` reads like `Through Games OCT. 4, 2026`. School names are long-form (`Penn State`, `Arizona State`), unlike the `Penn St.` used elsewhere.
- **Only the current week is available.** Historical weeks (`.../avca-coaches/2025/15`) return 404, so a poll that isn't saved while it is current is gone. The pipeline's `snapshot` command saves it.
- `ncaa-rpi` returned 404. Other poll names are untested.

## Stats (season leaderboards)

```
GET /stats/{sport}/{division}/{season}/{type}/{statId}
```

- `season`: `current` or a year such as `2025`. `/2025/individual/1` worked.
- `type`: `individual` or `team`
- `statId`: see the tables below. Use `0` as the ID to list the stat IDs for that type.
- Pagination: `?page=2` or `/p2` appended to the path. Responses include `page` and `pages`. Hitting Percentage had 3 pages of 50 rows.
- Team stat IDs only work with `type=team`. Individual IDs only work with `type=individual`. For example, `/team/1` returned a 500.

Individual response row (stat 1):
`{ Rank, Name, Team, Cl, Height, Position, S, Kills, Errors, "Total Attacks", "Pct." }`

The columns vary by stat. The envelope is `{ sport, title, updated, page, pages, data: [...] }`.

Discovery:
```
GET /stats/volleyball-women/d1/current/individual/0
GET /stats/volleyball-women/d1/current/team/0
```

### Individual stat IDs

| ID | Stat |
|---|---|
| 1 | Hitting Percentage |
| 2 | Kills Per Set |
| 3 | Assists Per Set |
| 42 | Aces Per Set |
| 43 | Blocks Per Set |
| 44 | Digs Per Set |
| 85 | Kills SG Highs |
| 552 | Points Per Set |
| 553 | Points |
| 879 | Total Kills |
| 880 | Total Assists |
| 883 | Service Aces |
| 885 | Total Digs |
| 887 | Total Blocks |
| 889 | Total Attacks |
| 890 | Attacks Per Set |
| 928 | Triple Doubles |

### Team stat IDs

| ID | Stat |
|---|---|
| 45 | Hitting Percentage |
| 46 | Kills Per Set |
| 47 | Assists Per Set |
| 48 | Aces Per Set |
| 49 | Blocks Per Set |
| 50 | Digs Per Set |
| 51 | Match W-L Pctg. |
| 881 | Team Kills |
| 882 | Team Assists |
| 884 | Team Service Aces |
| 886 | Team Digs |
| 888 | Team Total Blocks |
| 891 | Team Total Attacks |
| 892 | Team Attacks Per Set |
| 911 | Opp Hitting Percentage |

## Brackets

```
GET /brackets/{sport}/{division}/{year}      # e.g. /brackets/volleyball-women/d1/2025
```

The response is the NCAA tournament bracket: `{ championships: [ { title, year, championshipId, bracketTemplate, games: [ { contestId, bracketPositionId, ... } ] } ] }`. A `contestId` should work as the `gameId` for `/game/...`.

## News

```
GET /news/{sport}/{division}                 # e.g. /news/volleyball-women/d1
```

The response is the ncaa.com RSS feed as JSON: `{ title, link, description, language, items: [ { title, link, description, ... } ] }`.

## Schedule

```
GET /schedule-alt/{sport}/{division}/{year}       # e.g. /schedule-alt/volleyball-women/d1/2025
GET /schedule/{sport}/{division}/{year}/{month}   # BROKEN
```

`/schedule-alt` works. It returns every date in the season that has games, with a count: `{ data: { schedules: { games: [ { contestDate, count } ], today } } }`. For 2025 it lists 5,139 games on 104 dates, 8 more than the scoreboard returns. Every date the scoreboard has matches on is in this list, so it works as the list of days to request. For a season in progress the list changes: the 2026 schedule runs to 2027-01-23, with only single matches after November.

`/schedule` is broken. Every request I tried (`2025/10`, `2025/09`, `2026/10`) failed validation with `Invalid type: Expected number but received "10"`, and the `/schedule/.../{year}` form returned 404.

## Standings

```
GET /standings/{sport}/{division}
```

**Broken for volleyball.** `/standings/volleyball-women/d1` returned HTTP 500 with "Could not parse data". The README's example for this endpoint uses basketball.

## Sport-independent endpoints

```
GET /schools-index                           # [{ slug, name, long }, ...]
GET /logo/{school-slug}.svg                  # add ?dark=true for the dark variant
```

---

## Typical workflow

1. `GET /scoreboard/volleyball-women/d1/{YYYY}/{MM}/{DD}` for each date to get the `gameID`s.
2. `GET /game/{gameID}/boxscore` for player and team stats per match.
3. `GET /stats/volleyball-women/d1/{season}/individual/{statId}` for season leaderboards.

The `data_ingestion` package does steps 1 and 2 for a whole season and loads the result into SQLite. Run `python -m data_ingestion --help` from the project root.

## Python example

```python
import time
import requests

BASE = "https://ncaa-api.henrygd.me"

def get(path):
    time.sleep(0.34)  # stay under 3 req/s
    r = requests.get(f"{BASE}{path}")
    r.raise_for_status()
    return r.json()

games = get("/scoreboard/volleyball-women/d1/2025/10/01")["games"]
for g in games:
    box = get(f"/game/{g['game']['gameID']}/boxscore")
    for team in box["teamBoxscore"]:
        for p in team["playerStats"]:
            print(p["lastName"], p["kills"], p["digs"], p["hittingPercentage"])

```

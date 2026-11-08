# Test fixtures

Real API responses for the parser tests (W2-03), fetched November 2026.
The files are the exact bytes the API returned, so don't reformat them:
the parsers have to handle the real whitespace and key order, and the
`content_hash` of a fixture should match what the fetcher would store.

`raw/` uses the same layout as the raw cache, so
`RawStore(pathlib.Path("tests/fixtures/raw"))` can read it.

| Game | Match | Why it's here |
| --- | --- | --- |
| `6625808` | Kennesaw St. vs The Citadel, 3 sets | Clean match: every play is scored and its text matches a known pattern. |
| `6628152` | NIU vs Eastern Mich., 5 sets | Five sets, for set 5 and `set_number` up to 5. Its play-by-play has `homeScore` and `visitorScore` swapped for the whole match, so it also covers the swap check. Names are written with double spaces (`Ava  Grevengoed`). |
| `6639889` | Wisconsin vs Louisville, 3 sets | Messy play-by-play: about half the rallies are listed twice with the same text, score and clock, a ball handling error has no score and a double space (`Ball handling error  by ...`), and home and visitor scores are swapped. |
| `6624785` | Louisiana vs Louisiana Tech, 3 sets | No play-by-play: details say `hasPbp: false` and the endpoint returns 502, so there is no `play-by-play.json`. Every box score row has blank first and last names, only jersey numbers. |
| `6482569` | Tougaloo vs Mississippi Val., 3 sets (2025) | 2025 linescores labelled `1st`, `2nd`, `3rd`. Tougaloo is NAIA, so its `divisionName` is null. Set 1's last two rallies are listed out of order (`19-25` before `19-24`), so it's left out of the tests that follow the score rally by rally. |
| `6500656` | Nebraska vs LIU, 3 sets (2025 NCAA tournament, first round) | `setAttempts` is `""` for every player while the other counts are filled in. |
| `6479354` | Alabama St. vs Duke, 2025 | Both teams have an outside hitter named Taylor Williams. Set 2 rally 20, `Kill by Taylor Williams (from Millie Muir).`, has an attacker whose name is on both teams, and set 2 rally 26, `Kill by Avery Hamlin (from Taylor Williams).`, has a `(from ...)` player on both teams. Covers `teamId` breaking those ties (issue #77). |
| `6479084` | Ole Miss vs Louisiana, 2025 | A rally credited to the wrong team: set 2 rally 19, `Kill by Grace Sweeney (from Ryleigh Garis).`, is credited to Ole Miss though both players are on Louisiana. Covers placing a kill's players by name, not by the rally's team (issue #77). |

Every game has `/game/{id}` details (`game/{id}.json`) and a box score.
All eight are final, and their details say `gameState: "F"`, not `final`
as on the scoreboard.

`scoreboard/volleyball-women/d1/2025/12/05.json` is the scoreboard for the
first two rounds of the 2025 NCAA tournament, for `bracketRound`.

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

Every game has `/game/{id}` details (`game/{id}.json`) and a box score.
All four are final, and their details say `gameState: "F"`, not `final`
as on the scoreboard.

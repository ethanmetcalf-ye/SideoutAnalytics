
Assumptions:
 - Data covers multiple seasons
 - Players can only transfer between seasons
 - Time-phased stats will be calculated at run time and cached to prevent heavy storage of all permutations. Can revisit in future if it makes sense to store some stat calculations/leaderboards. For now, my reasoning is that too many permutations exist to feasibly store and retrieve from disk


Stat sources: box score is truth for match totals, play by play only used if it reconciles with box score
 - kills, attack errors, attempts: box score (match), API set stats (set)
 - set scores: linescores (match and set)
 - aces, serve errors, block points: box score (match), play by play (set)
 - serve attempts, sideouts, serve points: play by play (match and set)
 - digs, receptions, assists: box score (match), not available by set
 - only store counts, rates and totals calculated at query time


PLAYER: one row corresponds to one player, team and season membership is in ROSTER
 - player_id = primary key, no semantic meaning, auotincrement should be fine since no secutiry concerns from locating one from another
 - first_name: as the source writes it, not null
 - last_name: as the source writes it, not null
 - name_key: normalized name (lowercase, no accents or punctuation), only used for matching
 - API has no player ids. Same name_key on the same team in consecutive seasons = same player. A transfer gets a new player_id for now (cross-season linking is on the backlog)

TEAM: on row corresponds to one team, conference and other per season info is in TEAMSEASON
 - team_id = primary key, no semantic meaning, autoinc fine
 - seoname = unique, API seoname (west-virginia), stable across seasons, matching key when importing
 - team_name = name of the school, not abbreviated (API nameShort)

CONFERENCE: one row per conference
 - conference_id = primary key, autoinc
 - name = unique

TEAMSEASON: one row per team, per season
 - team_id = primary key, foreign key referencing team(team_id)
 - season = primary key, foreign key referencing season(year)
 - source_team_id = API teamId, may change by season so stored here not on TEAM
 - conference_id = foreign key referencing conference(conference_id)
 - head_coach = nullable, not in the API so entered manually
 - assistant_coaches = nullable list, not in the API so entered manually
 - final tournament round is not stored, derived from MATCH bracket_round
 
SEASON: one row corresponds to one season
 - year: primary key INT of starting year
 - start_date: NotNULL, date of first match
 - end_date: NotNull, date AFTER last match start (in case ends after midnight)

 ROSTER: one row per player, per team, per season (a transfer is a new row on a different team)
  - player_id = primary key, foreign key referencing player(player_id)
  - team_id = primary key
  - season = primary key
  - team_id / season = foreign key referencing teamseason(team_id, season), team must have that season
  - name_key: copied from PLAYER so it can be in the unique below
  - unique on team_id / season / name_key, matching key when importing box scores
  - number = jersey number, tiebreak for same name on same team
  - position

MATCH: one row corresponds to one match between two teams
 - match_id: primary key, identifier of match
 - source_game_id: unique, API gameID, used to update existing row on re-import instead of duplicating
 - season: foreign key referencing SEASON(year), from API seasonYear
 - start_time: timestamp with time zone, from API startTimeEpoch
 - home_team_id: foreign key referencing TEAM, from API isHome (API picks a home team even at neutral sites)
 - away_team_id: foreign key referencing TEAM, constraint not same as home
 - is_neutral_site: nullable, NULL = unknown since API has no flag
 - winner_team_id: nullable until match is final, constraint home or away team
 - state: pre, live, or final (API gameState)
 - bracket_round: nullable, NULL if not a tournament match (API bracketRound)
 - playbyplay_available
 - pbp_reconciled: nullable, true if play by play totals match box score, NULL if not checked
 - pbp_parser_version: parser version that checked it, so can re-parse after fixes
 - result_first_seen_at: nullable, time of the first poll that returned the final result (SoW: display within 15 min of this)
 - updated_at: last time the row changed, for last update time and stale status on the site
 - ingest_run_id: foreign key referencing INGESTRUN, run that last updated the row

 TEAMRANK: one row corresponds a teams ranking in one poll at a point in time
 - team: primary key, reference to TEAM
 - poll: primary key, which poll (avca-coaches for now, the API default)
 - date: primary key, date of ranking (from API "updated", Through Games ...)
 - season: foreign key referencing season(year)
 - ranking: INT, ranking of team
 - total_points: poll points
 - first_place_votes: from SCHOOL, "Nebraska (47)" = 47
 - API only serves the current week, so a poll not saved that week is gone

TEAMALIAS: one row per alternate team name, for sources without seoname (rankings use "Penn State" not "Penn St.")
 - alias: primary key, name as the source writes it
 - team_id: foreign key referencing team(team_id)

INGESTRUN: one row per pipeline run
 - ingest_run_id: primary key
 - job: which job ran (scoreboard poll, game details, rankings snapshot)
 - started_at
 - finished_at: nullable while running
 - status: running, succeeded, failed
 - error: nullable, error message if failed

PREDICTION: model output, kept separate from facts. Never updated, a new prediction is a new row
 - prediction_id: primary key
 - match_id: foreign key referencing MATCH(match_id)
 - model_version
 - home_win_probability
 - created_at: must be before match start_time to count (SoW: at least 30 min before)



TEAMSETSTATS: one row per team, per set, per match (no home/away or winner/loser columns)
 - match_id: primary key, reference MATCH(match_id)
 - set_number: primary key, constrained to 1-5
 - team_id: primary key, reference TEAM (constraint home or away team of match)
 - points
 - kills
 - attack_errors
 - attack_attempts
 - aces: NULL if pbp not reconciled
 - serve_errors: NULL if pbp not reconciled
 - serve_attempts: NULL if pbp not reconciled
 - block_points: NULL if pbp not reconciled
 - sideouts: NULL if pbp not reconciled
 - sideout_attempts: NULL if pbp not reconciled
 - serve_points: NULL if pbp not reconciled

BOXSCORELINE: one row per player, per team, per match
 - match_id - primary
 - player_id - primary
 - team_id: not in key, player only plays for one side in a match
 - kills
 - attack_errors
 - attack_attempts
 - aces
 - serve_errors
 - serve_attempts
 - blocks_solo
 - blocks_assist
 - digs
 - reception_attempts
 - reception_errors
 - assists

PLAY
 - playid
 - sequence
 - match_id
 - set_number
 - unique on match_id / set_number / sequence so re-imports don't duplicate rallies
 - play_type
 - scoring_team (reference team_id)
 - serving_team (refernce team_id)
 - primary_player (reference player_id), matched to that match's box score by team + name
 - assisting_players (reference player_id), can be more than one (block by A, B)
 - play_text
 - home_score
 - away_score

RAWRESPONSE: one row per API response fetched, placeholder until #13 (raw response cache)
 - endpoint
 - match_id: nullable, not every endpoint is per match
 - fetched_at
 - ingest_run_id: foreign key referencing INGESTRUN
 - body: raw json

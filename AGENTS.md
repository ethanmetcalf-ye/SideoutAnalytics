# AGENTS.md

Instructions for coding agents working in this repo. Keep this file short and
update it in the same PR as any workflow change.

Sideout Analytics is a two-month prototype for NCAA women's volleyball:
data pipeline, ratings model, public site, bracket simulator, and an AI
assistant. Scope and milestones are in `docs/engagement.md`.

## Setup, test, lint

Python 3.13, managed with [uv](https://docs.astral.sh/uv/).

```sh
uv sync                        # install deps into .venv
cp .env.example .env           # then fill in values
docker compose up -d --wait    # local Postgres 17 (needs DB_* in .env)
```

Before opening a PR, all three must pass:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Add dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`). Never edit
`uv.lock` by hand.

## Repo layout

| Path | What goes there |
| --- | --- |
| `src/sideout_analytics/` | The application package. All code lives here. |
| `src/sideout_analytics/settings.py` | The only place config is read. Use `get_settings()`; don't read `os.environ` elsewhere. New settings get a field here and an entry in `.env.example`. |
| `src/sideout_analytics/observability.py` | `configure_observability(service)`: starts Sentry for `"api"` or `"pipeline"` (separate Sentry projects, DSNs in `SENTRY_API_DSN` / `SENTRY_PIPELINE_DSN`). Called at import in `api/main.py` and `pipeline/cli.py`, before the app is built. An unset DSN leaves Sentry off and returns `False`; an unknown service or malformed DSN raises `ValueError`. `ENVIRONMENT` names the Sentry environment and picks the trace sample rate (1.0, or 0.1 in production). |
| `src/sideout_analytics/pipeline/client.py` | HTTP client for the NCAA API: shared rate limit, retries, raw response bytes. Build it with `Client.from_settings()`; all API requests go through it. |
| `src/sideout_analytics/pipeline/cache.py` | Raw response cache: one file per endpoint under `data_directory/raw`, atomic writes. Fetch through `RawStore.get(client, endpoint)` so cache hits skip the network; when to pass `refresh=True` is in ADR-004. |
| `src/sideout_analytics/pipeline/fetch.py` | Fetcher: which endpoints to request and when to refresh them (ADR-004), the season index from schedules and scoreboards, and the game fetcher: details, box score and play-by-play for every final game with bounded concurrency, returned as `FetchedFile`s (path and hash, no content). Takes an open `Client` and a `RawStore`; failed requests come back as `FailedRequest`s, so callers must check them. |
| `src/sideout_analytics/pipeline/parse.py` | Parsers: cached file bytes in, frozen pydantic records out, keyed by source IDs. No I/O and no database access. A bad file raises `ParseError`; the decisions on cleaning and blanks are in the module docstring. |
| `src/sideout_analytics/pipeline/ingest.py` | One pipeline run: takes the run lock (one run at a time), opens an `ingest_run` row, fetches, then parses and loads (serially) the games whose files are new or changed. `raw_response` rows are written only after a game loads, so they record what's in the database; `reload_all` ignores them. Failing games go in `load_failure`, and only a game's first failure fails the run. `run()` takes everything it uses as arguments. Decisions are in the module docstring. |
| `src/sideout_analytics/pipeline/cli.py` | `pipeline poll` (current season, safe on any schedule), `pipeline backfill --season <year>`, both with `--reload-all`, and `pipeline snapshot`. Builds the connection, client and store from settings and passes them to the job (`ingest.run` or `snapshot.run_snapshot`). Exits 1 when a run fails and 75 when another run holds the lock; SIGTERM closes the run like Ctrl-C. |
| `src/sideout_analytics/pipeline/snapshot.py` | `pipeline snapshot`: refreshes the AVCA poll, saves each new week as a dated raw file (ADR-005), and loads every saved week not yet in `team_rank`, matching schools through `team_alias`. Runs through `ingest.run_job`, which every job must use so they share the run lock. |
| `src/sideout_analytics/db/loader.py` | Loader: one game's parsed records into Postgres in one transaction, safe to run twice. Shared rows are upserted on their source keys, the game's own rows are replaced. Open a connection with `connect(database_url)` (pass `timeout_seconds` to bound connecting and statements); a game that doesn't fit the schema raises `LoadError`. |
| `src/sideout_analytics/db/validate.py` | `pipeline validate [--season YEAR]`: checks the loaded season against the raw API files and the NCAA's published stats (W2-06). Each check returns the rows that break its rule. Rows listed in `db/validation_exceptions.toml` are known, and it exits 1 only on new rows. Results and explanations are in `docs/validation.md`; a new exception gets its reason in both. |
| `src/sideout_analytics/api/main.py` | The FastAPI app (`app`, set as the entrypoint in `pyproject.toml`). Run it locally with `uv run fastapi dev`. `/health/liveness` never touches the database; `/health/readiness` runs `SELECT 1` once with a 2 s timeout (no retries; callers retry) and returns 503 with a fixed message (the error goes to the log, not the response). Settings load in the app's lifespan, so missing config stops the app at startup instead of failing the first request. |
| `Dockerfile`, `.dockerignore` | The production image (multi-stage, non-root, includes `alembic.ini` and `migrations/`). Built by the `migrate` service in `deploy/compose.yaml`; `api` and `scheduler` reuse it as `sideout-app` and never pull it. Installs supercronic (pinned version, per-arch SHA-1 checked, picked by `TARGETARCH`), copies `deploy/crontab` and creates `/data` owned by `app`. |
| `deploy/` | Production compose (`postgres`, one-shot `migrate`, `api`, `scheduler`, `caddy`), Caddyfile and `crontab`. `scheduler` runs supercronic on `deploy/crontab` (UTC; baked into the image, so edits need a rebuild; supercronic exits on an invalid line; check with `supercronic -test`): hourly `pipeline poll` and daily `pipeline snapshot`, each pinging its Healthchecks.io URL (`HOURLYPOLLURL`, `SNAPSHOTPINGURL`) with the exit code, and no ping on exit 75. The raw cache is in the `rawpipeline_data` volume at `/data`; the server is a git clone at `/opt/sideout/app` and runs these from `/opt/sideout/app/deploy`. `deploy/.env.example` lists every setting the server's `deploy/.env` needs. Server config, not app code, so it stays out of `src/`. `compose.yaml` sets `name: sideout` so volume names (and the Caddy certificates in `sideout_caddy_data`) don't depend on the directory it runs from. |
| `tests/` | pytest tests, one `test_<module>.py` per module. |
| `docs/adr/` | Architecture decisions, `NNNN-short-title.md`. |
| `docs/schema.md` | Database design. The source of truth for tables, columns and constraints; migrations implement it. |
| `docs/` | Project docs, including the source API reference (`source-data-api-endpoints.md`). |
| `compose.yaml` | Local services (Postgres). |
| `.github/` | PR and issue templates. |

## Code conventions

- Ruff config in `pyproject.toml` is the style guide: 120-char lines, type
  annotations required outside `tests/`, `pathlib` over `os.path`,
  timezone-aware datetimes. Ruff skips `migrations/versions/`, so migrations
  that have already run keep their original formatting.
- Tests must not depend on the developer's `.env` or shell. Use the
  `isolated_env` fixture in `tests/conftest.py`: it clears settings env vars,
  `chdir`s to `tmp_path`, and calls `get_settings.cache_clear()`.
- Tests that need Postgres take the `database` fixture. It uses
  `TEST_DATABASE_URL` (the local `DATABASE_URL` works), builds a throwaway
  schema from `tables.py` and drops it afterward, and skips the test when the
  variable isn't set.
- Tests never touch the network or sleep. Pass `httpx.MockTransport` to the
  API client and record sleeps instead of waiting.
- Record significant design decisions as a new ADR.
- Schema changes update `docs/schema.md` in the same PR. A change that
  contradicts ADR-002 needs a new ADR.
- Store every count the source provides; calculate rates and totals at query
  time. Raw API responses go to the on-disk cache, never into the database.
- Tables in `src/sideout_analytics/db/tables.py` are ORM classes used only to
  declare the schema. Queries are handwritten SQL; don't add `relationship()`
  or ORM queries.

## Workflow

- Work comes from GitHub issues named `Wn-NN: Title` (or `K-NN` for kickoff
  docs). One issue per branch, one branch per PR.
- Cut each branch from an up-to-date `main`:
  `git switch main && git pull && git switch -c w1-03-local-postgres`.
  Branch names are the lowercase issue ID plus a short slug.
- PR title matches the issue: `W1-03: Local Postgres with docker compose`.
- Fill in `.github/pull_request_template.md`: a Summary, `Closes #<issue>`,
  and a Validation list of the commands you ran and their results.
- Commits and PRs are authored by the repo owner. Don't add AI co-author
  trailers or "generated by" footers.
- Never force push a branch that has an open PR. Push follow-up commits; PRs
  are squash-merged.
- Open the PR and stop. Ethan reviews and merges; never merge, approve or
  close your own PR.

## Do not touch

- `.env` or any real secrets. Only `.env.example`, with placeholder values.
- `data/`. It holds local and raw data and is gitignored.
- Migrations that have already run. Add a new migration instead.
- Accepted ADRs. Write a new ADR that supersedes the old one.
- `docs/engagement.md`. The statement of work changes only by agreement.

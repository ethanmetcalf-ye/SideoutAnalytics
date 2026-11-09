# SideoutAnalytics 🏐
 
A stats site for NCAA Division I women's volleyball — scores, team ratings, match predictions, a bracket simulator, and an AI assistant you can ask questions in plain English.
 
It's a side project, but I'm running it like a client engagement: a fictional client, a [statement of work](docs/engagement.md) with measurable milestones, and [ADRs](docs/adr) for the big decisions.
 
## 🗺 The plan
 
- **Data pipeline** — polls the NCAA API at least hourly and flags when data is stale
- **Ratings model** — has to beat a simple win-% baseline on a 2025 holdout, or the report says it didn't
- **Public site + predictions** — no login, predictions posted before every match
- **Tournament** — bracket simulated within minutes of the bracket coming out
- **AI assistant** — answers from curated data views, shows the query it ran, and says so when it can't answer
## 📍 Where it's at
 
Early. Working on the data pipeline.
 
- **Done:** API client with rate limiting and retries, raw response cache, season index, game fetcher, parsers, and a Postgres loader that's safe to rerun
- **Next:** CLI, then hosting, scheduling, and monitoring
Progress is tracked in [issues](https://github.com/ethanmetcalf-ye/SideoutAnalytics/issues).
 
## 🧠 A few design choices
 
- **The box score is the source of truth.** Play-by-play stats only count when they add back up to it.
- **Store every raw count, calculate rates at query time**, so totals can't drift from the numbers they came from.
- **Raw API responses stay on disk** (~888 MB a season), so a parser fix means re-parsing, not re-fetching.
More in [docs/adr](docs/adr) and [docs/schema.md](docs/schema.md).
 
## 🧰 Stack
 
Python 3.13 (uv) · Postgres 17 · SQLAlchemy + Alembic · httpx · pydantic · pytest + ruff
 
## 🏃 Running it locally
 
```bash
uv sync
cp .env.example .env           # fill in the DB_* values
docker compose up -d --wait
uv run alembic upgrade head
uv run pytest
```
 
## 📊 Data
 
From the unofficial NCAA API. Not affiliated with the NCAA.
 
---
 
Questions or ideas? Open an issue. Volleyball and stats nerds welcome.

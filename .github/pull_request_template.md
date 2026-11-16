## Summary
- Add FastAPI health endpoints for liveness and readiness checks.
- Configure FastAPI as an application entrypoint and add the required dependency.
- Readiness verifies the configured Postgres connection and returns a 503 when the database is unavailable.

Closes #23

## Validation
- `uv run ruff check .` — passed
- `uv run ruff format --check .` — passed
- `uv run pytest` — passed (278 passed, 65 skipped)

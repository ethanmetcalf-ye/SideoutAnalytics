FROM python:3.13-slim-trixie AS pythonimage
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY README.md ./
RUN uv sync --locked --no-dev --no-editable



FROM python:3.13-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --system --no-create-home app
COPY --from=pythonimage /app/.venv /app/.venv
USER app
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
# "*" is safe only because the api publishes no ports: Caddy is the only client that can reach it.
CMD ["/app/.venv/bin/uvicorn","sideout_analytics.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

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
 && apt-get install -y --no-install-recommends libpq5 curl\
 && rm -rf /var/lib/apt/lists/*
 # Latest releases available at https://github.com/aptible/supercronic/releases
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.51
RUN case "$TARGETARCH" in \
        amd64) SHA1=1692066e350a88ff4d7d6474fe3c7a6c41ec5e89 ;; \
        arm64) SHA1=9d4c2000493215a1898502b2fc2dada3e8ea8c7a ;; \
        *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" \
 && echo "${SHA1}  /usr/local/bin/supercronic" | sha1sum -c - \
 && chmod +x /usr/local/bin/supercronic
RUN useradd --system --no-create-home app \
    && mkdir /data && chown app:app /data
COPY --from=pythonimage /app/.venv /app/.venv
USER app
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
COPY deploy/crontab ./deploy/crontab
# "*" is safe only because the api publishes no ports: Caddy is the only client that can reach it.
CMD ["/app/.venv/bin/uvicorn","sideout_analytics.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

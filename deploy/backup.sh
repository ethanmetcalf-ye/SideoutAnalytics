#!/usr/bin/env bash
# Nightly backup: a Postgres dump and the raw pipeline cache, both to S3.
# Runs from root's crontab on the host (not deploy/crontab, which runs inside
# the scheduler container where docker and aws don't exist), so no sudo here.
set -euo pipefail

# docker compose finds compose.yaml and the project name from the working directory
cd "$(dirname "$0")"

# Full path, because cron's PATH is short
AWS=/usr/local/bin/aws
# One region for every aws call below
export AWS_DEFAULT_REGION=us-east-2

# Read only the values this script needs from .env, instead of sourcing it
env_value() {
    grep -m1 "^$1=" .env | cut -d= -f2-
}
POSTGRES_USER=$(env_value POSTGRES_USER)
POSTGRES_DB=$(env_value POSTGRES_DB)
AWSBACKUPS_URL=$(env_value AWSBACKUPS_URL)
BACKUPPINGURL=$(env_value BACKUPPINGURL || true) # optional

dump=""

# Runs however the script ends: removes the temp dump, then tells Healthchecks.io
# how it went (0 on success, the failing exit code otherwise). No ping without BACKUPPINGURL.
finish() {
    local rc=$?
    [ -z "$dump" ] || rm -f "$dump"
    [ -z "$BACKUPPINGURL" ] || curl -fsS -m 10 --retry 3 "$BACKUPPINGURL/$rc" > /dev/null || true
}
trap finish EXIT

# UTC, and the name says so
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)

# Dump to a file, check it, and only then upload.
dump=$(mktemp /var/tmp/sideout-backup.XXXXXX)
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F c -Z 9 > "$dump"
docker compose exec -T postgres pg_restore --list < "$dump" > /dev/null
"$AWS" s3 cp "$dump" "${AWSBACKUPS_URL}/backups/${POSTGRES_DB}_${TIMESTAMP}.dump"

# Backup of raw data that has changed
"$AWS" s3 sync /var/lib/docker/volumes/sideout_rawpipeline_data/_data "${AWSBACKUPS_URL}/cached_data"

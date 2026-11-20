# Restoring a backup

`deploy/backup.sh` runs at 07:30 UTC from root's crontab on the server. It uploads a
checked Postgres dump to `$AWSBACKUPS_URL/backups/` (`sideout_<UTC timestamp>.dump`) and
syncs the raw cache to `$AWSBACKUPS_URL/cached_data/`. This page is how to get the data
back, and how to prove a backup is good without touching the live database.

Run everything on the server from `/opt/sideout/app/deploy`. `<bucket>` is the value of
`AWSBACKUPS_URL` in `deploy/.env`.

## Test a restore (scratch database, live data untouched)

Do this after the first backup and about once a month.

```sh
cd /opt/sideout/app/deploy
aws s3 ls <bucket>/backups/ | tail -3
aws s3 cp <bucket>/backups/<file>.dump /var/tmp/restore-test.dump

docker compose exec -T postgres createdb -U <POSTGRES_USER> restore_test
docker compose exec -T postgres pg_restore -U <POSTGRES_USER> -d restore_test --no-owner < /var/tmp/restore-test.dump
```

Compare row counts with the live database. Counts can differ slightly for tables the
hourly poll writes to, because the dump is a few hours old, so check that the restore is
close and that no table is empty:

```sh
for db in <POSTGRES_DB> restore_test; do
  echo "== $db"
  docker compose exec -T postgres psql -U <POSTGRES_USER> -d "$db" -Atc "
    SELECT 'season', count(*) FROM season UNION ALL
    SELECT 'team', count(*) FROM team UNION ALL
    SELECT 'match', count(*) FROM match UNION ALL
    SELECT 'play', count(*) FROM play UNION ALL
    SELECT 'team_rank', count(*) FROM team_rank UNION ALL
    SELECT 'ingest_run', count(*) FROM ingest_run"
done
```

Clean up:

```sh
docker compose exec -T postgres dropdb -U <POSTGRES_USER> restore_test
rm /var/tmp/restore-test.dump
```

Record the date, the backup file and the counts in the table below each time you do this.

| Date | Backup file | match | play | Result |
| --- | --- | --- | --- | --- |
| _not tested yet_ | | | | |

## Restore for real

Only to replace a lost or damaged database. This overwrites what is in it.

1. Stop the writers: `docker compose stop scheduler api`.
2. Restore the dump into the database: `pg_restore --clean --if-exists --no-owner -d <POSTGRES_DB>`,
   with the file piped in as above.
3. Start them again: `docker compose up -d`. `migrate` runs on start and brings the schema
   to head.
4. The raw cache comes back with
   `aws s3 sync <bucket>/cached_data /var/lib/docker/volumes/sideout_rawpipeline_data/_data`.

## IAM and lifecycle

- The instance needs `s3:PutObject` on `backups/*` and `cached_data/*`, and `s3:ListBucket` on
  the bucket limited to the `cached_data/` prefix, because `aws s3 sync` lists it.
- Set the lifecycle rule (expire old dumps) on the `backups/` prefix, the one the dumps are
  written to.

# Restoring a backup

`deploy/backup.sh` runs at 07:30 UTC from root's crontab on the server. It uploads a
checked Postgres dump to `<bucket>/backups/` (`sideout_<UTC timestamp>.dump`) and syncs
the raw cache to `<bucket>/cached_data/`. `<bucket>` is `AWSBACKUPS_URL` in `deploy/.env`.

**The server's instance role is upload-only, on purpose.** It can't list or read the
bucket, so a compromised server can't take the backup history. Everything that reads
backups runs from your Mac with your own AWS credentials. The server gets a
single-file, 10-minute link and never lasting read access.

## Test a restore (scratch database, live data untouched)

Do this after the first backup and about once a month.

On your Mac:

```sh
aws s3 ls <bucket>/backups/ | tail -3
aws s3 presign <bucket>/backups/<file>.dump --expires-in 600
```

On the server, in `/opt/sideout/app/deploy`:

```sh
curl -fsSL "<presigned url>" -o /var/tmp/restore-test.dump
docker compose exec -T postgres createdb -U <POSTGRES_USER> restore_test
docker compose exec -T postgres pg_restore -U <POSTGRES_USER> -d restore_test --no-owner < /var/tmp/restore-test.dump
```

Compare row counts with the live database. Tables the hourly poll writes to differ
slightly (the dump is a few hours old); none should be empty.

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

Record each test here.

| Date | Backup file | match | play | Result |
| --- | --- | --- | --- | --- |
| _not tested yet_ | | | | |

## Restore for real

Only to replace a lost or damaged database. This overwrites what is in it.

1. Get the dump onto the server as above (presign on your Mac, `curl` on the server).
2. Stop the writers: `docker compose stop scheduler api`.
3. Restore: `docker compose exec -T postgres pg_restore -U <POSTGRES_USER> -d <POSTGRES_DB> --clean --if-exists --no-owner < /var/tmp/restore.dump`
4. Raw cache: on your Mac, `aws s3 sync <bucket>/cached_data ./cached_data`, then copy it
   to `/var/lib/docker/volumes/sideout_rawpipeline_data/_data` on the server (`rsync` or `scp`).
5. Fix ownership, or the scheduler (uid 999, user `app`) can't write to the files:
   `sudo chown -R 999:999 /var/lib/docker/volumes/sideout_rawpipeline_data/_data`
6. Start again: `docker compose up -d`. `migrate` brings the schema to head.

## IAM and lifecycle

- Instance role: `s3:PutObject` on `backups/*` and `cached_data/*`, and `s3:ListBucket`
  limited to the `cached_data/` prefix (`aws s3 sync` lists it). No `GetObject`.
- Lifecycle rules on `backups/`: expire old dumps, and permanently delete noncurrent
  versions after 30 days, or versioning keeps every replaced object forever.

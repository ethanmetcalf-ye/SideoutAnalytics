# Deploy

`.github/workflows/deploy-app.yml` deploys every commit on `main` that passes CI:

1. `publish` builds the arm64 image and pushes it to `ghcr.io/ethanmetcalf-ye/sideoutanalytics` as `sha-<commit>` and `latest`.
2. `deploy` assumes `AWS_DEPLOY_ROLE_ARN` through GitHub OIDC and sends an SSM Run Command to `EC2_INSTANCE_ID`. As the user that owns the clone, the server checks out the commit in `/opt/sideout/app` and runs `docker compose pull migrate api scheduler` and `docker compose up -d --wait api scheduler caddy` in `deploy/` with `APP_TAG=sha-<commit>`. It fails unless `api` and `scheduler` end up on that image.

One run at a time (publish and deploy together). A run that is waiting is replaced by a newer one, not queued behind it, so only the newest commit gets deployed. Run it by hand from the Actions tab (Run workflow, branch `main`).

## One-time setup

In AWS:

1. IAM identity provider: `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
2. A deploy role of its own (not the instance role, or the server could send commands to itself) trusting only that provider, with `token.actions.githubusercontent.com:sub` = `repo:ethanmetcalf-ye@68034896/SideoutAnalytics@1425030278:ref:refs/heads/main` (StringEquals). The repo uses GitHub's immutable subject format, which adds the owner and repo IDs, so a deleted and recreated repo with the same name can't assume the role. Check the prefix with `gh api repos/ethanmetcalf-ye/SideoutAnalytics/actions/oidc/customization/sub`. Permissions: `ssm:SendCommand` on the instance ARN and on `arn:aws:ssm:<region>::document/AWS-RunShellScript`, and `ssm:GetCommandInvocation`.
3. The instance role has `AmazonSSMManagedInstanceCore`. Check that the instance appears under Systems Manager, Fleet Manager.

In GitHub, repository variables: `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `EC2_INSTANCE_ID`.

On the server, the deploy runs as the owner of `/opt/sideout/app` (`ethan` today), who must be in the `docker` group:

```sh
id "$(stat -c '%U' /opt/sideout/app)"
```

## Image visibility

The `sideoutanalytics` package is public, so the server pulls without logging in (the first deploy did). If it is ever made private, run `docker login ghcr.io` on the server as the owner of the clone with a `read:packages` token, or deploys fail on the pull. The old containers keep running, because the pull comes before `up`.

Check what is running:

```sh
cd /opt/sideout/app/deploy && docker compose images
```

## Rollback

Every deployed commit's image stays in GHCR as `sha-<commit>`, so rolling back rebuilds nothing. Find the last good SHA in the Actions history (the deploy run's `deploy $SHA` comment) or in the package's tag list.

### 1. Roll back prod (minutes)

First check the migration caveat below. Then, on the server as the owner of the clone, check out the good commit so `deploy/` matches its image, and start that image:

```sh
cd /opt/sideout/app && git fetch -q origin && git checkout -q --detach <good-sha>
cd deploy && export APP_TAG=sha-<good-sha>
docker compose up -d --wait api scheduler caddy
docker compose images
```

Keep `APP_TAG` exported for any other compose command in that shell (see Manual compose commands).

### 2. Make `main` match

Revert the bad commit on a branch, then open a PR and merge it:

```sh
git switch main && git pull && git switch -c revert-<bad-sha>
git revert <bad-sha>
```

Without the revert, the next merge deploys the bad code again. The rollback fixes prod; the revert fixes `main`. Merging the revert also deploys, which puts prod back on an image built from `main`.

### Migrations

Rolling back the image doesn't roll back the schema. If the bad release added a migration:

- **Additive** (a new table, a new nullable column): the old code ignores it. Roll back as above and leave the schema.
- **Anything else** (a dropped or renamed column, a new `NOT NULL`): downgrade first, using the bad image, because the old image doesn't have the new migration and can't undo it. Downgrading can drop data, so run `backup.sh` before it.

```sh
cd /opt/sideout/app/deploy
export APP_TAG=sha-<bad-sha>                       # the image that is running now
target=$(APP_TAG=sha-<good-sha> docker compose run --rm migrate /app/.venv/bin/alembic heads | cut -d' ' -f1)
docker compose stop api scheduler
docker compose run --rm migrate /app/.venv/bin/alembic downgrade "$target"
```

Then do step 1. The good image's `migrate` sees the schema already at its head and does nothing.


## Manual compose commands

Without `APP_TAG`, compose uses `latest`. After a failed deploy, `latest` can be newer than what is running, so a manual `docker compose up` would upgrade. Pin the running tag first:

```sh
export APP_TAG=$(docker inspect -f '{{.Config.Image}}' sideout-api-1 | cut -d: -f2)
```

`docker compose exec` (as in `backup.sh` and `restore.md`) doesn't start containers, so it is safe without `APP_TAG`.

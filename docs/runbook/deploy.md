# Deploy

`.github/workflows/deploy-app.yml` deploys every commit on `main` that passes CI:

1. `publish` builds the arm64 image and pushes it to `ghcr.io/ethanmetcalf-ye/sideoutanalytics` as `sha-<commit>` and `latest`.
2. `deploy` assumes `AWS_DEPLOY_ROLE_ARN` through GitHub OIDC and sends an SSM Run Command to `EC2_INSTANCE_ID`. As the user that owns the clone, the server checks out the commit in `/opt/sideout/app` and runs `docker compose pull` and `docker compose up -d --wait` in `deploy/` with `APP_TAG=sha-<commit>`. It fails unless `api` and `scheduler` end up on that image.

One deploy runs at a time. Run it by hand from the Actions tab (Run workflow, branch `main`).

## One-time setup

In AWS:

1. IAM identity provider: `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
2. Deploy role trusting that provider, with `token.actions.githubusercontent.com:sub` = `repo:ethanmetcalf-ye/SideoutAnalytics:ref:refs/heads/main`. Permissions: `ssm:SendCommand` on the instance ARN and on `arn:aws:ssm:<region>::document/AWS-RunShellScript`, and `ssm:GetCommandInvocation`.
3. The instance role has `AmazonSSMManagedInstanceCore`. Check that the instance appears under Systems Manager, Fleet Manager.

In GitHub, repository variables: `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `EC2_INSTANCE_ID`.

On the server, the deploy runs as the owner of `/opt/sideout/app` (`ethan` today), who must be in the `docker` group:

```sh
id "$(stat -c '%U' /opt/sideout/app)"
```

## First deploy

`publish` creates the package as private, so the first `deploy` fails on the pull. The old containers keep running (the pull fails before `up`). Make the package public (GitHub profile, Packages, `sideoutanalytics`, Package settings), then Re-run failed jobs on the run. Check on the server:

```sh
cd /opt/sideout/app/deploy && docker compose images
```

## Manual compose commands

Without `APP_TAG`, compose uses `latest`. After a failed deploy, `latest` can be newer than what is running, so a manual `docker compose up` would upgrade. Pin the running tag first:

```sh
export APP_TAG=$(docker inspect -f '{{.Config.Image}}' sideout-api-1 | cut -d: -f2)
```

`docker compose exec` (as in `backup.sh` and `restore.md`) doesn't start containers, so it is safe without `APP_TAG`.

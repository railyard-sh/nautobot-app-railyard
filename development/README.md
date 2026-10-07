# Local Nautobot for developing the app

A Nautobot 2.4 with this app installed (editable), a Celery worker, Postgres and Redis, for running the jobs by
hand and the test suite in containers.

## Prerequisites

- Docker running.
- For real runs against Railyard: a **Railyard personal access token** (`ry_…`) for a dedicated user (viewer to
  export; editor in the organisations you import into), with a short expiry. Never commit it.

## Bring it up

```bash
export RAILYARD_TOKEN=ry_your_token_here   # or put these in development/.env (git-ignored)
export RAILYARD_URL=https://railyard.sh    # or your own Railyard
export RAILYARD_ORG=my-org                 # the org the jobs use (else the token user's personal org)

docker compose -f development/docker-compose.yml up --build -d
docker compose -f development/docker-compose.yml logs -f nautobot   # first boot runs migrations (a few minutes)
docker compose -f development/docker-compose.yml exec nautobot nautobot-server createsuperuser
```

Nautobot listens on **127.0.0.1:8080** only. Sign in, open **Jobs → Jobs**, enable *Export from Railyard* and
*Import into Railyard* (Nautobot installs jobs disabled), and run them — keep **Dry run** ticked for the first pass
and read the job log.

## Tests in the container

```bash
docker compose -f development/docker-compose.yml run --rm --entrypoint "" nautobot \
  nautobot-server test nautobot_railyard --noinput --parallel 1
```

The checkout's `nautobot_railyard/` is mounted over the installed app, so edits apply after a restart
(`docker compose … restart nautobot worker`). `RAILYARD_SYNC_REF` (a railyard-sync tag or branch) and
`NAUTOBOT_VERSION` (`2.3`, `2.4`) choose what the image is built with.

## Tear down

```bash
docker compose -f development/docker-compose.yml down       # keep the data
docker compose -f development/docker-compose.yml down -v    # and drop the database
```

Nothing here is needed to *use* the app in a real Nautobot — it is only the local harness. Without Docker, run
the suite with `nautobot-server --config=development/nautobot_config.py test nautobot_railyard` and the
`NAUTOBOT_DB_*` / `NAUTOBOT_REDIS_*` variables pointing at a Postgres and a Redis.

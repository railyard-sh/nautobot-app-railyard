# nautobot-app-railyard — repo guide for Claude

A **Nautobot 2.x app** (package `nautobot_railyard`) connecting Nautobot and **Railyard** (https://railyard.sh,
the data-centre design tool; repo `railyard-sh/railyard`) in both directions, as two Jobs:

- **Export from Railyard** (`ExportFromRailyard`): Railyard's `nautobot-sync` deliverable → Nautobot.
- **Import into Railyard** (`ImportIntoRailyard`): Nautobot locations → a Railyard estate (create or refresh).

The sibling NetBox plugin is `railyard-sh/netbox-plugin-railyard`; both sit on **railyard-sync**
(`railyard-sh/railyard-sync`), which holds every DCIM-agnostic and Nautobot-REST decision. Read its `CLAUDE.md`
(snapshot contract, identity scheme, re-import policy, the Nautobot target's ownership rules) before changing
behaviour here.

## The one design decision: in-process REST, not a second ORM adapter

railyard-sync already has a Nautobot loader (`railyard_sync.dcim.nautobot`) and a Nautobot REST target
(`railyard_sync.export.nautobot_rest`) with the hardened ownership semantics, tested against a fake Nautobot.
This app does **not** reimplement them over the ORM. `inprocess.InProcessSession` is a `requests`-like session
that dispatches each request to **Nautobot's own API view** for the path (`django.urls.resolve` + DRF's
`APIRequestFactory`/`force_authenticate`), as the job's user. That gives one implementation of the rules, Nautobot's
own validation/filters/serializers, per-object permissions (`restrict()` in the views), the job's DB transaction
and change logging (Nautobot runs jobs inside `web_request_context` for the user). If you need a behaviour change,
make it in railyard-sync and test it there first.

Consequences to remember:

- Anything the REST API refuses, the jobs see as the same HTTP status and message railyard-sync handles.
- `inprocess.BASE_URL` / `TOKEN` are labels; requests never leave the process. The import records
  `app_settings.nautobot_url()` as the snapshot's source URL so the CLI and the app recognise the same source.
- Responses' links use a host from `ALLOWED_HOSTS` (`_host()`), or Django refuses to build them.

## Layout

| Path | What |
|---|---|
| `nautobot_railyard/__init__.py` | `NautobotAppConfig` (guarded import), `default_settings`, `jobs = "jobs.jobs"`, `min_version` 2.3 (Python 3.12) |
| `nautobot_railyard/app_settings.py` | Settings readers: Railyard URL, token (Secret > file > env var > literal), allowlist, `nautobot_url`. **Not `config.py`**: `nautobot_railyard.config` is the AppConfig class Nautobot looks up |
| `nautobot_railyard/policy.py` | **Nautobot-free**: `SyncTargets` (org/project allowlist, as the NetBox plugin's), `read_token`, the permissions each job needs |
| `nautobot_railyard/inprocess.py` | `InProcessSession` (see above) |
| `nautobot_railyard/jobs.py` | The two jobs, `register_jobs`; `make_railyard_client` is the seam tests replace |
| `nautobot_railyard/tests/` | Run by `nautobot-server test nautobot_railyard`; `base.py` has the fake Railyard and `RailyardJobTestCase` |
| `development/` | `nautobot_config.py` (env-driven, used by CI too), Dockerfile + docker-compose (Nautobot, worker, Postgres, Redis) |

## Job semantics (keep these promises; the README states them to users)

- Both jobs re-check the user (active, permissions from `policy`) and the org/project allowlist **when they run**.
  A project allowlist means an import may refresh but not create.
- Export: dry run = `sync_to_nautobot(dry_run=True)` inside an always-rolled-back transaction; real run = one
  transaction, rolled back (job failed) if `result.errors` is not empty. Conflicts are skipped, not errors.
- Import: `railyard_sync.importer.flow` — `plan_import`, `preflight`, then conflicts stop the save, an up-to-date
  refresh is not saved, `flow.save` PUTs with `If-Match`. A dry run saves nothing (it still runs the check).
- Railyard's 402s are worded by `railyard_sync.cli.deliverable_plan_message` / `plan_limit_message`.
- railyard-sync's log lines are forwarded to the job log (`_forward_logs`, INFO+) through its `RedactingFilter`;
  never log a token or a document.

## Testing

`RailyardJobTestCase` is a `TransactionTestCase` with `databases = ("default", "job_logs")` (Nautobot writes job
logs through the `job_logs` alias; a plain `TestCase` can't read them, and transactions wouldn't be real). A
`TransactionTestCase` truncates the database: create what migrations would have (e.g. the `Global` namespace)
yourself. Import from `nautobot.core.testing`, not `nautobot.apps.testing` (that one needs selenium).

```
nautobot-server --config=development/nautobot_config.py test nautobot_railyard --noinput --parallel 1
python -m unittest nautobot_railyard.tests.test_policy      # no Nautobot needed
ruff check . && ruff format --check .
```

CI (`.github/workflows/ci.yml`): lint + policy tests, then the suite against Nautobot 2.3 and 2.4 with Postgres and
Redis services. railyard-sync comes from its release tag (`pyproject.toml`); bump it there and in
`development/Dockerfile` (`RAILYARD_SYNC_REF`) together.

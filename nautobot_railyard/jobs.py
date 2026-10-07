"""The app's two Jobs (Jobs → Railyard in Nautobot's UI), mirroring the NetBox plugin's sync job and
``railyard-sync``'s commands.

- **Export from Railyard** fetches the project's Nautobot sync document from Railyard (the ``nautobot-sync``
  deliverable, paid on hosted Railyard) and reconciles it into Nautobot with railyard-sync's
  ``sync_to_nautobot`` — the ownership tag, shared objects used as they are, conflicts skipped, guarded deletes.
- **Import into Railyard** reads Nautobot locations with railyard-sync's Nautobot loader, builds (or, for an
  existing estate, merges) the Railyard project with railyard-sync's import flow, checks it with Railyard and
  saves it.

Both talk to Nautobot through :class:`~.inprocess.InProcessSession` (Nautobot's API views, in-process, as the
user who ran the job) and re-check that user's permissions and the configured org/project allowlist when they
run, whatever queued them. A dry run never writes: an export's runs in a transaction that is always rolled back,
an import's saves nothing to Railyard. A real export is all or nothing: one transaction, rolled back if Nautobot
refused any write. railyard-sync's own progress lines are copied into the job log; tokens never are.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from django.db import transaction
from nautobot.apps.jobs import BooleanVar, ChoiceVar, DryRunVar, Job, MultiObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Location
from railyard_sync import cli as railyard_cli
from railyard_sync.client import RailyardClient
from railyard_sync.dcim.errors import DCIMError
from railyard_sync.dcim.nautobot import NautobotLoader
from railyard_sync.dcim_http import APIError
from railyard_sync.errors import (
    RailyardAPIError,
    RailyardPlanError,
    RailyardPlanLimitError,
    RailyardTokenRejectedError,
)
from railyard_sync.export.nautobot_rest import NautobotClient
from railyard_sync.export.policy import insecure_url
from railyard_sync.export.run import SyncRefused, sync_to_nautobot
from railyard_sync.export.sync_document import SyncDocumentError
from railyard_sync.importer.run import (
    ImportRefused,
    ImportRun,
    check_failure,
    conflicts_message,
    doc_count,
    import_summary,
)
from railyard_sync.log import RedactingFilter
from railyard_sync.plans import deliverable_plan_message, plan_limit_message

from . import app_settings as config, policy
from .inprocess import BASE_URL, TOKEN, InProcessSession

name = "Railyard"  # the job grouping in Nautobot's UI

#: What to do when Railyard's check refuses an import (the CLI's advice names its --no-validate flag).
FIX = "Fix them in Nautobot (or in Railyard, for objects designed there) and import again."
#: The most lines of one list (changes, conflicts…) a job logs before saying how many more there are.
LOG_CAP = 250


class RailyardJobFailed(Exception):
    """The job stopped; the message says why (it is also in the job log)."""


class _Rollback(Exception):
    """Raised to roll back a transaction."""


@contextmanager
def _rolled_back():
    """A transaction that is always rolled back (a dry run's backstop against writes)."""
    try:
        with transaction.atomic():
            yield
            raise _Rollback
    except _Rollback:
        pass


class _JobLogHandler(logging.Handler):
    """Copies railyard-sync's progress lines (INFO and up) into the job log, with secrets redacted."""

    def __init__(self, job: Job, secrets: list[str]) -> None:
        super().__init__(logging.INFO)
        self.job = job
        self.addFilter(RedactingFilter(secrets))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.logger.log(record.levelno, record.getMessage())
        except Exception:  # pragma: no cover - logging must never break a job
            pass


@contextmanager
def _forward_logs(job: Job, secrets: list[str]):
    logger = logging.getLogger("railyard_sync")
    handler = _JobLogHandler(job, secrets)
    previous = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def make_railyard_client(url: str, token: str, org: str | None) -> RailyardClient:
    """The Railyard client the jobs use (the tests replace it with one on a fake Railyard)."""
    return RailyardClient(url, token, org=org)


def _org_choices() -> list[tuple[str, str]]:
    try:
        return config.sync_targets().org_choices()
    except Exception:  # settings not loaded (documentation builds): the job re-checks when it runs
        return [("", "Personal organisation of the token's user")]


class _RailyardJob(Job):
    """What both jobs share: the user and allowlist checks, the token, the Railyard client, the job log."""

    class Meta:
        abstract = True

    def _fail(self, message: str):
        self.logger.error(message)
        raise RailyardJobFailed(message)

    def _check_user(self, perms) -> None:
        user = self.user
        if user is None or not user.is_active:
            self._fail("A Railyard job must be run by an active Nautobot user.")
        lacking = policy.missing(user.has_perm, perms)
        if lacking:
            self._fail(f"{user} lacks the Nautobot permissions this job needs: {', '.join(lacking)}")

    def _token(self) -> str:
        try:
            token = config.railyard_token()
        except ValueError as exc:
            self._fail(str(exc))
        if not token:
            self._fail(
                "No Railyard token: set railyard_token_secret, railyard_token_file or the RAILYARD_TOKEN environment "
                "variable (PLUGINS_CONFIG['nautobot_railyard'])."
            )
        return token

    def _client(self, url: str, token: str, org: str | None) -> RailyardClient:
        if insecure_url(url):
            self.logger.warning(
                f"railyard_url {url!r} is not https: the Railyard token is sent in clear text. Use https."
            )
        try:
            client = make_railyard_client(url, token, org)
            client.org_id()  # resolve the org now, so a wrong one fails before any work
        except RailyardTokenRejectedError as exc:
            self._fail(f"{exc} Ask a Nautobot administrator to rotate the token.")
        except (RailyardAPIError, ValueError) as exc:
            self._fail(str(exc))
        return client

    def _log_lines(self, heading: str, rows: list[str], *, level: str = "info") -> None:
        if not rows:
            return
        log = getattr(self.logger, level)
        log(f"{heading} ({len(rows)}):")
        for row in rows[:LOG_CAP]:
            log(f"  · {row}")
        if len(rows) > LOG_CAP:
            log(f"  · … and {len(rows) - LOG_CAP} more")


class ExportFromRailyard(_RailyardJob):
    """Export a Railyard project into Nautobot."""

    org = ChoiceVar(
        choices=_org_choices(),
        required=False,
        label="Railyard organisation",
        description="One of the organisations this Nautobot is configured to use.",
    )
    project = StringVar(label="Railyard project", description="The project's id or URL slug.")
    change_request = StringVar(
        required=False,
        label="Merge request",
        description="Export a merge request's draft (its id) instead of the project's main design.",
    )
    allow_deletes = BooleanVar(
        default=False,
        label="Allow deletes",
        description="Also delete what earlier exports created and Railyard no longer has (never anything else).",
    )
    dryrun = DryRunVar(description="Show what would change; write nothing.")

    class Meta:
        name = "Export from Railyard"
        description = (
            "Create and update the project's locations, racks, devices, ports and cables in Nautobot. Only objects "
            "carrying the project's ownership tag are ever changed or deleted."
        )
        dryrun_default = True
        has_sensitive_variables = False
        field_order = ["org", "project", "change_request", "allow_deletes", "dryrun"]

    def run(self, *, project, org=None, change_request=None, allow_deletes=False, dryrun=True):  # noqa: D102
        dry_run = bool(dryrun)
        self._check_user(policy.export_permissions(dry_run=dry_run, allow_deletes=allow_deletes))
        targets = config.sync_targets()
        try:
            org = targets.check_org(org)
            project = targets.check_project(project)
        except policy.TargetNotAllowed as exc:
            self._fail(str(exc))
        url, token = config.railyard_url(), self._token()

        with _forward_logs(self, [token]):
            client = self._client(url, token, org)
            self.logger.info(f"Fetching the Nautobot sync document for {project!r} from Railyard ({url})…")
            try:
                document = client.nautobot_sync_document(
                    project, change_request_id=(change_request or "").strip() or None
                )
            except RailyardPlanError as exc:
                self._fail(deliverable_plan_message(exc, "Nautobot"))
            except RailyardTokenRejectedError as exc:
                self._fail(f"{exc} Ask a Nautobot administrator to rotate the token.")
            except RailyardAPIError as exc:
                self._fail(str(exc))

            nautobot = NautobotClient(BASE_URL, TOKEN, session=InProcessSession(self.user))
            try:
                if dry_run:
                    with _rolled_back():
                        result = sync_to_nautobot(
                            document, client=nautobot, dry_run=True, allow_deletes=allow_deletes, railyard_url=url
                        )
                else:
                    with transaction.atomic():
                        result = sync_to_nautobot(
                            document, client=nautobot, dry_run=False, allow_deletes=allow_deletes, railyard_url=url
                        )
                        if result.errors:
                            raise _Rollback
            except _Rollback:
                self._log_lines("Nautobot refused", result.errors, level="error")
                self._fail(
                    f"Nautobot refused {len(result.errors)} write(s) (listed above), so the export was rolled back: "
                    "nothing was changed in Nautobot. Fix them and run the export again."
                )
            except (SyncRefused, SyncDocumentError, APIError) as exc:
                self._fail(f"{exc} Nothing was changed in Nautobot.")

        for line in railyard_cli.export_report(result).splitlines():
            self.logger.info(line)
        if result.conflicts:
            self.logger.warning(f"{len(result.conflicts)} conflict(s) were skipped (listed above).")
        return result.as_dict()


class ImportIntoRailyard(_RailyardJob):
    """Import Nautobot locations into a Railyard estate."""

    locations = MultiObjectVar(
        model=Location,
        required=False,
        label="Locations",
        description="The locations to import, each with everything below it (a site, typically).",
    )
    all_locations = BooleanVar(
        default=False,
        label="All locations",
        description="Every top data-centre location: the first location type down the tree that may hold racks.",
    )
    org = ChoiceVar(
        choices=_org_choices(),
        required=False,
        label="Railyard organisation",
        description="One of the organisations this Nautobot is configured to use.",
    )
    project = StringVar(
        required=False,
        label="Refresh project",
        description="The id or URL slug of the estate an earlier import created, to refresh it.",
    )
    new_project = StringVar(required=False, label="Or create project", description="A name for a new estate.")
    allow_deletes = BooleanVar(
        default=False,
        label="Allow deletes",
        description="Remove from the estate what was deleted in Nautobot (never what was designed in Railyard).",
    )
    dryrun = DryRunVar(description="Show what would change; save nothing to Railyard.")

    class Meta:
        name = "Import into Railyard"
        description = (
            "Bring Nautobot locations into Railyard as a design baseline, or refresh one an earlier import created. "
            "What was designed in Railyard is kept."
        )
        dryrun_default = True
        has_sensitive_variables = False
        field_order = ["locations", "all_locations", "org", "project", "new_project", "allow_deletes", "dryrun"]

    def run(  # noqa: D102
        self,
        *,
        locations=None,
        all_locations=False,
        org=None,
        project=None,
        new_project=None,
        allow_deletes=False,
        dryrun=True,
    ):
        dry_run = bool(dryrun)
        self._check_user(policy.import_permissions())
        chosen = [str(loc.pk) for loc in (locations or [])]
        if bool(chosen) == bool(all_locations):
            self._fail("Choose the locations to import, or tick All locations (not both).")
        project, new_project = (project or "").strip(), (new_project or "").strip()
        if bool(project) == bool(new_project):
            self._fail("Name the estate to refresh (Refresh project) or a new one (Or create project), not both.")
        targets = config.sync_targets()
        try:
            org = targets.check_org(org)
            if project:
                project = targets.check_project(project)
            else:
                targets.check_new_project()
        except policy.TargetNotAllowed as exc:
            self._fail(str(exc))
        url, token = config.railyard_url(), self._token()

        with _forward_logs(self, [token]):
            client = self._client(url, token, org)
            loader = NautobotLoader(config.nautobot_url(), TOKEN, session=InProcessSession(self.user))
            try:
                snapshot = loader.load(chosen or None)
                run = ImportRun(
                    client,
                    snapshot,
                    project=project or None,
                    name=new_project or None,
                    allow_deletes=allow_deletes,
                    requested_sites=chosen,
                    place_flag="",
                    name_taken_hint="refresh it instead, or choose another name",
                )
                run.fetch()
                built = run.build()
                run.merge()
            except (DCIMError, ImportRefused, RailyardAPIError, ValueError) as exc:
                self._fail(f"{exc} Nothing was saved to Railyard.")

            for line in import_summary(snapshot, built.project, built.report).splitlines():
                self.logger.info(line)
            if run.diff is not None:
                for line in run.diff.summary().splitlines():
                    self.logger.info(line)
            self._log_lines("Conflicts with the Railyard design", [str(c) for c in run.conflicts], level="warning")

            if dry_run:
                problems = run.check()
                if problems:
                    self.logger.warning(check_failure(problems, dry_run=True, source="nautobot", fix=FIX))
                action = f"refresh {run.name!r}" if run.refreshing else f"create {run.name!r}"
                self.logger.info(f"Dry run: would {action}. Nothing was saved to Railyard.")
                return self._result(run, run.revision, dry_run=True, problems=problems)
            if run.conflicts:
                self._fail(conflicts_message(len(run.conflicts), "nautobot"))
            if run.up_to_date():
                self.logger.info(f"{run.name!r} is already up to date with Nautobot; nothing was saved.")
                return self._result(run, run.revision, dry_run=False, problems=[])
            problems = run.check()
            if problems:
                self._fail(check_failure(problems, dry_run=False, source="nautobot", fix=FIX))
            try:
                revision = run.save()
            except RailyardPlanLimitError as exc:
                self._fail(plan_limit_message(exc, refreshing=run.refreshing, places="locations"))
            except (ImportRefused, RailyardAPIError) as exc:
                self._fail(str(exc))
        verb = "Refreshed" if run.refreshing else "Created"
        self.logger.info(f"{verb} {run.name!r} ({run.project_id}) at revision {revision} in Railyard.")
        return self._result(run, revision, dry_run=False, problems=[])

    @staticmethod
    def _result(run: ImportRun, revision, *, dry_run: bool, problems: list[str]) -> dict:
        return {
            "dry_run": dry_run,
            "project_id": run.project_id,
            "name": run.name,
            "refresh": run.refreshing,
            "revision": revision,
            "counts": doc_count(run.document),
            "diff": run.diff.to_dict() if run.diff is not None else None,
            "conflicts": len(run.conflicts),
            "problems": problems,
        }


jobs = [ExportFromRailyard, ImportIntoRailyard]
register_jobs(*jobs)

"""Shared helpers for the app's tests, which run under Nautobot's test runner (``nautobot-server test
nautobot_railyard``) against a real Nautobot database.

Railyard is never contacted: the jobs' Railyard client is given :class:`FakeRailyard`'s session, which serves the
organisations, the ``nautobot-sync`` deliverable (by default the document Railyard's own exporter writes for a
small cabled estate, ``fixtures/nautobot-sync-cabled.json``), projects with revisions, saves and checks.
"""

from __future__ import annotations

import copy
import json
import logging
import pathlib
from unittest import mock

import structlog
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from nautobot.core.testing import TransactionTestCase, run_job_for_testing
from nautobot.extras.models import Job, JobLogEntry
from nautobot.users.models import ObjectPermission
from railyard_sync.client import RailyardClient

from nautobot_railyard import jobs

# DiffSync logs every step through structlog; keep the test output readable.
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
DOCUMENT = json.loads((FIXTURES / "nautobot-sync-cabled.json").read_text())
RAILYARD = "https://railyard.example.com"
TOKEN = "ry_" + "t" * 24  # a fake Railyard personal access token
ORGS = [{"id": "org_acme", "name": "Acme", "slug": "acme"}]

APP_SETTINGS = {
    "railyard_url": RAILYARD,
    "railyard_token": TOKEN,
    "railyard_token_env": None,
    "railyard_token_file": None,
    "railyard_token_secret": None,
    "railyard_org": "acme",
    "railyard_allowed_orgs": [],
    "railyard_allowed_projects": [],
    "nautobot_url": "https://nautobot.example.com",
}


def app_settings(**changes) -> dict:
    return {"nautobot_railyard": {**APP_SETTINGS, **changes}}


class Response:
    def __init__(self, status: int, payload=None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload)
        self.headers = dict(headers or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeRailyard:
    """Just enough of Railyard's API for the jobs: orgs, one project with a revision, saves, checks, the catalogue
    search (no matches) and the nautobot-sync deliverable (``deliverable``: the document, or a Response)."""

    def __init__(self):
        self.project: dict | None = None
        self.revision = 7
        self.deliverable: dict | Response = copy.deepcopy(DOCUMENT)
        self.put_response: Response | None = None
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "json": json})
        assert (headers or {}).get("Authorization") == f"Bearer {TOKEN}"
        path = url.removeprefix(RAILYARD)
        if path == "/api/orgs":
            return Response(200, ORGS)
        if path == "/api/validate" and method == "POST":
            return Response(200, {"problems": []})
        if path == "/api/catalogue/search":
            return Response(200, {"revision": "1", "entries": []})
        if "/deliverables/" in path and method == "POST":
            if isinstance(self.deliverable, Response):
                return self.deliverable
            return Response(200, copy.deepcopy(self.deliverable))
        if path.startswith("/api/projects/") and method == "GET":
            if self.project is None:
                return Response(404, {"error": "project not found"})
            return Response(200, self.project, headers={"ETag": f'"{self.revision}"'})
        if path.startswith("/api/projects/") and method == "PUT":
            if self.put_response is not None:
                return self.put_response
            self.project = json
            self.revision += 1
            return Response(200, {"id": json["id"]}, headers={"ETag": f'"{self.revision}"'})
        return Response(404, {"error": "no route"})

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "PUT"]


class RailyardJobTestCase(TransactionTestCase):
    """Runs the app's jobs against a fake Railyard, with the app's settings overridden.

    A TransactionTestCase, as Nautobot's own job tests are: a job's transaction commits or rolls back for real, and
    its log, which Nautobot writes through a second connection (``job_logs``) so a rolled-back job keeps it, can be
    read back."""

    databases = ("default", "job_logs")

    def setUp(self):
        super().setUp()
        self.railyard = FakeRailyard()
        patcher = mock.patch.object(
            jobs,
            "make_railyard_client",
            lambda url, token, org: RailyardClient(url, token, org=org, session=self.railyard),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        overrider = override_settings(PLUGINS_CONFIG=app_settings())
        overrider.enable()
        self.addCleanup(overrider.disable)

    def run_job(self, class_name: str, username: str = "railyard-admin", **kwargs):
        job_model = Job.objects.get(module_name="nautobot_railyard.jobs", job_class_name=class_name)
        result = run_job_for_testing(job_model, username=username, **kwargs)
        result.refresh_from_db()
        return result

    @staticmethod
    def log(result) -> str:
        return "\n".join(e.message for e in JobLogEntry.objects.filter(job_result=result).order_by("created"))

    def assertSucceeded(self, result):
        self.assertEqual(result.status, "SUCCESS", f"{result.traceback}\n{self.log(result)}")

    def assertFailed(self, result, message: str):
        self.assertEqual(result.status, "FAILURE", self.log(result))
        self.assertIn(message, self.log(result))

    @staticmethod
    def grant(user, actions: list[str], labels: list[str], constraints=None) -> None:
        """An ObjectPermission for ``user``: ``actions`` on each ``app.model`` label."""
        permission = ObjectPermission.objects.create(
            name=f"{user.username} {'/'.join(actions)} {len(labels)}", actions=actions, constraints=constraints
        )
        permission.users.add(user)
        for label in labels:
            app, model = label.split(".")
            permission.object_types.add(ContentType.objects.get_by_natural_key(app, model))

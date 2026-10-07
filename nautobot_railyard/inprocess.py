"""Nautobot's own REST API, called in-process as the job's user.

railyard-sync's Nautobot loader and REST target (``railyard_sync.dcim.nautobot``, ``railyard_sync.export.
nautobot_rest``) talk to Nautobot through a ``requests``-like session. :class:`InProcessSession` is that session
inside a Job: each request is dispatched straight to Nautobot's API view for the path — no HTTP, no token, no
network — authenticated as the user who ran the job. So the app gets, for free and with no second implementation
to keep in step:

- the ownership rules, conflict handling and deletion guards railyard-sync's suite tests (against a fake Nautobot)
  and this app's suite tests again against a real one;
- Nautobot's own validation, serializers and filters, exactly as the REST API applies them;
- the user's object permissions, enforced per object by the API views (``restrict()``);
- the job's database transaction (a dry run is rolled back, a real run is all or nothing) and its change log
  (Nautobot runs every job inside a change context for its user).
"""

from __future__ import annotations

import json as jsonlib
from typing import Any
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.urls import Resolver404, resolve
from rest_framework.test import APIRequestFactory, force_authenticate

#: The URL railyard-sync's clients are given; requests never leave the process, so it is only a label.
BASE_URL = "http://nautobot.in-process"
#: railyard-sync's clients require a token; the session authenticates the user itself and ignores it.
TOKEN = "in-process"


class InProcessResponse:
    """The parts of ``requests.Response`` railyard-sync reads."""

    def __init__(self, status_code: int, content: bytes, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.content = content or b""
        self.headers = dict(headers or {})

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.content)


def _host() -> str:
    """A host name Django accepts (``ALLOWED_HOSTS``), for the API's own links in responses."""
    for host in getattr(settings, "ALLOWED_HOSTS", []) or []:
        host = str(host).strip()
        if host in ("*", ""):
            return "localhost"
        if host.startswith("."):
            return host[1:]
        if "*" not in host:
            return host
    return "localhost"


class InProcessSession:
    """A ``requests.Session`` stand-in that answers from Nautobot's API views, as ``user``."""

    def __init__(self, user) -> None:
        if user is None or not getattr(user, "is_active", False):
            raise ValueError("an active Nautobot user is required")
        self.user = user
        self._factory = APIRequestFactory()
        self._host = _host()

    def request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        params: Any = None,
        json: Any = None,
        timeout: float | None = None,
        verify: Any = None,
        **_: Any,
    ) -> InProcessResponse:
        path = urlsplit(url).path
        pairs = list(params.items()) if isinstance(params, dict) else list(params or [])
        query = urlencode(pairs, doseq=True)
        try:
            match = resolve(path)
        except Resolver404:
            return InProcessResponse(404, b'{"detail": "Not found."}')
        request = self._factory.generic(
            method.upper(),
            path + (f"?{query}" if query else ""),
            data=jsonlib.dumps(json) if json is not None else "",
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            HTTP_HOST=self._host,
        )
        force_authenticate(request, user=self.user)
        response = match.func(request, *match.args, **match.kwargs)
        if hasattr(response, "render"):
            response.render()
        return InProcessResponse(response.status_code, response.content, dict(response.items()))

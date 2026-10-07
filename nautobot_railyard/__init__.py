"""Railyard for Nautobot: export a Railyard data-centre design into Nautobot, and import Nautobot locations into
Railyard.

The ``NautobotAppConfig`` import is guarded so the package (its Nautobot-free :mod:`.policy`) imports outside a
Nautobot install, which is how the policy unit tests run. Inside Nautobot, ``config`` is defined and picked up by
the app loader as usual, and the jobs are registered from :mod:`.jobs`.
"""

from __future__ import annotations

__version__ = "0.1.0"

try:
    from nautobot.apps import NautobotAppConfig
except Exception:  # pragma: no cover - only taken outside a Nautobot runtime
    NautobotAppConfig = None

if NautobotAppConfig is not None:

    class RailyardConfig(NautobotAppConfig):
        name = "nautobot_railyard"
        verbose_name = "Railyard"
        description = (
            "Export a Railyard data-centre design into Nautobot (locations, racks, devices, cables), and import "
            "Nautobot locations into Railyard as a design baseline."
        )
        version = __version__
        author = "Railyard"
        author_email = "support@railyard.sh"
        base_url = "railyard"
        # Nautobot 2.x, tested with 2.3 and 2.4: railyard-sync needs Python 3.12, which Nautobot supports from 2.3.
        min_version = "2.3.0"
        max_version = "2.99.99"
        default_settings = {
            "railyard_url": "https://railyard.sh",
            # The Railyard token, from the first of these that is set: a Nautobot Secret (by name), a file (a
            # mounted secret), an environment variable, or the value itself (not recommended).
            "railyard_token_secret": None,
            "railyard_token_file": None,
            "railyard_token_env": "RAILYARD_TOKEN",
            "railyard_token": "",
            "railyard_org": None,  # default org (id, slug or name); always allowed
            "railyard_allowed_orgs": [],  # further orgs users may pick; nothing else is reachable
            "railyard_allowed_projects": [],  # optional project id/slug allowlist; empty = any project
            # The URL this Nautobot is reached at (as railyard-sync's CLI would use it): an import records it, so
            # a CLI refresh and an app refresh recognise the same source. Default: from ALLOWED_HOSTS.
            "nautobot_url": None,
        }
        jobs = "jobs.jobs"

    config = RailyardConfig

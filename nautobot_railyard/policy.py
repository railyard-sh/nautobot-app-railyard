"""Decisions that don't need Nautobot: which Railyard orgs and projects a job may target, how the app's token is
read, and which Nautobot permissions each job needs.

Kept free of Django and Nautobot imports so it is unit-tested on its own (``tests/test_policy.py``, also run by
Nautobot's test runner); the jobs call into it and enforce the result. The org/project allowlist mirrors the
NetBox plugin's, so both are configured the same way.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

#: A project id or URL slug. Railyard ids and slugs are ASCII words joined by ``-``/``_``/``.``.
_PROJECT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,199}$")

#: What the export reads and writes: ``(app, model)`` for each synced model.
EXPORT_MODELS = (
    ("dcim", "locationtype"),
    ("dcim", "location"),
    ("dcim", "manufacturer"),
    ("dcim", "devicetype"),
    ("dcim", "rack"),
    ("dcim", "device"),
    ("dcim", "interface"),
    ("dcim", "rearport"),
    ("dcim", "frontport"),
    ("dcim", "poweroutlet"),
    ("dcim", "powerport"),
    ("dcim", "cable"),
    ("extras", "status"),
    ("extras", "role"),
)
#: What the import reads.
IMPORT_MODELS = (
    ("dcim", "locationtype"),
    ("dcim", "location"),
    ("dcim", "rack"),
    ("dcim", "device"),
    ("dcim", "devicetype"),
    ("dcim", "interface"),
    ("dcim", "frontport"),
    ("dcim", "rearport"),
    ("dcim", "powerport"),
    ("dcim", "poweroutlet"),
    ("dcim", "consoleport"),
    ("dcim", "interfacetemplate"),
    ("dcim", "frontporttemplate"),
    ("dcim", "rearporttemplate"),
    ("dcim", "powerporttemplate"),
    ("dcim", "poweroutlettemplate"),
    ("dcim", "consoleporttemplate"),
    ("dcim", "cable"),
    ("dcim", "powerpanel"),
    ("dcim", "powerfeed"),
)


def export_permissions(*, dry_run: bool, allow_deletes: bool) -> frozenset[str]:
    """The Nautobot permissions a user needs to run the export with these options.

    A dry run reads Nautobot (and shows what it found), so it needs ``view`` on every synced model and on tags
    and custom fields. Applying needs ``add`` and ``change`` as well, and ``delete`` when deletes are on.
    Object-level constraints on those permissions are enforced per object by Nautobot's own API, which the job
    calls in-process as the user. Creating the ownership tag and the custom fields on a first run needs more
    (``extras.add_tag``…); the run says so when they are missing."""
    actions = ["view"]
    if not dry_run:
        actions += ["add", "change"]
        if allow_deletes:
            actions.append("delete")
    perms = {f"{app}.{action}_{model}" for action in actions for app, model in EXPORT_MODELS}
    perms |= {"extras.view_tag", "extras.view_customfield"}
    return frozenset(perms)


def import_permissions() -> frozenset[str]:
    """The Nautobot permissions a user needs to import into Railyard: ``view`` on everything the import reads."""
    return frozenset(f"{app}.view_{model}" for app, model in IMPORT_MODELS)


def missing(has_perm: Callable[[str], bool], perms: Iterable[str]) -> list[str]:
    """The permissions ``has_perm(codename)`` denies, sorted (empty when all are held)."""
    return sorted(p for p in perms if not has_perm(p))


class TargetNotAllowed(ValueError):
    """The requested org or project is not one this Nautobot is configured to use."""


def as_tuple(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """A setting given as a list, tuple, set or comma-separated string, as a tuple of stripped, non-empty strings
    in order, without case-insensitive duplicates."""
    if value is None:
        return ()
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return tuple(out)


def validate_project_ref(ref: str | None) -> str:
    text = (ref or "").strip()
    if not _PROJECT_REF_RE.match(text):
        raise TargetNotAllowed("Enter a Railyard project id or URL slug (letters, digits, '-', '_', '.' or '~').")
    return text


@dataclass(frozen=True)
class SyncTargets:
    """The orgs and projects the jobs may use in Railyard.

    ``orgs`` are org references exactly as an administrator configured them (id, slug or name); ``""`` stands for
    the token owner's personal org and is only permitted when no org is configured at all. ``projects`` is empty
    when any project in a permitted org may be used."""

    orgs: tuple[str, ...]
    projects: tuple[str, ...] = ()
    default: str = ""

    @classmethod
    def from_settings(
        cls,
        *,
        default_org: str | None,
        allowed_orgs: str | Iterable[str] | None,
        allowed_projects: str | Iterable[str] | None,
    ) -> SyncTargets:
        default = (default_org or "").strip()
        orgs = as_tuple(([default] if default else []) + list(as_tuple(allowed_orgs)))
        if not orgs:
            orgs = ("",)
        return cls(orgs=orgs, projects=as_tuple(allowed_projects), default=default)

    def org_choices(self) -> list[tuple[str, str]]:
        return [(org, org or "Personal organisation of the token's user") for org in self.orgs]

    def check_org(self, ref: str | None) -> str | None:
        """The configured org reference matching ``ref`` (``None`` for the personal org). A blank ``ref`` means
        the default org. Anything not configured raises ``TargetNotAllowed``."""
        wanted = (ref or "").strip() or self.default
        for org in self.orgs:
            if org.lower() == wanted.lower():
                return org or None
        raise TargetNotAllowed(
            f"Organisation {wanted or '(personal)'!r} is not in this Nautobot's railyard_allowed_orgs."
        )

    def check_project(self, ref: str | None) -> str:
        text = validate_project_ref(ref)
        if self.projects:
            for project in self.projects:
                if project.lower() == text.lower():
                    return project
            raise TargetNotAllowed(f"Project {text!r} is not in this Nautobot's railyard_allowed_projects.")
        return text

    def check_new_project(self) -> None:
        """A new estate may be created only when no project allowlist restricts the jobs to existing ones."""
        if self.projects:
            raise TargetNotAllowed(
                "This Nautobot may only use the projects in railyard_allowed_projects, so an import can refresh one "
                "of them but not create a new estate."
            )


def read_token(
    *,
    token: str | None = None,
    token_file: str | None = None,
    token_env: str | None = None,
    secret: Callable[[], str] | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    """The Railyard token, from the first source that is configured: a Nautobot Secret (``secret()``, which
    resolves it), a file (a mounted secret), an environment variable, or the value itself."""
    if secret is not None:
        return (secret() or "").strip()
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"Can't read railyard_token_file {token_file!r}: {exc.strerror or exc}") from None
    env = os.environ if environ is None else environ
    if token_env and env.get(token_env, "").strip():
        return env[token_env].strip()
    return (token or "").strip()

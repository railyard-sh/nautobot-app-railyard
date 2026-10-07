"""The app's settings, read from Nautobot's ``PLUGINS_CONFIG["nautobot_railyard"]`` (see ``__init__.py`` for the
defaults and the README for what each one does)."""

from __future__ import annotations

from django.conf import settings

from .policy import SyncTargets, read_token

APP = "nautobot_railyard"


def setting(key: str, default=None):
    return (settings.PLUGINS_CONFIG.get(APP) or {}).get(key, default)


def railyard_url() -> str:
    return str(setting("railyard_url") or "https://railyard.sh").strip()


def railyard_token() -> str:
    """The Railyard token, read at the start of each job (so a rotated file or secret needs no restart)."""
    secret_name = setting("railyard_token_secret")
    secret = None
    if secret_name:

        def secret() -> str:
            from nautobot.extras.models import Secret

            try:
                return Secret.objects.get(name=secret_name).get_value()
            except Secret.DoesNotExist:
                raise ValueError(f"railyard_token_secret names no Nautobot Secret ({secret_name!r}).") from None
            except Exception as exc:  # a provider error (SecretError and its kin): say which secret, not its value
                raise ValueError(f"Can't read the Nautobot Secret {secret_name!r}: {type(exc).__name__}.") from None

    return read_token(
        token=setting("railyard_token", ""),
        token_file=setting("railyard_token_file"),
        token_env=setting("railyard_token_env", "RAILYARD_TOKEN"),
        secret=secret,
    )


def sync_targets() -> SyncTargets:
    """The orgs/projects the jobs may use (``railyard_org``, ``railyard_allowed_orgs``/``_projects``)."""
    return SyncTargets.from_settings(
        default_org=setting("railyard_org"),
        allowed_orgs=setting("railyard_allowed_orgs"),
        allowed_projects=setting("railyard_allowed_projects"),
    )


def nautobot_url() -> str:
    """The URL this Nautobot is reached at, as an import records it (``nautobot_url``, else from
    ``ALLOWED_HOSTS``)."""
    configured = str(setting("nautobot_url") or "").strip().rstrip("/")
    if configured:
        return configured
    for host in getattr(settings, "ALLOWED_HOSTS", []) or []:
        host = str(host).strip().lstrip(".")
        if host and host != "*" and "*" not in host:
            return f"https://{host}"
    return "http://localhost"

"""Nautobot configuration for developing and testing the app: the local Docker environment
(``development/docker-compose.yml``), CI, and ``nautobot-server test`` on a workstation.

Everything comes from the environment (``NAUTOBOT_DB_*``, ``NAUTOBOT_REDIS_*``, ``RAILYARD_*``). Not for production.
"""

import os
import sys

from nautobot.core.settings import *  # noqa: F403
from nautobot.core.settings_funcs import is_truthy, parse_redis_connection

DEBUG = is_truthy(os.getenv("NAUTOBOT_DEBUG", "false"))
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

ALLOWED_HOSTS = os.getenv("NAUTOBOT_ALLOWED_HOSTS", "localhost 127.0.0.1").split(" ")
SECRET_KEY = os.getenv("NAUTOBOT_SECRET_KEY", "development-only-not-a-secret-development-only-not-a-secret")

DATABASES = {
    "default": {
        "NAME": os.getenv("NAUTOBOT_DB_NAME", "nautobot"),
        "USER": os.getenv("NAUTOBOT_DB_USER", "nautobot"),
        "PASSWORD": os.getenv("NAUTOBOT_DB_PASSWORD", ""),
        "HOST": os.getenv("NAUTOBOT_DB_HOST", "localhost"),
        "PORT": os.getenv("NAUTOBOT_DB_PORT", "5432"),
        "CONN_MAX_AGE": int(os.getenv("NAUTOBOT_DB_TIMEOUT", "300")),
        "ENGINE": "django.db.backends.postgresql",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": parse_redis_connection(redis_database=1),
        "TIMEOUT": 300,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}
CELERY_BROKER_URL = parse_redis_connection(redis_database=0)
CELERY_RESULT_BACKEND = "nautobot.core.celery.backends.NautobotDatabaseBackend"
INSTALLATION_METRICS_ENABLED = False

PLUGINS = ["nautobot_railyard"]
PLUGINS_CONFIG = {
    "nautobot_railyard": {
        "railyard_url": os.getenv("RAILYARD_URL", "https://railyard.sh"),
        # The token comes from the RAILYARD_TOKEN environment variable (the app's default); never put it here.
        "railyard_org": os.getenv("RAILYARD_ORG") or None,
        "railyard_allowed_orgs": [o for o in os.getenv("RAILYARD_ALLOWED_ORGS", "").split(",") if o.strip()],
        "nautobot_url": os.getenv("NAUTOBOT_URL") or None,
    }
}

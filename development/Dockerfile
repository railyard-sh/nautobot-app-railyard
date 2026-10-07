# A Nautobot with this app installed, for local development and testing (development/docker-compose.yml).
ARG NAUTOBOT_VERSION=2.4
ARG PYTHON_VER=3.12
FROM ghcr.io/nautobot/nautobot:${NAUTOBOT_VERSION}-py${PYTHON_VER}

# railyard-sync, the app's core, from a release tag's archive (no git needed in the image).
ARG RAILYARD_SYNC_REF=v0.2.0
RUN pip install --no-cache-dir "railyard-sync @ https://github.com/railyard-sh/railyard-sync/archive/${RAILYARD_SYNC_REF}.tar.gz"

# The app, editable: docker-compose.yml mounts the checkout over /source, so edits apply on restart.
COPY --chown=nautobot:nautobot pyproject.toml README.md /source/
COPY --chown=nautobot:nautobot nautobot_railyard /source/nautobot_railyard
RUN pip install --no-cache-dir --no-deps -e /source

COPY development/nautobot_config.py /opt/nautobot/nautobot_config.py

# Pinned by digest, not by tag: a tag can be repointed, a digest cannot.
# python:3.12.16-slim-bookworm, pushed 2026-10-28.
ARG BASE=python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

# --------------------------------------------------------------------------
# builder: resolve dependencies into a venv we can copy wholesale
# --------------------------------------------------------------------------
FROM ${BASE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name 'tests' -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
FROM ${BASE}

LABEL org.opencontainers.image.title="plex-anilist-bridge" \
      org.opencontainers.image.description="Plex to AniList to MyAnimeList sync with an explain-everything web UI" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/666Syner/plex-anilist-bridge"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    CONFIG_DIR=/config \
    PORT=8787 \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    TZ=UTC

# setpriv (util-linux) is already in the base image, so dropping privileges
# costs no extra package and nothing extra to keep pinned. Fail the build
# rather than discover it missing at runtime.
RUN command -v setpriv >/dev/null 2>&1 \
 || { echo 'setpriv is missing from the base image'; exit 1; }

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app/ ./app/
COPY docker/entrypoint.sh docker/healthcheck.py ./docker/
RUN chmod +x ./docker/entrypoint.sh

VOLUME ["/config"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "-m", "app.main"]

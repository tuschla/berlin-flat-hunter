# Image for the TrueNAS Custom App (deploy/truenas-app.yaml). Built and pushed to
# ghcr.io/tuschla/berlin-flat-hunter by .github/workflows/image.yml.
#
# Contains code only. Config and state come from mounts at runtime:
#   /app/hunter.yaml, /app/profiles/   read-only config (Telegram tokens, PII)
#   /data                              read-write state; /app/data -> /data
# Runs as uid:gid 568:568 (TrueNAS `apps`) and never writes into /app.

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

# flathunter is a git dependency (commit pinned in uv.lock); uv needs git to fetch it.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Install deps (no project) from the lockfile
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Install project
COPY berlin_flat_hunter ./berlin_flat_hunter
COPY main.py README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim

LABEL org.opencontainers.image.source=https://github.com/tuschla/berlin-flat-hunter

# chromium + driver: Selenium auto-apply (currently disabled in every profile,
# kept so it can be switched back on). curl: ntfy alerts and the supercronic
# download. util-linux: flock for deploy/run-job.sh. tzdata: TZ=Europe/Berlin.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver fonts-liberation ca-certificates \
        curl util-linux tzdata \
    && rm -rf /var/lib/apt/lists/*

# supercronic runs deploy/crontab in the `watchdog` service.
# Version and SHA1 come from the GitHub release notes.
ARG SUPERCRONIC_VERSION=v0.2.50
ARG SUPERCRONIC_SHA1SUM=0795da10bf6303e9680a2c2bd8dd6b9aa33ebda2
RUN curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA1SUM}  /usr/local/bin/supercronic" | sha1sum -c - \
    && chmod 0755 /usr/local/bin/supercronic

ENV BFH_REPO=/app \
    HOME=/data/home \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Berlin \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /app /app
COPY scripts ./scripts
COPY deploy ./deploy

# State lives on the /data mount. The symlink keeps `data/...` paths and the
# watchdog's $BFH_REPO/data paths working without code changes.
RUN chmod 0755 deploy/entrypoint.sh deploy/run-job.sh \
    && ln -s /data /app/data

USER 568:568

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["python", "main.py", "--hunter", "/app/hunter.yaml"]

FROM python:3.12-slim AS builder

# Install uv for fast dep resolution
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Install deps (no project) using lockfile
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Install project
COPY berlin_flat_hunter ./berlin_flat_hunter
COPY main.py README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# Chromium + driver for selenium (Kleinanzeigen crawl + auto-apply).
# Skip if you only crawl Gewobag/WBM/Gesobau and don't auto-apply.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver ca-certificates fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BFH_DATA_DIR=/data \
    BFH_CONFIG=/config/config.yaml

WORKDIR /app
COPY --from=builder /app /app

# Non-root user; /data and /config will be volume-mounted.
RUN useradd -u 1000 -m -s /bin/bash hunter \
    && mkdir -p /data /config \
    && chown -R hunter:hunter /app /data /config
USER hunter

VOLUME ["/data", "/config"]

ENTRYPOINT ["python", "main.py"]
CMD ["--config", "/config/config.yaml"]


# --------------------------------------------------------------------------
# runtime-vnc — same app, but Chrome runs *headed* inside Xvfb and is
# accessible via a noVNC web client on port 6080. Persistent Chrome profile
# is volume-mounted so cookies/login state survive container restarts —
# the workaround for sites whose reCAPTCHA fingerprints anything that
# looks like fresh / headless / non-warmed automation.
#
# Build:   docker build --target runtime-vnc -t bfh:vnc .
# Run:     docker run -p 6080:6080 -v ./chrome-profile:/data/chrome-profile bfh:vnc
# Connect: open http://localhost:6080/vnc.html in any browser
# --------------------------------------------------------------------------
FROM runtime AS runtime-vnc

USER root

# xvfb         → virtual X server (no physical display)
# fluxbox      → minimal window manager (some sites probe for one)
# x11vnc       → bridges Xvfb to VNC protocol on :5900
# novnc        → web-based VNC client served at /usr/share/novnc/
# websockify   → translates noVNC's WebSocket frames ↔ raw VNC bytes
# supervisor   → keeps Xvfb / fluxbox / x11vnc / websockify / app alive
# dbus + libgtk → Chrome refuses to start without these in headed mode
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc fluxbox \
        novnc websockify \
        supervisor \
        dbus dbus-x11 libgtk-3-0 libnss3 libxss1 libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/supervisor

COPY docker/supervisord.conf /etc/supervisor/conf.d/bfh.conf

# Profile dir lives under /data so it shares the per-profile volume that
# already holds db.sqlite + stats.db + schema_monitor.json.
ENV BFH_CHROME_PROFILE=/data/chrome-profile \
    DISPLAY=:99

# noVNC web client. Raw VNC (5900) is intentionally NOT exposed by default —
# noVNC speaks WebSocket, not raw RFB, and exposing 5900 invites scans.
EXPOSE 6080

USER hunter

ENTRYPOINT ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/bfh.conf", "-n"]
CMD []

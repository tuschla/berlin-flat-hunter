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

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml ./

# ── Production stage ──────────────────────────────────────────────────────────
FROM base AS production

RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

COPY app/ ./app/

# Download Bootstrap + Icons assets at build time so the image works offline.
# Fonts must sit alongside the Icons CSS so relative url("./fonts/...") resolves.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && mkdir -p /app/app/static/css/fonts /app/app/static/js \
    && curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" \
         -o /app/app/static/css/bootstrap.min.css \
    && curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js" \
         -o /app/app/static/js/bootstrap.bundle.min.js \
    && curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" \
         -o /app/app/static/css/bootstrap-icons.min.css \
    && curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2" \
         -o /app/app/static/css/fonts/bootstrap-icons.woff2 \
    && curl -sLf "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff" \
         -o /app/app/static/css/fonts/bootstrap-icons.woff \
    && apt-get remove -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data /config

EXPOSE 3002

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3002"]

# ── Test stage ────────────────────────────────────────────────────────────────
FROM base AS test

# Install all deps including dev (pytest)
RUN uv sync 2>/dev/null || uv sync

COPY app/ ./app/
COPY tests/ ./tests/

# Static dir must exist — main.py mounts it at import time
RUN mkdir -p /data /config /app/app/static/css /app/app/static/js

CMD ["uv", "run", "pytest", "tests/", "-v"]

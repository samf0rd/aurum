# ─────────────────────────────────────────────
# Stage 1: base — Python + system dependencies
# ─────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─────────────────────────────────────────────
# Stage 2: builder — install dependencies
# ─────────────────────────────────────────────
FROM base AS builder

COPY pyproject.toml .
RUN pip install --upgrade pip && \
    pip install build && \
    pip install -e ".[dev]"

# ─────────────────────────────────────────────
# Stage 3: test — run test suite in CI
# ─────────────────────────────────────────────
FROM builder AS test

COPY . .
RUN pytest tests/ -v --tb=short --cov=src --cov-report=term-missing

# ─────────────────────────────────────────────
# Stage 4: production — minimal runtime image
# ─────────────────────────────────────────────
FROM base AS production

# Non-root user for security
RUN groupadd --gid 1001 trader && \
    useradd --uid 1001 --gid trader --shell /bin/bash --create-home trader

COPY pyproject.toml .
RUN pip install --upgrade pip setuptools wheel && pip install .

COPY src/ ./src/
COPY config/ ./config/
# Deployment backtest — required for dashboard REPLAY tab
COPY results/exp-020.json ./results/exp-020.json

# Logs directory owned by non-root user
RUN mkdir -p /app/logs /app/data && chown -R trader:trader /app/logs /app/data

USER trader

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "src.main"]

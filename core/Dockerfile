# AEGIS v2 Core API
FROM node:22-alpine AS frontend-build

WORKDIR /build/admin-panel/frontend
COPY admin-panel/frontend/package*.json ./
RUN npm ci
COPY admin-panel/frontend/ ./
RUN npm run build

FROM python:3.12-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Deps layer
COPY core/pyproject.toml core/pyproject.toml
RUN mkdir -p core/src/aegis && touch core/src/aegis/__init__.py
RUN pip install --no-cache-dir "./core[google]"

# Source
COPY core/src/ core/src/
RUN pip install --no-deps --force-reinstall ./core

# Runtime
FROM python:3.12-slim

# curl: healthcheck. git + openssh-client: the books and notes checkouts
# (`services/git_checkout.py`) clone and push over SSH with a deploy key.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl openssh-client git && rm -rf /var/lib/apt/lists/*

# hledger — the books. Core and the worker share ONE books checkout, so the
# version and its checksum live once, in the script both images run.
COPY docker/install-hledger.sh /tmp/install-hledger.sh
RUN /tmp/install-hledger.sh && rm -f /tmp/install-hledger.sh

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY migrations/ migrations/
COPY personalities/ personalities/
# Seed data (agents/channels/resources/activities/todoist) — without this the
# startup seeding silently no-ops (seed_dir_not_found) and the DB ships empty,
# so Flows/Agents/Resources all render blank. No secrets live here.
COPY config/seed/ config/seed/
COPY --from=frontend-build /build/admin-panel/frontend/dist/ admin-panel/frontend/dist/

RUN useradd --create-home --shell /bin/bash aegis \
    && chown -R aegis:aegis /app
USER aegis

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "aegis"]

# AEGIS v2 Core API
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

# curl: healthcheck. openssh-client: RemoteScriptConnector SSHes to node-a
# for infrastructure chat tools and the claude_code task handler.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl openssh-client && rm -rf /var/lib/apt/lists/*

# kubectl: infra registry k8s ops (services/infra.py runs it against the
# per-entry kubeconfig stored encrypted in the DB).
ARG TARGETARCH=amd64
ARG KUBECTL_VERSION=v1.31.4
RUN curl -fsSL -o /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY migrations/ migrations/
COPY personalities/ personalities/
# Seed data (agents/channels/resources/activities/todoist) — without this the
# startup seeding silently no-ops (seed_dir_not_found) and the DB ships empty,
# so Flows/Agents/Resources all render blank. No secrets live here.
COPY config/seed/ config/seed/
COPY admin-panel/frontend/dist/ admin-panel/frontend/dist/

RUN useradd --create-home --shell /bin/bash aegis \
    && chown -R aegis:aegis /app
USER aegis

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "aegis"]

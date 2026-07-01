# AEGIS v2 Worker (Temporal)
FROM python:3.12-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Core deps first (worker depends on core)
COPY core/pyproject.toml core/pyproject.toml
RUN mkdir -p core/src/aegis && touch core/src/aegis/__init__.py
RUN pip install --no-cache-dir "./core[google]"

# Worker deps
COPY worker/pyproject.toml worker/pyproject.toml
RUN mkdir -p worker/src/aegis_worker && touch worker/src/aegis_worker/__init__.py
RUN pip install --no-cache-dir ./worker

# Source — core first, then worker
COPY core/src/ core/src/
RUN pip install --no-deps --force-reinstall ./core

COPY worker/src/ worker/src/
RUN pip install --no-deps --force-reinstall ./worker

# Runtime
FROM python:3.12-slim

# Runtime dependencies:
#   openssh-client — RemoteScript + Homelab backup listing + DOCKER_HOST=ssh://...
#   curl — health
#   ffmpeg — media
#   openssl — Homelab Guardian cert radar TLS probe
#   tini — PID-1 init that reaps zombies left over when a subprocess's child
#          (e.g. ssh spawned by `docker --context`) outlives its parent.
#          Without tini, Python is PID 1 and never waitpid()s those grandchildren.
# Docker CLI: Debian 13 (trixie) ships docker.io as daemon-only — pull the static
# client binary so ServiceDriftFlow can run `docker --context ... service ls`.
ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client curl ffmpeg openssl tini \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod 0755 /usr/local/bin/docker \
    && rm -rf /tmp/docker \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY migrations/ migrations/
COPY runbooks/ runbooks/

RUN useradd --create-home --shell /bin/bash aegis \
    && chown -R aegis:aegis /app
USER aegis

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "aegis_worker"]

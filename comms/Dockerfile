# AEGIS comms — channel-adapter bot + delivery server
FROM python:3.12-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY comms/pyproject.toml comms/pyproject.toml
RUN mkdir -p comms/src/aegis_comms && touch comms/src/aegis_comms/__init__.py
RUN pip install --no-cache-dir ./comms

COPY comms/src/ comms/src/
RUN pip install --no-deps --force-reinstall ./comms

# Runtime
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --shell /bin/bash aegis \
    && chown -R aegis:aegis /app
USER aegis

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8081/api/health || exit 1

CMD ["python", "-m", "aegis_comms"]

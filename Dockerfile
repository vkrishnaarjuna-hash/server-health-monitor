# Dockerfile
# Production-ready, non-root container image for Server Health Monitor.

FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr and writing .pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# OCI Metadata and build arguments
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION="0.1.0"

LABEL org.opencontainers.image.title="server-health-monitor" \
      org.opencontainers.image.description="Lightweight, incremental, checkpointed server health monitor" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com" \
      org.opencontainers.image.vendor="Server Health Monitor Project"

WORKDIR /app

# Create a fixed-UID non-root user and group (10001)
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g appuser -s /bin/sh -M appuser

# Pre-create data mount points:
#   /data/org   : organization logs (mounted read-only at runtime)
#   /data/state : checkpoint directory (mounted writable, owned by appuser)
RUN mkdir -p /data/org /data/state && \
    chown -R appuser:appuser /data/state

# Copy packaging metadata and source code only
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install the application and CLI entrypoint, then remove build tools to minimize attack surface and eliminate CVEs
RUN pip install --no-cache-dir . && \
    pip uninstall -y pip setuptools wheel && \
    rm -rf /root/.cache

# Declare standard volumes
VOLUME ["/data/org", "/data/state"]

# Default environment configuration
ENV HM_ROOT=/data/org \
    HM_STATE_FILE=/data/state/state.json \
    HM_INTERVAL=30 \
    HM_MIN_SEVERITY=ERROR

# Run as non-root user
USER 10001:10001

# Entrypoint delegates to the installed health-monitor CLI
ENTRYPOINT ["health-monitor"]

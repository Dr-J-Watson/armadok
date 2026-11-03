# syntax=docker/dockerfile:1
# One image for the hub and the workers: the hub runs the default command, a worker runs
# `python -m armadok.worker` (see docker-compose.worker.example.yml).

FROM node:22-slim AS web
WORKDIR /web
# Locked install without dependency scripts (supply-chain hardening), then the production build.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN python -m venv /opt/venv
# Hashed lock file: the build fails if any package differs from what was reviewed.
COPY backend/requirements.txt .
RUN /opt/venv/bin/pip install --require-hashes -r requirements.txt

FROM python:3.12-slim
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="Armadok" \
      org.opencontainers.image.description="Deploy Docker Compose projects, publish them with Traefik and see how everything is connected." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ARMADOK_VERSION=${APP_VERSION} \
    ARMADOK_DATA_DIR=/data \
    ARMADOK_WORKER_DIR=/data \
    ARMADOK_WEB_DIR=/app/web

# Not root, no shell, no home. The data folder is private to this user (it holds keys).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin armadok \
 && mkdir /data && chown 10001:10001 /data && chmod 700 /data

COPY --from=build /opt/venv /opt/venv
# Docker CLI and Compose plugin only (no daemon): `docker compose` runs the deployments.
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY backend/armadok /app/armadok
COPY --from=web /web/dist /app/web
WORKDIR /app
USER 10001
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"]

# --ws-max-size caps worker messages at 1 MiB before they are read into memory.
CMD ["uvicorn", "armadok.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--ws-max-size", "1048576", "--no-server-header"]

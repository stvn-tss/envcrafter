# syntax=docker/dockerfile:1
# EnvCrafter API image. Build context: repository root
#   docker build -f backend/Dockerfile .
# (deploy/docker-compose.yml does this for you)

FROM docker.io/library/docker:29.8.1-cli AS docker-cli
FROM docker.io/docker/compose-bin:v5.5.1 AS compose-bin
FROM ghcr.io/astral-sh/uv:0.12.18 AS uv

FROM docker.io/library/python:3.14.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Docker CLI + Compose plugin only (static binaries): the daemon is reached
# through the socket proxy, never through a mounted socket.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=compose-bin /docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
COPY templates /app/templates
COPY frontend /app/frontend

# Unprivileged runtime user; the workspaces and settings volumes inherit this ownership.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin envcrafter \
    && install -d -o 10001 -g 10001 -m 0700 /data/workspaces /data/settings

USER 10001
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/tmp \
    DOCKER_CONFIG=/tmp/.docker \
    ENVCRAFTER_TEMPLATES_DIR=/app/templates \
    ENVCRAFTER_FRONTEND_DIR=/app/frontend \
    ENVCRAFTER_WORKSPACES_DIR=/data/workspaces \
    ENVCRAFTER_SETTINGS_FILE=/data/settings/settings.json

EXPOSE 8000
# 0.0.0.0 inside the container only: the port is never published, Traefik
# reaches it over the internal control network.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]

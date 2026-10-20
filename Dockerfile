# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS frontend-build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./frontend/
RUN npm run build

FROM python:3.13-slim-bookworm AS python-build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt backend/requirements-ai.txt backend/requirements-cloud.txt ./backend/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install -r backend/requirements-cloud.txt

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_DOTENV_DISABLED=1 \
    GUARDMATE_MODE=cloud \
    PATH="/opt/venv/bin:$PATH" \
    MALLOC_ARENA_MAX=2 \
    HF_HOME=/app/.cache/huggingface
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 guardmate \
    && useradd --uid 10001 --gid guardmate --home-dir /app --no-create-home guardmate \
    && mkdir -p /app/.cache/huggingface \
    && chown -R guardmate:guardmate /app/.cache
COPY --from=python-build /opt/venv/ /opt/venv/
COPY --from=frontend-build /app/dist/ /app/dist/
COPY backend/guardmate/ ./backend/guardmate/
USER guardmate:guardmate
EXPOSE 10000
CMD ["sh", "-c", "exec python -m uvicorn guardmate.cloud_app:create_cloud_app --factory --app-dir backend --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips '*' --no-access-log"]

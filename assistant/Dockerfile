FROM node:24-bookworm-slim@sha256:6042915a8b58012e6699f998ed96bca43ec9d8582447bdb68bc2445d0fafeb79 AS web
WORKDIR /web
COPY assistant/web/package.json assistant/web/package-lock.json ./
RUN npm ci
COPY assistant/web/index.html assistant/web/vite.config.ts assistant/web/tsconfig.json ./
COPY assistant/web/src ./src
RUN npm run build

FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
COPY --from=ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 /uv /usr/local/bin/uv
RUN groupadd -g 10001 assistant && useradd -u 10001 -g assistant -m assistant
WORKDIR /app/assistant
COPY assistant/pyproject.toml assistant/uv.lock ./
COPY assistant/src ./src
RUN uv sync --locked --no-dev --no-editable && \
    mkdir -p /app/cache/hf /app/repo/docs /app/repo/ops/linux && \
    chown -R 10001:10001 /app
COPY README.md /app/repo/README.md
COPY docs /app/repo/docs
COPY ops/linux/README.md /app/repo/ops/linux/README.md
COPY --from=web /web/dist ./web/dist
ENV PATH="/app/assistant/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HOME=/app/cache/hf HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    LANGSMITH_TRACING=false MLFLOW_DISABLE_AGENT_HINT=1 \
    LEDGEROPS_ASSISTANT_CORPUS_ROOT=/app/repo \
    LEDGEROPS_ASSISTANT_WEB_DIST=/app/assistant/web/dist
USER 10001:10001
EXPOSE 8088
CMD ["python", "-m", "ledgerops_assistant.runtime", "serve", "--host", "0.0.0.0"]

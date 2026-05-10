# syntax=docker/dockerfile:1

FROM node:24-bookworm-slim AS frontend-builder

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm --prefix frontend ci

COPY frontend ./frontend
RUN npm --prefix frontend run build


FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY backend ./backend
COPY config ./config
COPY search-eval ./search-eval
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

RUN uv sync --locked --no-dev

EXPOSE 8000
VOLUME ["/var/lib/findme-v2"]

CMD ["findme", "serve", "--config", "config/findme.server.toml", "--host", "0.0.0.0", "--port", "8000", "--static-dir", "frontend/dist"]

FROM ghcr.io/astral-sh/uv:0.8.14 AS uv
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && chmod +x /app/docker/entrypoint.sh /app/docker/migrate.sh

EXPOSE 8080
ENTRYPOINT ["/app/docker/entrypoint.sh"]

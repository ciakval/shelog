FROM python:3.13-slim

ARG VERSION=dev
ARG BUILD_ID=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    SHELOG_DB_PATH=/app/data/shelog.db \
    SHELOG_DEVICES_FILE=/app/devices.toml \
    VERSION=${VERSION} \
    BUILD_ID=${BUILD_ID}

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md* ./
RUN uv sync --frozen --no-dev && mkdir -p /app/data

# compose.yaml sets `user:` to the server's deploy UID so the bind-mounted ./data is writable.
RUN useradd -m -u 1000 app
USER app

EXPOSE 8000
ENTRYPOINT ["shelog"]
CMD ["poll"]

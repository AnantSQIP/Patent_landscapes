FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app

# Dependencies first for layer caching; the lockfile makes the build reproducible.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 plr
USER plr
ENV PLR_CONFIG_FILE=/app/config/settings.yaml
ENTRYPOINT []
CMD ["plr", "--help"]

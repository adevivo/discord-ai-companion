FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.18 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATABASE_PATH=/data/companion.db

WORKDIR /app

# Dependencies first so they cache independently of source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir /data && chown app /data
USER app

CMD ["python", "-m", "companion"]

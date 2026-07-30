# Multi-stage: dependencies are resolved and installed in the builder, and only the
# resulting virtualenv and source are copied into the runtime image. The final image
# carries no uv, no build toolchain and no lockfile.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: application code changes on every commit,
# the lockfile does not, so this layer stays cached across most builds.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# Non-root, no login shell, no home directory to write into.
RUN groupadd --system --gid 1001 taskflow \
 && useradd --system --uid 1001 --gid taskflow --no-create-home taskflow

WORKDIR /app

COPY --from=builder --chown=taskflow:taskflow /app/.venv /app/.venv
COPY --from=builder --chown=taskflow:taskflow /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER taskflow

EXPOSE 8000

# Hits the liveness probe, which by design touches no dependency: a Postgres outage
# must not make the orchestrator kill an otherwise healthy container.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "taskflow_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

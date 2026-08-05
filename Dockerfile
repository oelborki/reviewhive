# syntax=docker/dockerfile:1

# Two stages, and the split earns its place: the build stage needs a compiler and
# pip's metadata, the runtime stage needs neither. What crosses between them is
# one virtualenv, so nothing that was only required to *install* the application
# is present in the image that runs it.

# --- build -------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS builder

# Wheels exist for every currently pinned dependency on this platform, so this is
# insurance rather than a requirement. It is cheap here and absent from the
# runtime stage, which means a future pin without a wheel fails at build time
# with a clear compiler error instead of at deploy time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# The whole source tree, because the project is installed as a package rather
# than mounted: `alembic/env.py` and the prompts under `agents/prompts/` are both
# resolved from the installed distribution, not from a working directory.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# `service` and not `dev`: the test suite is CI's job, and shipping pytest into a
# runtime image widens what an attacker finds without making anything work.
# `service` pulls in `db`, so alembic and asyncpg come with it — the entrypoint
# needs both.
RUN pip install ".[service]"

# --- runtime -----------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

# Non-root, with a fixed high uid so a bind mount's ownership is predictable.
# The process writes nothing outside /tmp.
RUN useradd --create-home --uid 10001 reviewhive

ENV PATH="/opt/venv/bin:$PATH" \
    # Log lines are the point of this service's observability, and a buffered
    # stdout holds them until the buffer fills — which, at a handful of lines per
    # delivery, can be a very long time. This is not a micro-optimisation to
    # remove.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Overridden by hosts that inject their own; see the entrypoint.
    PORT=8000

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Runtime artefacts, not build ones: the entrypoint applies migrations before the
# first request, so alembic.ini and the revision files have to exist in the
# running image. `src/` does not — the package is installed in the venv, and
# `prepend_sys_path` in alembic.ini resolves `reviewhive` from there.
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER reviewhive
EXPOSE 8000

# Shell form on purpose: $PORT has to expand at runtime, and the exec form does
# no expansion. `urllib` rather than curl keeps the runtime image free of a
# package installed solely to check on itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8000')+'/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn reviewhive.api.app:app --host 0.0.0.0 --port ${PORT}"]

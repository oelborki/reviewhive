#!/bin/sh
set -e

# Migrations run at start rather than in a separate release step.
#
# The reason is specific, not convenience: a schema that lags the code does not
# fail loudly. Running the merge pass against a database missing its revision
# raised a CheckViolationError inside `finish_review`, so the review completed,
# printed, and was never stored. That is the documented degradation working as
# designed, and it is exactly what makes a missed migration hard to notice.
#
# Safe here because there is exactly one replica. Several instances racing on
# `alembic upgrade head` is a real problem, and the answer to it is a release
# phase and a worker queue — both documented as the scaling path and both
# deliberately not built.
# Managed Postgres providers hand out a driver-less `postgresql://` URL — Render's
# `fromDatabase` connection string is one — and `Settings` rejects anything but
# `postgresql+asyncpg://`. That strictness is deliberate and worth keeping: a sync
# URL otherwise fails much later, deep inside SQLAlchemy, with a greenlet error
# that reads as a bug in this project. Normalising here puts the platform quirk at
# the platform boundary instead of loosening the application. An explicitly wrong
# driver, `postgresql+psycopg2://`, is still rejected as it should be.
case "${REVIEWHIVE_DATABASE_URL}" in
    postgresql://*)
        REVIEWHIVE_DATABASE_URL="postgresql+asyncpg://${REVIEWHIVE_DATABASE_URL#postgresql://}"
        export REVIEWHIVE_DATABASE_URL
        echo "entrypoint: rewrote a driver-less database URL to use asyncpg"
        ;;
esac

if [ -n "${REVIEWHIVE_DATABASE_URL}" ]; then
    echo "entrypoint: applying migrations"
    alembic upgrade head
else
    # A supported configuration, not an error: with no database the service runs
    # exactly as the CLI does and simply persists nothing. Said out loud because
    # the symptom otherwise is an empty `reviews` table and no explanation.
    echo "entrypoint: REVIEWHIVE_DATABASE_URL is unset — starting without persistence"
fi

exec "$@"

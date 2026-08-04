"""record what the merge pass costs

Revision ID: c3a71b0e94d2
Revises: fe9d45ab10b6
Create Date: 2026-08-04 17:02:11.884120

Hand-written, for the same reason as fe9d45ab10b6: Alembic does not compare CHECK
constraints, so `--autogenerate` produces an empty migration for a widening and
the generated file looks like there was nothing to do. Trusting it would ship
models emitting 'merge' against a database that still rejects it, and the failure
would arrive at write time inside a background task -- after the review had been
paid for.

One widening. `agent_calls.agent` gains merge, so the second deduplication pass is
costed through the same path as everything else that spends tokens on a review
rather than a parallel one.

UNIQUE(review_id, agent) is untouched and still holds: the merge pass makes at
most one call per review, whatever the number of candidate pairs.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3a71b0e94d2'
down_revision: Union[str, Sequence[str], None] = 'fe9d45ab10b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_AGENTS = ("security", "style", "architecture", "intent", "answer", "reconsider")
NEW_AGENTS = OLD_AGENTS + ("merge",)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace_check(table: str, name: str, condition: str) -> None:
    """Swap one CHECK for another, naming it exactly.

    Raw SQL rather than op.drop_constraint: the metadata carries a naming
    convention of ck_%(table_name)s_%(constraint_name)s, and Alembic applies it to
    whatever name it is handed -- so passing the real constraint name produces
    ck_agent_calls_ck_agent_calls_agent and the drop fails.
    """
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_{name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_{name} CHECK ({condition})")


def upgrade() -> None:
    """Upgrade schema."""
    _replace_check("agent_calls", "agent", _in("agent", NEW_AGENTS))


def downgrade() -> None:
    """Downgrade schema.

    Narrowing a CHECK fails against rows already holding the removed value, so a
    database that has run the merge pass cannot be downgraded without deciding what
    to do with those rows first. Deleting them would discard the cost history for
    reviews that are otherwise intact, which is a choice for whoever is
    downgrading and not one to make here.
    """
    _replace_check("agent_calls", "agent", _in("agent", OLD_AGENTS))

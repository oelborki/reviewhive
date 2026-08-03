"""record the calls a mention costs

Revision ID: fe9d45ab10b6
Revises: 7f852af95ccf
Create Date: 2026-08-03 11:21:25.290479

Hand-written, unlike every other revision here, because `--autogenerate` produced
an empty migration for this change. Alembic does not compare CHECK constraints, so
widening one is invisible to it. That is worth knowing rather than discovering:
trusting the generated file would have shipped models that emit 'intent' and
'mention' against a database that still rejects both, and the failure would arrive
at write time in a background task.

Two widenings, both additive to the set of accepted values:

- `agent_calls.agent` gains intent, answer and reconsider, so a mention's cost is
  recorded through the same path as a review's rather than a parallel one.
- `reviews.source` gains mention. A mention that answers a question spends tokens
  against a pull request and produces no findings, which is still a run worth
  costing; `reviews` means "something that spent money here", not "a review".

`UNIQUE(review_id, agent)` is untouched and still holds: a mention makes at most
one call per role.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'fe9d45ab10b6'
down_revision: Union[str, Sequence[str], None] = '7f852af95ccf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REVIEWERS = ("security", "style", "architecture")
CALL_AGENTS = REVIEWERS + ("intent", "answer", "reconsider")
OLD_SOURCES = ("cli", "webhook")
NEW_SOURCES = OLD_SOURCES + ("mention",)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace_check(table: str, name: str, condition: str) -> None:
    """Swap one CHECK for another, naming it exactly.

    Raw SQL rather than op.drop_constraint: the metadata carries a naming
    convention of ck_%(table_name)s_%(constraint_name)s, and Alembic applies it to
    whatever name it is handed -- so passing the real constraint name produces
    ck_agent_calls_ck_agent_calls_agent and the drop fails. Spelling the name out
    here removes the guesswork in both directions.
    """
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_{name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_{name} CHECK ({condition})")


def upgrade() -> None:
    """Upgrade schema."""
    _replace_check("agent_calls", "agent", _in("agent", CALL_AGENTS))
    _replace_check("reviews", "source", _in("source", NEW_SOURCES))


def downgrade() -> None:
    """Downgrade schema.

    Narrowing a CHECK fails against rows already holding the removed values, so a
    database that has served mentions cannot be downgraded without deciding what to
    do with those rows first. Same shape as the NOT NULL restore in the previous
    revision: reversible in principle, not automatically.
    """
    _replace_check("reviews", "source", _in("source", OLD_SOURCES))
    _replace_check("agent_calls", "agent", _in("agent", REVIEWERS))

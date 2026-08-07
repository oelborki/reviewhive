"""record what the critic pass did

Revision ID: d519be77ed56
Revises: c3a71b0e94d2
Create Date: 2026-08-07 10:14:52.331904

Hand-written, for the same reason as c3a71b0e94d2 and fe9d45ab10b6: Alembic does
not compare CHECK constraints, so `--autogenerate` produces an empty migration for
a widening and the generated file looks like there was nothing to do. Trusting it
would ship models emitting 'critic' against a database that still rejects it, and
the failure arrives at write time inside `finish_review` -- after the review has
been produced, printed and paid for, and never stored.

Three changes, one widening and two columns.

`agent_calls.agent` gains critic, so the pass is costed through the same path as
everything else that spends tokens on a review. UNIQUE(review_id, agent) is
untouched and still holds: the critic makes at most one call per review, whatever
the number of findings it judges.

`findings.amended` records that the pass rewrote a finding. It is deliberately not
folded into `sources`, which types who may be credited as a reviewer -- editing a
finding is not authoring one.

`reviews.retracted_count` records how many findings the pass withdrew, and is
deliberately not folded into `suppressed_count`. The two are different claims: a
suppressed finding is one the review stands behind and had no room for, a retracted
one is a claim the pass judged wrong. Summed together, "is the critic deleting too
much" is a question this table could not answer -- which is the whole reason the
count is kept at all.

Both columns are NOT NULL with a server default, so the rows already in the table
get the right answer for a pass that did not exist when they were written.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd519be77ed56'
down_revision: Union[str, Sequence[str], None] = 'c3a71b0e94d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_AGENTS = ("security", "style", "architecture", "intent", "answer", "reconsider", "merge")
NEW_AGENTS = OLD_AGENTS + ("critic",)


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
    op.add_column(
        "findings",
        sa.Column("amended", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "reviews",
        sa.Column("retracted_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Downgrade schema.

    The columns go cleanly. Narrowing the CHECK does not: it fails against rows
    already holding 'critic', so a database that has run the pass cannot be
    downgraded without first deciding what to do with those rows. Deleting them
    would discard the cost history for reviews that are otherwise intact, which is
    a choice for whoever is downgrading and not one to make here.
    """
    op.drop_column("reviews", "retracted_count")
    op.drop_column("findings", "amended")
    _replace_check("agent_calls", "agent", _in("agent", OLD_AGENTS))

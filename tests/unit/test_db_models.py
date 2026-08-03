"""Schema decisions worth pinning.

`importorskip` rather than a `db` marker: these need SQLAlchemy but no running
database, so they should run in a normal offline suite when the extra is
installed and simply not exist when it is not. That keeps `pip install -e .`
enough to run the default tests.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy", reason="requires the db extra")

from reviewhive.db.models import (
    AGENT_NAMES,
    CALL_AGENTS,
    SEVERITIES,
    AgentCallRow,
    Base,
    FindingRow,
    ReviewRow,
)
from reviewhive.models import AgentName, CallAgent, Severity


def _literal_values(annotation) -> set[str]:
    return set(annotation.__args__)


def test_check_constraints_match_the_domain_literals() -> None:
    """The CHECK constraints and the Pydantic `Literal`s are two hand-written
    copies of the same closed set. If they drift, the database rejects a value the
    application considers valid — at write time, mid-review."""
    assert set(AGENT_NAMES) == _literal_values(AgentName)
    assert set(CALL_AGENTS) == _literal_values(CallAgent)
    assert set(SEVERITIES) == _literal_values(Severity)


def test_a_classifier_can_spend_tokens_but_cannot_author_a_finding() -> None:
    """The reason the two sets are separate. `CallAgent` is who can appear in
    `agent_calls`; `AgentName` is who can appear in a finding's `sources`. Merging
    them would let a mention classifier be credited as having found something."""
    assert set(AGENT_NAMES) < set(CALL_AGENTS)
    assert {"intent", "answer", "reconsider"} <= set(CALL_AGENTS)
    assert {"intent", "answer", "reconsider"}.isdisjoint(_literal_values(AgentName))


def test_every_constraint_is_named_by_the_convention() -> None:
    """Server-generated constraint names cannot be dropped by name, which makes a
    downgrade unwritable. The convention only applies to constraints that do not
    carry an explicit name, so this catches a `name=` slipping back in."""
    prefixes = ("pk_", "fk_", "uq_", "ck_", "ix_")
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            assert constraint.name.startswith(prefixes), f"{table.name}: {constraint.name}"
        for index in table.indexes:
            assert index.name.startswith("ix_"), f"{table.name}: {index.name}"


def test_one_call_per_agent_per_review_is_enforced() -> None:
    """The invariant that turns a double-write into an error rather than a
    silently doubled cost figure."""
    unique = {
        tuple(c.name for c in con.columns)
        for con in AgentCallRow.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("review_id", "agent") in unique


def test_findings_are_identified_by_posted_rank() -> None:
    unique = {
        tuple(c.name for c in con.columns)
        for con in FindingRow.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("review_id", "ordinal") in unique


def test_a_delivery_can_only_be_recorded_once() -> None:
    """The actual idempotency guarantee. GitHub redelivers on timeout and has a
    Redeliver button, and two concurrent redeliveries both pass a SELECT — only a
    constraint decides which one wins. The query in the handler is ergonomics; this
    is correctness."""
    unique = {
        tuple(c.name for c in con.columns)
        for con in ReviewRow.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("delivery_id",) in unique


def test_a_review_can_exist_before_its_diff_does() -> None:
    """A webhook answers 202 before fetching the diff, because the fetch is the
    slow call and GitHub's delivery timeout is not. The row is written first, so
    the hash columns have to tolerate not knowing yet."""
    assert ReviewRow.__table__.c.diff_sha256.nullable
    assert ReviewRow.__table__.c.diff_bytes.nullable


def test_cost_columns_are_exact_not_floating_point() -> None:
    """These are summed and compared against a printed figure, so they cannot be
    floats. A hundred-token call is $0.0001 and must not round to zero."""
    for column in (ReviewRow.__table__.c.total_cost_usd, AgentCallRow.__table__.c.cost_usd):
        assert column.type.__class__.__name__ == "Numeric"
        assert column.type.scale >= 8, f"{column.name} would round small calls away"


def test_an_unpriced_call_can_be_recorded_as_unknown() -> None:
    """NULL has to be reachable, or a missing price becomes indistinguishable from
    a free call the moment it is stored."""
    assert AgentCallRow.__table__.c.cost_usd.nullable
    assert not ReviewRow.__table__.c.total_cost_usd.nullable


def test_children_are_removed_with_their_review() -> None:
    for table in (AgentCallRow.__table__, FindingRow.__table__):
        fk = next(iter(table.c.review_id.foreign_keys))
        assert fk.ondelete == "CASCADE", table.name


def test_the_diff_itself_is_not_stored() -> None:
    """Deliberate: unbounded, usually someone else's source, re-fetchable by ref.
    The hash is what answers 'was this the same diff?'."""
    assert "diff_sha256" in ReviewRow.__table__.c
    assert "diff_text" not in ReviewRow.__table__.c

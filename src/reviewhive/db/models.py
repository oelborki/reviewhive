"""Table definitions.

Three tables: a run, its per-agent calls, and the findings it posted.

The row types are deliberately not the Pydantic types. `ReviewResult` is the
graph's output and knows nothing about identity or time; these carry the primary
keys, timestamps and foreign keys a database needs, and `repository.py` maps
between the two explicitly. That mapping is what stops a `model_dump()` from
silently deciding the schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

REVIEW_STATUSES = ("pending", "running", "succeeded", "failed")
REVIEW_SOURCES = ("cli", "webhook", "mention")

# Everything that can spend tokens against a review row, which is wider than the
# three reviewers. Mirrors `models.CallAgent`; `AGENT_NAMES` stays the reviewers
# alone because that set also types who can be credited as a finding's source.
CALL_AGENTS = ("security", "style", "architecture", "intent", "answer", "reconsider", "merge")
AGENT_NAMES = ("security", "style", "architecture")
SEVERITIES = ("high", "medium", "low")

# Money. Eight decimal places because a hundred-token call costs $0.0001 and must
# not round to zero, and Numeric rather than float because these are summed.
MONEY = Numeric(12, 8)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class Base(DeclarativeBase):
    # Without an explicit convention, CHECK and UNIQUE constraints get
    # server-generated names that a downgrade cannot drop by name.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class ReviewRow(Base):
    """One review run.

    `status` and `source` exist before anything writes a value other than
    'succeeded' and 'cli', because they are the two columns that change the shape
    of the write path rather than just the contents of a row. `status` is what
    forces the two-call repository API, and the webhook needs a row to exist
    before the review starts.

    The pull-request columns arrived with the webhook and are all nullable, so a
    CLI row is still a valid row. `diff_sha256` and `diff_bytes` became nullable
    at the same time, which is the one thing here that was not purely additive: a
    webhook must answer 202 before fetching the diff, so the row exists before
    there is a diff to hash.
    """

    __tablename__ = "reviews"

    # Generated client-side so the caller holds the id before the row is durable,
    # which is what lets `start_review` be a single round trip.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)

    # The diff itself is not stored: unbounded in size, usually someone else's
    # source, and re-fetchable by PR ref. The hash still answers "was this the
    # same diff?" in 64 bytes. The cost accepted is that a review cannot be
    # replayed offline from the database alone.
    #
    # Nullable because a webhook row is written before the diff is fetched — the
    # delivery has to be acknowledged inside GitHub's timeout, and the fetch is
    # the slow call. `mark_running` fills both in at the moment the diff exists,
    # so a row never claims a hash it does not have.
    diff_sha256: Mapped[str | None] = mapped_column(String(64))
    diff_bytes: Mapped[int | None] = mapped_column(Integer)
    diff_path: Mapped[str | None] = mapped_column(Text)

    # --- Where the review came from, when it came from a pull request ---
    repo_full_name: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    head_sha: Mapped[str | None] = mapped_column(String(40))

    # GitHub's X-GitHub-Delivery. Typed as an opaque string rather than a UUID:
    # it is a vendor identifier, and giving it a shape is a bet on a format that
    # is not ours to guarantee. UNIQUE is the actual idempotency guarantee —
    # redelivery is routine, two concurrent redeliveries both pass a SELECT, and
    # only a constraint decides which one wins. Postgres ignores NULLs here, so
    # every CLI row coexists.
    delivery_id: Mapped[str | None] = mapped_column(String(64))

    # Proof the review reached the pull request. `posted_review_id IS NULL AND
    # status = 'succeeded'` is exactly "reviewed but never delivered".
    posted_review_id: Mapped[int | None] = mapped_column(BigInteger)
    # How many findings actually went inline. Derivable from findings.line for a
    # normal run, and *not* derivable when the 422 fallback strips the comments —
    # which is also the more interesting question: did the degradation fire?
    posted_comment_count: Mapped[int | None] = mapped_column(SmallInteger)

    # Set when a mention narrowed the run. Stored because a narrowed review is
    # not comparable to a full one, and a cost query that mixes them is wrong.
    focus: Mapped[str | None] = mapped_column(Text)

    suppressed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Stored as the human-readable strings budget.py already builds, e.g.
    # "package-lock.json (lockfile)". Not parsed back into columns: those strings
    # are written for people, and a regex round-trip would silently misclassify
    # the day someone edits a reason. If structure is wanted, add it upstream in
    # BudgetedDiff rather than recovering it here.
    skipped_files: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    truncated_files: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Denormalised on purpose. Computed once at persist time so it stays a
    # snapshot of the prices in force when the run happened; recomputing it later
    # from tokens would silently rewrite history the next time a rate changed.
    total_cost_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))

    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    calls: Mapped[list[AgentCallRow]] = relationship(
        back_populates="review", cascade="all, delete-orphan", order_by="AgentCallRow.agent"
    )
    findings: Mapped[list[FindingRow]] = relationship(
        back_populates="review", cascade="all, delete-orphan", order_by="FindingRow.ordinal"
    )

    __table_args__ = (
        CheckConstraint(_in("status", REVIEW_STATUSES), name="status"),
        CheckConstraint(_in("source", REVIEW_SOURCES), name="source"),
        UniqueConstraint("delivery_id"),
        Index("ix_reviews_created_at", created_at.desc()),
        Index("ix_reviews_diff_sha256", "diff_sha256"),
        # Plain, not unique. A review that failed must be retryable for the same
        # head sha, so this is only there to make the lookup cheap.
        Index(
            "ix_reviews_repo_full_name_pr_number_head_sha",
            "repo_full_name",
            "pr_number",
            "head_sha",
        ),
    )


class AgentCallRow(Base):
    """One Anthropic request made on behalf of a review."""

    __tablename__ = "agent_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )

    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings_returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    # NULL means the model was not in the price table, which is a different fact
    # from a call that cost nothing. SUM ignores it; WHERE cost_usd IS NULL finds
    # every gap.
    cost_usd: Mapped[Decimal | None] = mapped_column(MONEY)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    review: Mapped[ReviewRow] = relationship(back_populates="calls")

    __table_args__ = (
        CheckConstraint(_in("agent", CALL_AGENTS), name="agent"),
        # One call per agent per run is a real invariant of the current graph, and
        # `agent` is a closed set, so this doubles as the ordering key. It turns a
        # double-write into a loud error instead of a silently doubled cost. If
        # retries ever land, drop this and add an `attempt` column.
        UniqueConstraint("review_id", "agent"),
        Index("ix_agent_calls_review_id", "review_id"),
    )


class FindingRow(Base):
    """One posted finding.

    Posted, not produced: `rank_and_cut` withholds anything under the confidence
    floor, anything under the severity floor, and anything past the cap, and only a
    count of those survives into `ReviewResult`. This
    table therefore holds what a reader would have seen, which is also what
    Phase 3 needs when it records whether a finding went inline or into the
    summary.
    """

    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )

    # Posted rank. `ReviewResult.findings` is ordered by severity, confidence and
    # agreement, and has no natural key, so position is the only stable identity.
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    file: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)

    # A Postgres array rather than JSONB: it is a short list of closed-set strings
    # and `'security' = ANY(sources)` is the query anyone would actually write.
    sources: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)

    review: Mapped[ReviewRow] = relationship(back_populates="findings")

    __table_args__ = (
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        UniqueConstraint("review_id", "ordinal"),
        Index("ix_findings_review_id", "review_id"),
    )

"""Merging overlapping findings from independent agents.

Two agents flagging the same line is the common case — security and architecture
both dislike the same overloaded function. This module is the deterministic half:
pure functions, no I/O, no model calls, so its behaviour is pinned by tests rather
than by vibes.

A `MergedFinding` with a single source goes in and a `MergedFinding` with one or
more sources comes out, so the graph carries exactly one finding type end to end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from reviewhive.models import SEVERITY_RANK, AgentName, MergedFinding, Severity

# Grammatical filler only. Deliberately excludes domain nouns (function, class,
# variable, value) and verbs (use, used): stripping those makes distinct findings
# collide — "Function does too much" and "Class does too much" would both reduce to
# {much} and merge. Negations are kept for the same reason.
STOPWORDS = frozenset(
    [
        # determiners
        "a", "an", "the", "this", "that", "these", "those",
        # copulas
        "is", "are", "was", "were", "be", "been", "being",
        # prepositions and conjunctions
        "in", "on", "at", "to", "for", "of", "with", "by", "from", "as", "and", "or",
        # pronouns and modals
        "it", "its", "should", "could", "would", "may", "might", "can",
        # interrogatives and connectives
        "here", "there", "when", "where", "which", "what", "while", "if", "then", "than",
    ]
)

_WORD = re.compile(r"[a-z0-9_]+")

# Models spell the same compound both ways in a single review — "Hardcoded API
# token" from one agent, "Hard-coded API token" from another. Splitting on the
# hyphen turns one shared token into two unshared ones, which cost that observed
# pair enough overlap (0.375) to miss the 0.5 threshold. Joining the halves
# instead makes the two spellings identical. Only an intra-word hyphen is
# removed, so a spaced dash used as punctuation still separates words.
_INTRAWORD_HYPHEN = re.compile(r"(?<=[a-z0-9])-(?=[a-z0-9])")


@dataclass
class _Cluster:
    """A group of findings believed to describe the same issue."""

    path: str
    members: list[MergedFinding] = field(default_factory=list)

    @property
    def is_file_level(self) -> bool:
        return all(m.line is None for m in self.members)

    @property
    def lines(self) -> list[int]:
        return [m.line for m in self.members if m.line is not None]


def normalize_path(path: str) -> str:
    """Make paths comparable when a model echoes back `a/src/x.py` or `./src/x.py`."""
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith(("a/", "b/")):
        cleaned = cleaned[2:]
    return cleaned


def title_tokens(title: str) -> frozenset[str]:
    joined = _INTRAWORD_HYPHEN.sub("", title.lower())
    return frozenset(w for w in _WORD.findall(joined) if w not in STOPWORDS)


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def collapse(
    findings: list[MergedFinding],
    *,
    line_tolerance: int = 3,
    title_similarity: float = 0.5,
) -> list[MergedFinding]:
    """Group near-identical findings, keeping one representative per group.

    Greedy single-pass clustering. Two findings join the same cluster when they
    share a file, sit within `line_tolerance` lines of each other, and their titles
    overlap by at least `title_similarity`. File-level findings (`line is None`)
    only ever merge with other file-level findings for the same file — with no
    position to compare, proximity says nothing.
    """
    clusters: list[_Cluster] = []

    for finding in findings:
        normalized = finding.model_copy(update={"file": normalize_path(finding.file)})
        tokens = title_tokens(normalized.title)

        target = next(
            (
                cluster
                for cluster in clusters
                if cluster.path == normalized.file
                and _lines_compatible(cluster, normalized.line, line_tolerance)
                and _titles_match(cluster, tokens, title_similarity)
            ),
            None,
        )

        if target is None:
            clusters.append(_Cluster(path=normalized.file, members=[normalized]))
        else:
            target.members.append(normalized)

    return [_merge(cluster) for cluster in clusters]


def _lines_compatible(cluster: _Cluster, line: int | None, tolerance: int) -> bool:
    if line is None:
        return cluster.is_file_level
    return any(abs(line - existing) <= tolerance for existing in cluster.lines)


def _titles_match(cluster: _Cluster, tokens: frozenset[str], threshold: float) -> bool:
    return any(
        jaccard(title_tokens(member.title), tokens) >= threshold for member in cluster.members
    )


def _merge(cluster: _Cluster) -> MergedFinding:
    """Pick a representative and union the provenance.

    The representative is the most severe member, breaking ties on confidence —
    the sharpest statement of the issue survives. `sources` records who raised it
    as provenance for the reader, not as evidence: see the note on confidence
    below.
    """
    if len(cluster.members) == 1:
        return cluster.members[0]

    representative = max(
        cluster.members,
        key=lambda m: (SEVERITY_RANK[m.severity], m.confidence),
    )

    sources: list[AgentName] = []
    for member in cluster.members:
        for source in member.sources:
            if source not in sources:
                sources.append(source)

    severity: Severity = max((m.severity for m in cluster.members), key=lambda s: SEVERITY_RANK[s])

    # Confidence is not raised by agreement. Doing so would treat the agents as
    # independent observers, and single-agent probes show they are not: they
    # converge on whatever defect is most salient in the diff, so a second report
    # is usually the same observation restated rather than corroboration. Worse,
    # a reviewer straying outside its specialty produces exactly that shape, which
    # would let a scope violation promote the finding it duplicates.
    confidence = max(m.confidence for m in cluster.members)

    return MergedFinding(
        file=cluster.path,
        line=_representative_line(cluster),
        severity=severity,
        category=representative.category,
        title=representative.title,
        body=representative.body,
        confidence=confidence,
        sources=sources,
    )


def _representative_line(cluster: _Cluster) -> int | None:
    """The earliest reported line — anchoring at the start of a problem region
    reads better than anchoring in the middle of it."""
    lines = cluster.lines
    return min(lines) if lines else None


def rank_and_cut(
    findings: list[MergedFinding],
    *,
    min_confidence: float,
    max_posted: int,
) -> tuple[list[MergedFinding], int]:
    """Order findings for posting and enforce the per-PR cap.

    Returns the findings to post and how many were dropped, so the summary can say
    "and 4 more" rather than silently hiding them.
    """
    # Confidence outranks agreement. A second reviewer raising the same issue is
    # weak evidence at best — the agents converge on salient defects rather than
    # observing independently — so it breaks ties between equally confident
    # findings rather than overriding a more confident one.
    surviving = [f for f in findings if f.confidence >= min_confidence]
    surviving.sort(
        key=lambda f: (
            -SEVERITY_RANK[f.severity],
            -f.confidence,
            -f.agreement,
            f.file,
            f.line if f.line is not None else 0,
        )
    )

    if len(surviving) <= max_posted:
        return surviving, 0
    return surviving[:max_posted], len(surviving) - max_posted

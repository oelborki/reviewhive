"""The critic evaluation set, checked for the things that would make it lie.

`probe_critic.py` scores a prompt against `critic_cases.json`. None of that means
anything if the fixture asks an unfair question, and there are three ways it can:
a case whose finding does not sit on the diff it names, a case whose window does not
contain the evidence its expectation depends on, and a set weighted so that a critic
which deletes everything scores well.

This project has already paid for the first kind of failure three times — twice in
`probe_merge.py` and once in `probe_security_scope.py`, where a *passing* run was
reported as a failure. A probe that cannot prove it asked the right question is
worse than no probe, so the question is pinned here, offline, where it costs
nothing.
"""

from __future__ import annotations

import json

import pytest
from tests.conftest import FIXTURES

from reviewhive.config import Settings
from reviewhive.diff.parser import parse_diff
from reviewhive.models import MergedFinding

CASES = json.loads((FIXTURES / "critic_cases.json").read_text(encoding="utf-8"))["cases"]
RADIUS = Settings().critic_context_radius

# What each case's window has to contain for its expectation to be answerable. Only
# the cases whose verdict turns on a specific line need an entry: a `survives` case
# asserting a real defect is answerable from any window that shows the defect.
EVIDENCE = {
    # The whitelist that makes the interpolation safe. Without it in view, calling
    # the finding poached is a guess.
    "pr5_round2/db.py:49": ["SORT_DIRECTIONS", "direction not in"],
    # The first clause of the condition, which is the half every false positive
    # here fails to read.
    "sound_auth/main.py:36": ["if not expected or not"],
    "sound_auth/main.py:34": ["if not expected or not"],
    # Two live XSS holes four lines apart. Each window shows both, which is the
    # trap: a critic keying on the title sees a duplicate it has already judged.
    "demo_pr/reports.py:40": ["owner", "title"],
    "demo_pr/reports.py:44": ["owner", "title"],
}


def _window(case: dict) -> str:
    parsed = parse_diff((FIXTURES / "diffs" / case["fixture"]).read_text(encoding="utf-8"))
    matches = [f for f in parsed.files if f.path == case["finding"]["file"]]
    assert matches, f"{case['id']}: {case['finding']['file']} is not in {case['fixture']}"
    return matches[0].window(case["finding"]["line"], radius=RADIUS)


def test_the_set_is_not_empty() -> None:
    assert CASES


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_case_is_a_valid_finding(case) -> None:
    """The fixture holds what the pipeline carries. A case that cannot be built into
    a `MergedFinding` would be scored against a shape the critic never sees."""
    finding = MergedFinding(**case["finding"])

    assert case["expect"] in {"survives", "loses_high"}
    assert case["why"].strip(), f"{case['id']} has no stated reason"
    assert finding.sources, f"{case['id']} names no lane, and the lane is the signal"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_finding_anchors_to_a_real_line_in_its_fixture(case) -> None:
    """A finding whose line is not in the diff would reach the critic with an empty
    window, and every verdict on it would be about nothing."""
    window = _window(case)

    assert window, f"{case['id']}: no window at line {case['finding']['line']}"


def test_every_evidence_entry_names_a_real_case() -> None:
    """Without this, a typo in an `EVIDENCE` key silently drops a fairness check
    and the suite still passes. The test below parametrizes over `EVIDENCE`, so a
    key that matches nothing would simply not be asserted about."""
    known = {case["id"] for case in CASES}

    assert set(EVIDENCE) <= known, f"unknown case ids: {set(EVIDENCE) - known}"


@pytest.mark.parametrize("case_id", sorted(EVIDENCE), ids=lambda i: i)
def test_the_window_contains_the_evidence_the_verdict_needs(case_id) -> None:
    """The fairness check. `loses_high` says the critic should have known better,
    and that is only true if the thing it should have known is in front of it.

    Parametrized over `EVIDENCE` rather than over every case and skipped where it
    does not apply: CI fails the offline job on *any* skip, because a skip there
    normally means an extra is missing and half the suite quietly did not run. A
    deliberate skip would spend that tripwire.
    """
    case = next(c for c in CASES if c["id"] == case_id)
    window = _window(case)

    for fragment in EVIDENCE[case_id]:
        assert fragment in window, f"{case_id} window is missing {fragment!r}"


def test_deleting_everything_scores_badly() -> None:
    """The balance is the design. `probe_merge.py` records the same rule for merges:
    a set of positives alone tunes the pass toward merging everything. Here a critic
    that drops every finding must fail most of the set, or the score rewards exactly
    the behaviour the pass is most dangerous for."""
    survives = [c for c in CASES if c["expect"] == "survives"]

    assert len(survives) > len(CASES) - len(survives), (
        "cases that must survive have to outnumber the rest, or a critic that "
        "deletes everything scores at least half"
    )


def test_the_poached_pair_is_present_on_both_sides() -> None:
    """D12b is the reason this pass exists, and it is only measured if both halves
    are in the set. Scoring the poached finding alone would let a critic pass by
    lowering every severity it sees."""
    ids = {c["id"]: c["expect"] for c in CASES}

    assert ids.get("pr5_round2/db.py:49") == "loses_high"
    assert ids.get("pr5_round2/db.py:57") == "survives"

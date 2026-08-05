"""Measure how the mention classifier reads real comment phrasings.

The offline suite pins the plumbing around the classifier — when it is skipped,
what happens when it fails, which source wins on a threaded reply — and says
nothing about whether it reads a comment correctly. That is a prompt question, and
this is how to answer it.

    python scripts/probe_intent.py                 # the built-in phrasing set
    python scripts/probe_intent.py --only ambiguous
    python scripts/probe_intent.py "/reviewhive why does this matter?"

Each case carries the action it should produce, so a run scores itself. The point
is not a perfect score — several cases are genuinely ambiguous and are marked so —
but to see *which* readings drift when the prompt changes.

Costs about **$0.03** for the full set — measured, not estimated. Each call carries
the findings list as well as the comment, and there are seventeen of them, so it is
comparable to one real review rather than to a fraction of one. A single group
(`--only ambiguous`) is under a cent.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from reviewhive.config import get_settings
from reviewhive.mentions.intent import PriorFinding, classify
from reviewhive.pricing import cost_of

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# Stands in for a review the bot already posted, so `reconsider` has something to
# point at and the ordinals in a comment mean something.
FINDINGS = [
    PriorFinding(0, "app/db.py", 56, "high", "SQL injection in search_tasks"),
    PriorFinding(1, "app/auth.py", 5, "high", "API secret hardcoded in source"),
    PriorFinding(2, "app/auth.py", 11, "high", "Secret compared with =="),
    PriorFinding(3, "app/main.py", 43, "medium", "done endpoint lacks authentication"),
]


@dataclass(frozen=True)
class Case:
    comment: str
    expect: str
    group: str
    note: str = ""


CASES = [
    # --- plainly asking for the whole thing again ---
    Case("/reviewhive", "full_review", "bare", "short-circuits, no call"),
    Case("/reviewhive take another look", "full_review", "full"),
    Case("/reviewhive re-review please", "full_review", "full"),
    Case("/reviewhive I've pushed fixes, can you check again", "full_review", "full"),
    # --- asking again, but narrowed ---
    Case("/reviewhive check this again but focus on error handling", "focused_review", "focused"),
    Case("/reviewhive just look at the auth changes", "focused_review", "focused"),
    Case("/reviewhive can you re-check the SQL stuff specifically", "focused_review", "focused"),
    # --- asking something answerable ---
    Case("/reviewhive why is this a problem?", "answer_question", "question"),
    Case("/reviewhive what would you use instead?", "answer_question", "question"),
    Case("/reviewhive does this apply to the other handler too?", "answer_question", "question"),
    Case("/reviewhive how would I fix finding 2?", "answer_question", "question"),
    # --- disputing something already filed ---
    Case(
        "/reviewhive this is intentional, the input is validated upstream",
        "reconsider",
        "dispute",
    ),
    Case("/reviewhive the caller already holds the lock, so this is fine", "reconsider", "dispute"),
    Case("/reviewhive I disagree with finding 1, that's a test fixture", "reconsider", "dispute"),
    # --- genuinely ambiguous: must land on answer_question, the cheap wrong ---
    Case("/reviewhive thoughts?", "answer_question", "ambiguous", "vague but has text"),
    Case("/reviewhive hmm", "answer_question", "ambiguous", "a word, so not bare"),
    Case("/reviewhive not sure about this one", "answer_question", "ambiguous"),
    Case("/reviewhive anything else?", "answer_question", "ambiguous", "could read as re-review"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("comment", nargs="*", help="probe one comment instead of the set")
    parser.add_argument(
        "--only",
        help="restrict to one group: bare, full, focused, question, dispute, ambiguous",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set.")

    cases = (
        [Case(" ".join(args.comment), "?", "adhoc")]
        if args.comment
        else [c for c in CASES if not args.only or c.group == args.only]
    )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    hits = misses = 0
    spend = 0.0

    try:
        for case in cases:
            result, call = await classify(
                client, settings, comment=case.comment, findings=FINDINGS
            )
            if call is not None:
                cost = cost_of(call)
                spend += float(cost) if cost else 0.0

            ok = case.expect in ("?", result.action)
            if case.expect != "?":
                hits, misses = (hits + 1, misses) if ok else (hits, misses + 1)
            mark = f"{GREEN}ok  {RESET}" if ok else f"{RED}MISS{RESET}"

            print(f"\n{mark} {case.comment}")
            if not ok:
                print(f"     expected {case.expect}, got {YELLOW}{result.action}{RESET}")
            else:
                print(f"     {DIM}-> {result.action}{RESET}")
            for label, value in (
                ("focus", result.focus),
                ("target", result.target_ordinal),
                ("question", result.question),
            ):
                if value is not None:
                    print(f"     {DIM}{label}: {value}{RESET}")
            print(f"     {DIM}why: {result.rationale}{RESET}")
            if case.note:
                print(f"     {DIM}({case.note}){RESET}")
    finally:
        await client.close()

    total = hits + misses
    if total:
        print(f"\n{hits}/{total} as expected")
    print(f"{DIM}spend: ${spend:.4f}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())

"""Measure how the mention responder answers real questions.

The offline suite checks what context the model is handed and that a failure
produces no reply. Whether the reply is *good* — direct, specific, and free of new
accusations — is a prompt question, and this is how to answer it.

    python scripts/probe_mention.py
    python scripts/probe_mention.py --only scope
    python scripts/probe_mention.py "does this apply to the html branch too?"

The scope cases matter most. The tempting failure for this prompt is not refusing
to answer — it is answering and then continuing into a fresh review, which reads as
helpfulness and is the thing the boundary exists to stop. Those cases ask questions
whose honest answer sits next to a defect the reviewers did not report.

Costs about a cent per question: the diff dominates, and it is one call rather
than three.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from anthropic import AsyncAnthropic

from reviewhive.config import get_settings
from reviewhive.mentions.intent import PriorFinding
from reviewhive.mentions.respond import answer_question
from reviewhive.pricing import cost_of

DIM, BOLD, YELLOW, RESET = "\033[2m", "\033[1m", "\033[33m", "\033[0m"

# The diff the first live webhook review actually ran against, so the findings
# below are the ones that review actually produced. A probe whose findings do not
# correspond to its diff measures nothing.
DIFF_PATH = Path("tests/fixtures/diffs/demo_pr.diff")

# All fifteen findings that review posted, verbatim from the `findings` table.
# Deliberately not trimmed: a shortened list makes the responder look like it is
# overstepping when it names a defect that was in fact already reported, which is
# an artefact of the probe rather than a fact about the prompt.
FINDINGS = [
    PriorFinding(0, "app/db.py", 56, "high",
        "SQL injection via string interpolation in search_tasks",
    ),
    PriorFinding(1, "app/db.py", 62, "high", "SQL injection via string concatenation in mark"),
    PriorFinding(2, "app/reports.py", 19, "high",
        "SQL injection via string concatenation in build_and_send_report",
    ),
    PriorFinding(3, "app/auth.py", 5, "high", "API secret hardcoded in source"),
    PriorFinding(4, "app/auth.py", 7, "high", "Admin tokens hardcoded in source"),
    PriorFinding(5, "app/main.py", 43, "high",
        "POST /tasks/{owner}/{id}/done endpoint lacks authentication",
    ),
    PriorFinding(6, "app/main.py", 44, "high", "Missing authorization check in done endpoint"),
    PriorFinding(7, "app/reports.py", 13, "high", "SMTP password hardcoded in source"),
    PriorFinding(8, "app/auth.py", 11, "high", "Secret comparison using equality operator"),
    PriorFinding(9, "app/reports.py", 1, "high",
        "reports.py duplicates database connection logic already in db module",
    ),
    PriorFinding(10, "app/reports.py", 16, "high",
        "build_and_send_report conflates data fetching, formatting, and email transmission",
    ),
    PriorFinding(11, "app/reports.py", 40, "high", "HTML injection in report body"),
    PriorFinding(12, "app/reports.py", 44, "high", "HTML injection in report body"),
    PriorFinding(13, "app/main.py", 36, "medium", "No ownership check in search endpoint"),
    PriorFinding(14, "app/main.py", 49, "medium", "Admin endpoint uses weak condition check"),
]


@dataclass(frozen=True)
class Case:
    question: str
    group: str
    target: int | None = None
    watch: str = ""


CASES = [
    # --- ordinary explanation ---
    Case("why is this a problem?", "explain", target=0),
    Case("what would you use instead?", "explain", target=0),
    Case("how do I fix the == comparison?", "explain", target=8),
    # --- questions whose answer is not in the diff ---
    Case(
        "is owner validated before it gets here?",
        "unknown",
        target=0,
        watch="should say it cannot tell from a diff, not guess",
    ),
    Case(
        "does the framework escape this automatically?",
        "unknown",
        watch="should decline to assume",
    ),
    # --- the boundary: an honest answer sits next to unreported defects ---
    Case(
        "is this the only injection in the file?",
        "scope",
        target=0,
        watch="must not file the other injections as new findings",
    ),
    Case(
        "anything else wrong with auth.py?",
        "scope",
        watch="must answer about what was filed, not open a new review",
    ),
    Case(
        "while you're here, is the rest of this file ok?",
        "scope",
        watch="an explicit invitation to overstep",
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("question", nargs="*", help="probe one question instead of the set")
    parser.add_argument("--only", help="restrict to a group: explain, unknown, scope")
    parser.add_argument("--diff", type=Path, default=DIFF_PATH)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set.")
    if not args.diff.is_file():
        sys.exit(f"no such diff: {args.diff}")

    diff_text = args.diff.read_text(encoding="utf-8")
    cases = (
        [Case(" ".join(args.question), "adhoc")]
        if args.question
        else [c for c in CASES if not args.only or c.group == args.only]
    )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    spend = 0.0

    try:
        for case in cases:
            target = FINDINGS[case.target] if case.target is not None else None
            reply, call = await answer_question(
                client,
                settings,
                question=case.question,
                diff_text=diff_text,
                findings=FINDINGS,
                target=target,
            )
            cost = cost_of(call)
            spend += float(cost) if cost else 0.0

            print(f"\n{BOLD}[{case.group}] {case.question}{RESET}")
            if target:
                print(f"{DIM}  about: {target.render()}{RESET}")
            if case.watch:
                print(f"{YELLOW}  watch: {case.watch}{RESET}")
            if reply is None:
                print(f"  {YELLOW}no reply ({call.error}){RESET}")
                continue
            for line in reply.splitlines():
                print(textwrap.fill(line, 88, initial_indent="  ", subsequent_indent="  "))
            print(f"{DIM}  ({call.output_tokens} output tokens){RESET}")
    finally:
        await client.close()

    print(f"\n{DIM}spend: ${spend:.4f}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())

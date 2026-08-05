"""Measure the merge prompt against pairs that were actually recorded.

`probe_agent.py` is to the reviewer prompts what this is to `merge.md`. The test
suite is offline, so it pins which pairs get asked about and what happens to the
answers, and says nothing about how the question lands:

    python scripts/probe_merge.py
    python scripts/probe_merge.py --only style_only

One call, all nine pairs, roughly a tenth of a cent. It scores itself against
`tests/fixtures/merge_pairs.json`, which holds ten pairs taken from the `findings`
table rather than written to suit the prompt — a fixture invented for the prompt
measures the prompt against itself.

**Read the two columns separately.** A false merge deletes a finding and a missed
merge only repeats one, so a run that merges everything scores 5/9 and is worse
than useless. The score to watch is the one on pairs that must *not* merge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from anthropic import AsyncAnthropic

from reviewhive.config import get_settings
from reviewhive.graph.llm_merge import candidate_pairs, merge_findings
from reviewhive.logging_setup import configure_logging
from reviewhive.models import MergedFinding
from reviewhive.pricing import cost_of

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "merge_pairs.json"

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
DIM = "\033[2m"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="Substring of a case id to run alone")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show pipeline logging")
    return parser.parse_args()


def load_cases(only: str | None) -> list[dict]:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    # Pairs filtered before the prompt cannot say anything about it. Scoring them
    # would credit the prompt for a decision candidate selection already made.
    cases = [case for case in cases if case["reaches_model"]]
    if only:
        cases = [case for case in cases if only in case["id"]]
    return cases


async def run(cases: list[dict]) -> tuple[list[tuple[dict, bool]], object]:
    settings = get_settings()
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")

    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.agent_timeout_seconds,
        max_retries=settings.agent_max_retries,
    )

    # One call for all of them, because that is what a review does — asking one
    # pair at a time would measure a different prompt.
    #
    # Each case gets its own directory prefix first. Several cases come from the
    # same fixture file at the same line, so without this they pair *across*
    # cases: findings from unrelated cases become candidates, the cap truncates
    # the real pairs, and the run scores decisions nobody was asked to make. That
    # is not hypothetical — it happened on the first run of this script.
    findings: list[MergedFinding] = []
    expected: list[tuple[dict, int, int, str]] = []
    for number, case in enumerate(cases):
        path = f"case{number}/{case['left']['file']}"
        left = len(findings)
        findings.append(MergedFinding(**{**case["left"], "file": path}))
        findings.append(MergedFinding(**{**case["right"], "file": path}))
        expected.append((case, left, left + 1, path))

    asked = {
        (p.left, p.right)
        for p in candidate_pairs(
            findings, window=settings.merge_line_window, max_pairs=len(findings) ** 2
        )
    }
    stray = len(asked) - len(expected)
    missing = [case["id"] for case, left, right, _ in expected if (left, right) not in asked]
    if missing or stray:
        # Silence here would look like a passing run. A pair that never reached
        # the model cannot be scored, and pretending otherwise is the probe
        # fidelity failure this project has already paid for once.
        print(f"{RED}probe is not measuring what it claims:{RESET}")
        if missing:
            print(f"{RED}  never asked about: {', '.join(missing)}{RESET}")
        if stray:
            print(f"{RED}  {stray} pair(s) asked about that no case describes{RESET}")
        print()

    merged, call = await merge_findings(client, settings, findings)
    await client.close()

    # A pair merged if its two findings ended up in one result — what the review
    # would actually show, rather than what the model said about it. The path
    # prefix makes this exact: only one case can own a given file.
    results = []
    for case, left, right, path in expected:
        both = set(findings[left].sources) | set(findings[right].sources)
        together = any(f.file == path and set(f.sources) >= both for f in merged)
        results.append((case, together))

    return results, call


def report(results: list[tuple[dict, bool]], call) -> None:
    should = [(c, got) for c, got in results if c["should_merge"]]
    should_not = [(c, got) for c, got in results if not c["should_merge"]]

    for label, group, want in (
        ("must merge", should, True),
        ("must NOT merge", should_not, False),
    ):
        print(f"{DIM}{label}{RESET}")
        for case, got in group:
            ok = got is want
            mark = f"{GREEN}pass{RESET}" if ok else f"{RED}FAIL{RESET}"
            print(f"  {mark}  {case['id']}")
            if not ok:
                print(f"        {DIM}{case['why']}{RESET}")
        correct = sum(1 for case, got in group if got is want)
        print(f"  {correct}/{len(group)}\n")

    false_merges = sum(1 for case, got in should_not if got)
    if false_merges:
        print(
            f"{RED}{false_merges} false merge(s). Each one discards a real finding "
            f"that nobody will ever see.{RESET}"
        )

    if call is not None:
        price = cost_of(call)
        print(
            f"{DIM}{call.latency_ms}ms  {call.input_tokens} in / {call.output_tokens} out"
            + (f"  ${price:.4f}" if price is not None else "")
            + (f"  ERROR: {call.error}" if call.error else "")
            + RESET
        )


def main() -> None:
    args = parse_args()
    configure_logging("INFO" if args.verbose else "WARNING")

    cases = load_cases(args.only)
    if not cases:
        sys.exit("No cases matched.")

    results, call = asyncio.run(run(cases))
    report(results, call)


if __name__ == "__main__":
    main()

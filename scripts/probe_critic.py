"""Measure the critic prompt against findings that were actually filed.

`probe_merge.py` is to `merge.md` what this is to `critic.md`. The test suite is
offline, so it pins which findings get asked about and what happens to the answers,
and says nothing about how the question lands:

    python scripts/probe_critic.py
    python scripts/probe_critic.py --only db.py --show-reasons

One call, all ten cases, roughly a fifth of a cent. It scores itself against
`tests/fixtures/critic_cases.json`, which holds findings taken from the `findings`
table and from a captured agent run rather than written to suit the prompt -- a
fixture invented for a prompt measures the prompt against itself.

**Read the two columns separately, and read `survives` first.** A finding left
standing at the wrong severity is noise a reader dismisses; a finding deleted is
gone, with nothing in the posted review to say it existed. A critic that drops
everything scores 3/10 here and is worse than no critic at all, so the number that
matters is the one on findings that must come through untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from anthropic import AsyncAnthropic

from reviewhive.config import get_settings
from reviewhive.diff.parser import DiffFile, parse_diff
from reviewhive.graph.critic import judgeable, review_findings
from reviewhive.logging_setup import configure_logging
from reviewhive.models import MergedFinding
from reviewhive.pricing import cost_of

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "critic_cases.json"
DIFFS = ROOT / "tests" / "fixtures" / "diffs"

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
DIM = "\033[2m"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="Substring of a case id to run alone")
    parser.add_argument(
        "--show-reasons", action="store_true", help="Print the critic's reason for every case"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show pipeline logging")
    return parser.parse_args()


def load_cases(only: str | None) -> list[dict]:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    if only:
        cases = [case for case in cases if only in case["id"]]
    return cases


def build(cases: list[dict]) -> tuple[list[MergedFinding], list[DiffFile]]:
    """One finding per case, and one diff file per (fixture, path) pair.

    **Prefixed by fixture, not by case.** Three fixtures contain a file called
    `app/main.py` and two cases sit on the same line of one, so without a prefix
    they resolve to each other's code and the run scores verdicts about the wrong
    thing. `probe_merge.py` learned that the expensive way.

    But prefixing by *case* is wrong in the other direction, and it flattered the
    score by 13/13 the first time it was tried: the pass asks one question per file,
    so a unique prefix per case gives every finding a private call, which is the
    isolated condition and not the one a review runs. Findings that would share a
    file in production have to share one here, or the probe measures a pipeline
    nobody uses.
    """
    findings: list[MergedFinding] = []
    files: dict[str, DiffFile] = {}

    for case in cases:
        raw = case["finding"]
        prefix = Path(case["fixture"]).stem
        path = f"{prefix}/{raw['file']}"

        if path not in files:
            parsed = parse_diff((DIFFS / case["fixture"]).read_text(encoding="utf-8"))
            source = next((f for f in parsed.files if f.path == raw["file"]), None)
            if source is None:
                sys.exit(f"{case['id']}: {raw['file']} is not in {case['fixture']}")
            files[path] = replace(source, path=path)

        findings.append(MergedFinding(**{**raw, "file": path}))

    return findings, list(files.values())


def check_fidelity(cases, findings, files, settings) -> bool:
    """Prove the critic was asked about exactly the cases this run claims to score.

    A probe that cannot prove it asked the right question is worse than no probe.
    Three instances on record, one of which scored 1/4 and 4/4 on the same prompt.
    """
    judged = judgeable(findings, files, max_findings=len(findings) + 1)

    missing = [cases[i]["id"] for i in range(len(cases)) if i not in judged]
    stray = [i for i in judged if i >= len(cases)]
    if not missing and not stray:
        return True

    print(f"{RED}probe is not measuring what it claims:{RESET}")
    if missing:
        print(f"{RED}  never asked about: {', '.join(missing)}{RESET}")
    if stray:
        print(f"{RED}  {len(stray)} finding(s) asked about that no case describes{RESET}")
    print()
    return False


async def run(cases: list[dict]):
    settings = get_settings()
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")

    findings, files = build(cases)
    if not check_fidelity(cases, findings, files, settings):
        sys.exit(1)

    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.agent_timeout_seconds,
        max_retries=settings.agent_max_retries,
    )
    # One call for all of them, because that is what a review does. Asking one
    # finding at a time would measure a different prompt.
    outcome = await review_findings(client, settings, findings, files)
    await client.close()

    # Straight from the pass, keyed by the index each finding went in under. Matching
    # by file path scored two cases against one finding the moment they shared a
    # file, and matching on any field an amendment can rewrite has the same problem.
    results = [
        (case, outcome.survivors.get(index, findings[index]), outcome.honoured.get(index))
        for index, case in enumerate(cases)
    ]

    return results, outcome


def passed(case: dict, survivor: MergedFinding | None) -> bool:
    """What the review would actually show, rather than what the model said.

    - `survives`: not dropped and not downgraded. Rewriting a title or body is
      allowed — those are the amendments the pass exists to make, and none of them
      destroys anything.
    - `loses_high`: dropped, or kept below high. Which of the two is right is left
      open on purpose; for a claim that overstates a real weakness, lowering it is
      the better answer and deleting it is also defensible.
    - `refuted`: the claim is false and must not stand as written. Either an
      amendment or a withdrawal passes; leaving it untouched does not.
    """
    original = case["finding"]["severity"]
    if case["expect"] == "survives":
        return survivor is not None and survivor.severity == original
    if case["expect"] == "refuted":
        return survivor is None or survivor.amended
    return survivor is None or survivor.severity != "high"


def describe(case: dict, survivor: MergedFinding | None) -> str:
    if survivor is None:
        return "withdrawn"
    original = case["finding"]
    if survivor.severity != original["severity"]:
        return f"{original['severity']} -> {survivor.severity}"
    if survivor.amended:
        return "rewritten, same severity"
    return "unchanged"


def report(results, outcome, show_reasons: bool) -> None:
    groups = (
        ("must survive untouched", [r for r in results if r[0]["expect"] == "survives"]),
        ("must not stand at high", [r for r in results if r[0]["expect"] == "loses_high"]),
        ("must not stand as written", [r for r in results if r[0]["expect"] == "refuted"]),
    )

    for label, group in groups:
        if not group:
            continue
        print(f"{DIM}{label}{RESET}")
        for case, survivor, verdict in group:
            ok = passed(case, survivor)
            mark = f"{GREEN}pass{RESET}" if ok else f"{RED}FAIL{RESET}"
            print(f"  {mark}  {case['id']}  {DIM}{describe(case, survivor)}{RESET}")
            if not ok:
                print(f"        {DIM}{case['why']}{RESET}")
            if (show_reasons or not ok) and verdict is not None:
                print(f"        {DIM}critic: {verdict.reason}{RESET}")
        correct = sum(1 for case, survivor, _ in group if passed(case, survivor))
        print(f"  {correct}/{len(group)}\n")

    destroyed = [
        case["id"]
        for case, survivor, _ in results
        if case["expect"] == "survives" and not passed(case, survivor)
    ]
    if destroyed:
        print(
            f"{RED}{len(destroyed)} finding(s) the reviewers were right about were "
            f"deleted or downgraded. Each one is invisible to the reader.{RESET}"
        )

    call = outcome.call
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

    results, outcome = asyncio.run(run(cases))
    report(results, outcome, args.show_reasons)


if __name__ == "__main__":
    main()

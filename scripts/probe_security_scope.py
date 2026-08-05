"""Two security-reviewer regressions, each scored, each run several times.

The test suite cannot answer either question here: it is offline by design, so it
pins the deterministic pipeline and says nothing about what a prompt elicits. Both
cases below were real failures on live pull requests, and both are probabilistic —
a single run proves nothing in either direction, which is why the default is three.

    python scripts/probe_security_scope.py                  # both cases, 3 runs
    python scripts/probe_security_scope.py --only sound_auth --runs 5

**missing_auth** — an endpoint with no authentication at all went unreported across
three live runs, while the reviewer *did* flag a timing-unsafe `==` on a sibling
endpoint six lines away in the same hunk. The full body of the ungated handler was
in the diff, so this was never a visibility problem: the auth enumeration simply had
no bullet for an absent mechanism, only for weak or disabled ones. The gated sibling
is in the fixture on purpose — it is the control. If the gated one is flagged and the
ungated one is not, the gap is the enumeration and nothing else.

**sound_auth** — the reviewer filed "empty default allows API key bypass" at high
severity against a check that fails closed, quoting the expression correctly and
evaluating it backwards. `not expected` is true when the variable is unset, so the
`or` short-circuits and the request is rejected. What is scored here is not whether
the model comments on the code — advising "fail at startup instead" is defensible —
but whether it calls correct code a *high* severity vulnerability.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import AsyncAnthropic

from reviewhive.agents.base import load_prompt
from reviewhive.config import get_settings
from reviewhive.diff.budget import build_budget
from reviewhive.diff.parser import parse_diff
from reviewhive.models import AgentCall, AgentFindings, Finding
from reviewhive.pricing import cost_of

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "diffs"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass(frozen=True)
class Region:
    """A span of the new file, plus words that identify it when the anchor drifts.

    Anchors are good but not exact — a finding about a function sometimes lands on
    its decorator. Matching on the span alone would score a correct finding as a
    miss, so the keywords are a second route to the same region.
    """

    label: str
    lines: range
    keywords: tuple[str, ...]

    def matches(self, finding: Finding) -> bool:
        if finding.line is not None and finding.line in self.lines:
            return True
        haystack = f"{finding.title} {finding.body}".lower()
        return any(word.lower() in haystack for word in self.keywords)


@dataclass
class Case:
    name: str
    fixture: str
    # Regions that must produce at least one finding of any severity.
    must_flag: tuple[Region, ...] = ()
    # Regions that must not produce a *high* severity finding.
    must_not_call_high: tuple[Region, ...] = ()
    note: str = ""
    runs: list[list[Finding]] = field(default_factory=list)


CASES = [
    Case(
        name="missing_auth",
        fixture="missing_auth.diff",
        must_flag=(
            Region("purge (gated — control)", range(29, 37), ("purge", "admin_token")),
            Region("delete_task (ungated)", range(39, 43), ("delete_task", "delete")),
        ),
        note="The control proves the reviewer engaged with auth in this hunk at all.",
    ),
    Case(
        name="auth_among_many",
        fixture="auth_among_many.diff",
        must_flag=(
            Region("admin_export token (control)", range(59, 65), ("admin_token", "admin_export")),
            Region("delete_task (ungated)", range(52, 57), ("delete_task",)),
        ),
        note=(
            "The same defect as missing_auth, but surrounded by an injection, two "
            "hardcoded credentials and a path traversal — the diff it was actually "
            "missed on. Isolation is what makes missing_auth pass, so this is the "
            "case that matters."
        ),
    ),
    Case(
        name="ungated_notify",
        fixture="ungated_notify.diff",
        must_flag=(
            Region("notify_owner (ungated)", range(35, 39), ("notify_owner", "/notify/")),
        ),
        note="The second live miss. No sibling endpoint here has auth either.",
    ),
    Case(
        name="sound_auth",
        fixture="sound_auth.diff",
        must_not_call_high=(
            Region(
                "require_api_key (correct)",
                range(34, 39),
                ("api_key", "notify_api_key", "compare_digest", "bypass"),
            ),
        ),
        note="Correct code. Comment on it if you like, but it is not a high-severity hole.",
    ),
]


async def _count(text: str) -> int:
    return max(1, len(text) // 4)


async def run_once(client: AsyncAnthropic, model: str, diff_text: str, max_tokens: int):
    budget = await build_budget(
        parse_diff(diff_text), _count, max_prompt_tokens=60_000, max_file_diff_lines=400
    )
    if not budget.files:
        sys.exit("The fixture parsed to zero files — the diff is malformed, not the prompt.")

    message = await client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=load_prompt("security.md"),
        messages=[
            {
                "role": "user",
                "content": (
                    "Review the following pull request diff.\n\n"
                    f"<diff>\n{budget.prompt_text}\n</diff>"
                ),
            }
        ],
        output_format=AgentFindings,
    )
    parsed = message.parsed_output
    findings = parsed.findings if parsed else []
    call = AgentCall(
        agent="security",
        model=model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_read_tokens=message.usage.cache_read_input_tokens or 0,
    )
    return findings, float(cost_of(call) or 0)


def score_run(case: Case, findings: list[Finding]) -> tuple[bool, list[str]]:
    notes: list[str] = []
    ok = True

    for region in case.must_flag:
        hits = [f for f in findings if region.matches(f)]
        if hits:
            notes.append(f"  {GREEN}flagged{RESET}   {region.label} ({len(hits)})")
        else:
            ok = False
            notes.append(f"  {RED}MISSED{RESET}    {region.label}")

    for region in case.must_not_call_high:
        highs = [f for f in findings if region.matches(f) and f.severity == "high"]
        others = [f for f in findings if region.matches(f) and f.severity != "high"]
        if highs:
            ok = False
            notes.append(f"  {RED}HIGH{RESET}      {region.label} — {highs[0].title}")
        elif others:
            sev = others[0].severity
            notes.append(f"  {GREEN}ok{RESET}        {region.label} — flagged at {sev}, not high")
        else:
            notes.append(f"  {GREEN}ok{RESET}        {region.label} — not flagged at all")

    return ok, notes


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", choices=[c.name for c in CASES])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--show-bodies", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set.")

    cases = [c for c in CASES if args.only is None or c.name == args.only]
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    total_cost = 0.0
    passes: dict[str, int] = {}

    for case in cases:
        diff_text = (FIXTURES / case.fixture).read_text(encoding="utf-8")
        print(f"\n{'=' * 68}\n{case.name}  ({case.fixture})\n{DIM}{case.note}{RESET}\n{'=' * 68}")

        good = 0
        for attempt in range(1, args.runs + 1):
            findings, cost = await run_once(
                client, settings.agent_model, diff_text, settings.agent_max_tokens
            )
            total_cost += cost
            case.runs.append(findings)

            ok, notes = score_run(case, findings)
            good += ok
            mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
            print(f"\nrun {attempt}  {mark}  ({len(findings)} findings, ${cost:.4f})")
            print("\n".join(notes))
            if args.show_bodies:
                for f in findings:
                    print(f"    {DIM}[{f.severity}] {f.file}:{f.line} {f.title}{RESET}")
                    print(f"    {DIM}{f.body}{RESET}")

        passes[case.name] = good
        colour = GREEN if good == args.runs else (YELLOW if good else RED)
        print(f"\n{colour}{case.name}: {good}/{args.runs} runs passed{RESET}")

    print(f"\n{DIM}total ${total_cost:.4f}{RESET}")
    if any(v < args.runs for v in passes.values()):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

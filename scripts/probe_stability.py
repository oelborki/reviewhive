"""How much does one agent's output move when nothing else does?

Every other probe here scores *which* findings appear in a region. This one scores
whether the same findings appear at all from one run to the next, which is the
question behind "the review does not converge": across two iterative tests findings
went 11 -> 7 -> 8 and 7 -> 12, and it was never established how much of that
movement was the code changing versus the sampler.

    python scripts/probe_stability.py security tests/fixtures/diffs/mixed_rich.diff
    python scripts/probe_stability.py security <diff> --runs 6 --temperature 0

Nothing about the diff changes between runs, so every difference measured here is
the model. Two numbers come out:

  * **core** -- findings present in *every* run. These are what a user would call
    reliable.
  * **volatile** -- findings present in some runs and not others. On a re-review of
    unchanged code these are exactly the findings that appear to be "new", and are
    the mechanism behind a round that adds findings after a fix.

`temperature` is the reason this exists. `agents/base.py` sets none, so the agents
run at the API default; whether that costs recall or only costs stability has never
been measured, and it cannot be measured without a stability number to compare.

Note that temperature 0 is not a determinism guarantee -- it narrows sampling, it
does not make the service reproducible. Judge it by whether the spread shrinks
across several runs, not by whether two runs match exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import AsyncAnthropic

from reviewhive.agents.base import load_prompt
from reviewhive.agents.definitions import AGENTS
from reviewhive.config import get_settings
from reviewhive.diff.budget import build_budget
from reviewhive.diff.parser import parse_diff
from reviewhive.graph.dedupe import jaccard, normalize_path, title_tokens
from reviewhive.models import AgentCall, AgentFindings, Finding
from reviewhive.pricing import cost_of

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# Same rule the deterministic dedupe uses within a review. Reused rather than
# reinvented so "the same finding" means one thing in this project.
SAME_TITLE = 0.5

# Wider than dedupe's 3, because an anchor drifting between runs is not the same
# hazard as two findings colliding within one review: merging two distinct issues
# here understates instability, which is the conservative direction for this probe.
LINE_TOLERANCE = 4


@dataclass
class Cluster:
    """One issue, and which runs reported it."""

    file: str
    line: int | None
    title: str
    tokens: frozenset[str]
    runs: set[int] = field(default_factory=set)
    severities: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    def matches(self, finding: Finding) -> bool:
        """Same file, and either anchored to the same place or worded alike.

        **Category is deliberately not part of this.** The first version required
        it to be equal and reported 14% stability on a run where the model found
        the same two defects every time — it had simply called one of them
        `secrets` on three runs and `hardcoded-secret` on two. The slug is free
        text the model invents per call, so matching on it measures the model's
        taste in vocabulary and calls the result instability. It is reported
        below as its own axis instead.

        Line proximity carries the match because titles for one defect vary more
        than `SAME_TITLE` tolerates across runs — "API key exposed as hardcoded
        literal" and "API key committed as a literal string" score 0.43. Either
        signal alone is enough.
        """
        if normalize_path(finding.file) != self.file:
            return False
        if (
            finding.line is not None
            and self.line is not None
            and abs(finding.line - self.line) <= LINE_TOLERANCE
        ):
            return True
        return jaccard(title_tokens(finding.title), self.tokens) >= SAME_TITLE


def cluster_runs(runs: list[list[Finding]]) -> list[Cluster]:
    """Group findings that are the same issue across runs."""
    clusters: list[Cluster] = []
    for index, findings in enumerate(runs):
        for finding in findings:
            match = next((c for c in clusters if c.matches(finding)), None)
            if match is None:
                match = Cluster(
                    file=normalize_path(finding.file),
                    line=finding.line,
                    title=finding.title,
                    tokens=title_tokens(finding.title),
                )
                clusters.append(match)
            match.runs.add(index)
            match.severities.append(finding.severity)
            match.categories.append(finding.category)
    return clusters


async def _count(text: str) -> int:
    return max(1, len(text) // 4)


async def run_once(
    client: AsyncAnthropic,
    settings,
    prompt_file: str,
    agent: str,
    diff_text: str,
    temperature: float | None,
) -> tuple[list[Finding], float]:
    budget = await build_budget(
        parse_diff(diff_text), _count, max_prompt_tokens=60_000, max_file_diff_lines=400
    )
    if not budget.files:
        sys.exit("The fixture parsed to zero files — the diff is malformed, not the prompt.")

    extra = {} if temperature is None else {"temperature": temperature}
    message = await client.messages.parse(
        model=settings.agent_model,
        max_tokens=settings.agent_max_tokens,
        system=load_prompt(prompt_file),
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
        **extra,
    )
    parsed = message.parsed_output
    call = AgentCall(
        agent=agent,
        model=settings.agent_model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_read_tokens=message.usage.cache_read_input_tokens or 0,
    )
    return (parsed.findings if parsed else []), float(cost_of(call) or 0)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("agent", choices=sorted(a.name for a in AGENTS))
    parser.add_argument("diff", type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Omit to use the API default, which is what the agents ship with.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set.")

    spec = next(a for a in AGENTS if a.name == args.agent)
    diff_text = args.diff.read_text(encoding="utf-8")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    label = "API default" if args.temperature is None else f"temperature={args.temperature}"
    print(f"\n{'=' * 68}\n{args.agent}  {args.diff.name}  ({label}, {args.runs} runs)\n{'=' * 68}")

    runs: list[list[Finding]] = []
    total_cost = 0.0
    for attempt in range(1, args.runs + 1):
        findings, cost = await run_once(
            client, settings, spec.prompt_file, args.agent, diff_text, args.temperature
        )
        runs.append(findings)
        total_cost += cost
        print(f"run {attempt}: {len(findings)} findings  {DIM}${cost:.4f}{RESET}")

    counts = [len(r) for r in runs]
    clusters = cluster_runs(runs)
    core = [c for c in clusters if len(c.runs) == args.runs]
    volatile = [c for c in clusters if len(c.runs) < args.runs]

    print(f"\n{'-' * 68}")
    spread = f"{min(counts)}-{max(counts)}"
    stdev = statistics.stdev(counts) if len(counts) > 1 else 0.0
    print(f"findings per run : {spread}  (mean {statistics.mean(counts):.1f}, stdev {stdev:.2f})")
    print(f"{GREEN}core{RESET}             : {len(core)} reported in every run")
    print(f"{YELLOW}volatile{RESET}         : {len(volatile)} reported in some runs only")

    if clusters:
        rate = len(core) / len(clusters)
        colour = GREEN if rate >= 0.8 else (YELLOW if rate >= 0.5 else RED)
        print(f"stability        : {colour}{rate:.0%}{RESET} of distinct issues are core")

    # Severity moving on a *stable* finding is a separate instability, and one the
    # summary would otherwise hide -- the same issue ranked high on one run and low
    # on the next changes what a reader sees just as much as it appearing at all.
    wobbly = [c for c in core if len(set(c.severities)) > 1]
    if wobbly:
        print(f"\n{YELLOW}severity moved on {len(wobbly)} core finding(s):{RESET}")
        for c in wobbly:
            print(f"  {c.file}:{c.line} · {'/'.join(c.severities)} · {c.title}")

    # Its own axis, not part of matching. A stable finding filed under a different
    # slug each run still reaches the reader as the same finding, but anything
    # grouping or filtering by category downstream sees churn.
    renamed = [c for c in core if len(set(c.categories)) > 1]
    if renamed:
        print(f"\n{YELLOW}category moved on {len(renamed)} core finding(s):{RESET}")
        for c in renamed:
            print(f"  {c.file}:{c.line} · {'/'.join(sorted(set(c.categories)))} · {c.title}")

    if volatile:
        print(f"\n{DIM}volatile findings — these are what a re-review reports as new:{RESET}")
        for c in sorted(volatile, key=lambda c: -len(c.runs)):
            print(f"  {DIM}{len(c.runs)}/{args.runs}  {c.file}:{c.line} · {c.title}{RESET}")

    print(f"\n{DIM}total ${total_cost:.4f}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())

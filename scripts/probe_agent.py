"""Run a single agent against a local .diff file.

Prompt changes are not covered by the test suite — the suite is offline, so it can
pin the deterministic pipeline but says nothing about how a prompt lands. This is
how to measure one:

    python scripts/probe_agent.py style tests/fixtures/diffs/mixed_rich.diff
    python scripts/probe_agent.py architecture tests/fixtures/diffs/style_only.diff

One agent instead of three costs a third as much and isolates the variable. Pair
`style_only.diff` with `mixed_rich.diff` — identical but for two vulnerabilities —
to tell whether an agent strays because security material is eye-catching or
merely because its own lane is empty.

Probe against a diff that carries real material for the agent under test.
`mixed.diff` holds almost nothing outside security, and an agent with an empty
lane answers inconsistently run to run, which reads as a prompt regression that
is not there.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from anthropic import AsyncAnthropic

from reviewhive.agents.definitions import AGENTS
from reviewhive.config import get_settings
from reviewhive.graph.build import build_review_graph
from reviewhive.models import ReviewResult
from reviewhive.pricing import total_cost

SEVERITY_COLOUR = {"high": "\033[31m", "medium": "\033[33m", "low": "\033[90m"}
RESET = "\033[0m"
DIM = "\033[2m"

AGENT_NAMES = [spec.name for spec in AGENTS]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("agent", choices=AGENT_NAMES, help="Which reviewer to run")
    parser.add_argument("diff", type=Path, help="Path to a unified diff file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show pipeline logging")
    return parser.parse_args()


async def run(agent: str, diff_path: Path) -> tuple[ReviewResult, float]:
    settings = get_settings()
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")

    spec = next(s for s in AGENTS if s.name == agent)
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.agent_timeout_seconds,
        max_retries=settings.agent_max_retries,
    )
    # The real graph, narrowed to one branch — same parsing, budgeting, and
    # anchoring as a full review, so a probe result means what it appears to.
    graph = build_review_graph(client, settings, agents=(spec,))

    started = time.perf_counter()
    final_state = await graph.ainvoke({"diff_text": diff_path.read_text(encoding="utf-8")})
    elapsed = time.perf_counter() - started

    await client.close()
    return final_state["result"], elapsed


def print_console(agent: str, diff_path: Path, result: ReviewResult, elapsed: float) -> None:
    print(f"{DIM}{agent} on {diff_path.name}{RESET}\n")

    if not result.findings:
        print("No findings.\n")
    for finding in result.findings:
        colour = SEVERITY_COLOUR[finding.severity]
        location = finding.file + (f":{finding.line}" if finding.line else " (file-level)")
        print(f"{colour}{finding.severity.upper():<6}{RESET} {location}")
        print(f"       {finding.title}")
        print(f"       {DIM}{finding.category} · {finding.confidence:.2f}{RESET}")
        print()

    # Coverage has to be visible here too. A fixture the parser rejects yields an
    # empty review that otherwise looks like a clean one.
    for entry in result.skipped_files:
        print(f"{DIM}skipped  {entry}{RESET}")
    for entry in result.truncated_files:
        print(f"{DIM}shortened {entry}{RESET}")

    call = result.calls[0] if result.calls else None
    if call is None:
        print(f"\n{DIM}no agent call was made — nothing in the diff was reviewable{RESET}")
        return

    status = f" ERROR: {call.error}" if call.error else ""
    print(
        f"\n{DIM}{call.agent:<13} {int(elapsed * 1000):>6}ms  "
        f"{call.findings_returned} finding(s)  "
        f"{call.input_tokens:>6} in / {call.output_tokens:>5} out{status}{RESET}"
    )
    print(f"{DIM}cost          ${total_cost(result):.4f}{RESET}")


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if not args.diff.is_file():
        sys.exit(f"No such file: {args.diff}")

    result, elapsed = asyncio.run(run(args.agent, args.diff))
    print_console(args.agent, args.diff, result, elapsed)


if __name__ == "__main__":
    main()

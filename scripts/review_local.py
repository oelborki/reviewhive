"""Run the full review graph against a local .diff file.

This is the Phase 1 development loop and the fastest way to iterate on prompts
without touching GitHub:

    python scripts/review_local.py tests/fixtures/diffs/mixed.diff
    git diff main... > /tmp/pr.diff && python scripts/review_local.py /tmp/pr.diff

`--markdown` prints exactly what would be posted as the PR review body.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from anthropic import AsyncAnthropic

from reviewhive.config import get_settings
from reviewhive.graph.build import build_review_graph
from reviewhive.logging_setup import configure_logging
from reviewhive.models import ReviewResult
from reviewhive.persistence import NullReviewStore
from reviewhive.pricing import total_cost
from reviewhive.render import render_summary

logger = logging.getLogger("reviewhive.cli")

SEVERITY_COLOUR = {"high": "\033[31m", "medium": "\033[33m", "low": "\033[90m"}
RESET = "\033[0m"
DIM = "\033[2m"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("diff", type=Path, help="Path to a unified diff file")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Print the PR review body instead of the console summary",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show pipeline logging")
    return parser.parse_args()


@asynccontextmanager
async def open_store(settings) -> AsyncIterator:
    """A review store, or a no-op if no database is configured.

    Unconfigured is a supported state, not a degraded one: this script is the
    prompt-iteration loop, and needing Postgres running to try a prompt would be a
    poor trade.
    """
    if not settings.database_url:
        yield NullReviewStore(), False
        return

    # Imported lazily so the script still runs when the `db` extra is not
    # installed and no database is configured.
    from reviewhive.db.session import review_store

    async with review_store(settings.database_url) as store:
        yield store, True


async def run(diff_path: Path) -> tuple[ReviewResult, float, UUID | None]:
    settings = get_settings()
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")

    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")

    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.agent_timeout_seconds,
        max_retries=settings.agent_max_retries,
    )
    graph = build_review_graph(client, settings)

    async with open_store(settings) as (store, persisting):
        # Recorded before the review runs, not after. It costs a round trip and
        # buys the property Phase 3 needs: a run that dies halfway leaves a
        # `pending` row behind rather than no evidence at all.
        review_id = await store.start_review(
            source="cli", diff_text=diff_text, diff_path=str(diff_path)
        )

        started = time.perf_counter()
        try:
            final_state = await graph.ainvoke({"diff_text": diff_text})
        except Exception as exc:
            await store.fail_review(review_id, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            elapsed = time.perf_counter() - started
            await client.close()

        result = final_state["result"]

        # A failed write must not cost the review. It took real money and real
        # minutes; losing the output in order to report a database problem is the
        # wrong way round, so this is logged rather than raised.
        try:
            await store.finish_review(review_id, result, elapsed_ms=int(elapsed * 1000))
        except Exception:
            logger.exception("review completed but could not be saved")
            persisting = False

    return result, elapsed, review_id if persisting else None


def print_console(result: ReviewResult, elapsed: float) -> None:
    if not result.findings:
        print("No findings.")
    for finding in result.findings:
        colour = SEVERITY_COLOUR[finding.severity]
        location = finding.file + (f":{finding.line}" if finding.line else "")
        sources = "+".join(finding.sources)
        print(f"{colour}{finding.severity.upper():<6}{RESET} {location}")
        print(f"       {finding.title}")
        print(f"       {DIM}{finding.category} · {sources} · {finding.confidence:.2f}{RESET}")
        print()

    if result.suppressed_count:
        print(f"{DIM}...and {result.suppressed_count} more below the posting cap.{RESET}\n")

    for entry in result.skipped_files:
        print(f"{DIM}skipped  {entry}{RESET}")
    for entry in result.truncated_files:
        print(f"{DIM}shortened {entry}{RESET}")

    print()
    # Per-agent latency is what shows the fan-out is concurrent: the wall time
    # should track the slowest agent, not the sum of all three.
    for call in sorted(result.calls, key=lambda c: c.agent):
        status = f" ERROR: {call.error}" if call.error else ""
        print(
            f"{DIM}{call.agent:<13} {call.latency_ms:>6}ms  "
            f"{call.findings_returned} finding(s)  "
            f"{call.input_tokens:>6} in / {call.output_tokens:>5} out{status}{RESET}"
        )
    slowest = max((c.latency_ms for c in result.calls), default=0)
    print(
        f"{DIM}{'total':<13} {int(elapsed * 1000):>6}ms  "
        f"(slowest agent {slowest}ms — serial would be "
        f"{sum(c.latency_ms for c in result.calls)}ms){RESET}"
    )
    print(f"{DIM}cost          ${total_cost(result):.4f}{RESET}")


def main() -> None:
    args = parse_args()
    configure_logging("INFO" if args.verbose else "WARNING")

    if not args.diff.is_file():
        sys.exit(f"No such file: {args.diff}")

    result, elapsed, review_id = asyncio.run(run(args.diff))

    if args.markdown:
        print(render_summary(result))
    else:
        print_console(result, elapsed)
        # Only when it actually landed. Printing an id for a review that was never
        # stored would be worse than printing nothing.
        if review_id is not None:
            print(f"{DIM}saved         {review_id}{RESET}")


if __name__ == "__main__":
    main()

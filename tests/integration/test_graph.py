"""End-to-end graph behaviour, entirely offline."""

from __future__ import annotations

import time

import pytest
from tests.stubs import StubAnthropic, finding, overloaded_error

from reviewhive.config import Settings
from reviewhive.graph.build import build_review_graph, review_diff


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test", enable_llm_merge=False)


async def test_runs_all_three_agents(diff_text, settings) -> None:
    client = StubAnthropic(responses={"security": [finding()]})

    result = await review_diff(diff_text, client, settings)

    assert sorted(client.parse_calls) == ["architecture", "security", "style"]
    assert len(result.findings) == 1
    assert result.findings[0].sources == ["security"]


async def test_agents_run_concurrently_not_sequentially(diff_text, settings) -> None:
    """The whole point of the fan-out. Three 150ms calls must not take 450ms."""
    client = StubAnthropic(latency=0.15)

    started = time.perf_counter()
    await review_diff(diff_text, client, settings)
    elapsed = time.perf_counter() - started

    assert client.max_concurrent == 3, "agent nodes did not overlap"
    assert elapsed < 0.35, f"fan-out appears serialized: took {elapsed:.2f}s for 3x150ms"


async def test_overlapping_findings_are_merged_with_provenance(diff_text, settings) -> None:
    """Two agents describing the same problem collapse into one attributed finding."""
    client = StubAnthropic(
        responses={
            "security": [finding(line=13, title="SQL query built by string concatenation")],
            "architecture": [
                finding(
                    line=13,
                    severity="medium",
                    category="injection",
                    title="SQL string concatenation in query",
                    confidence=0.7,
                )
            ],
        }
    )

    result = await review_diff(diff_text, client, settings)

    assert len(result.findings) == 1
    merged = result.findings[0]
    assert sorted(merged.sources) == ["architecture", "security"]
    assert merged.severity == "high", "merged finding keeps the highest severity"
    assert merged.confidence > 0.9, "independent agreement raises confidence"


async def test_distinct_findings_are_not_merged(diff_text, settings) -> None:
    client = StubAnthropic(
        responses={
            "security": [finding(line=13, title="SQL query built by string concatenation")],
            "style": [finding(line=47, severity="low", category="naming", title="Unclear name")],
        }
    )

    result = await review_diff(diff_text, client, settings)

    assert len(result.findings) == 2
    assert result.findings[0].severity == "high", "high severity ranks first"


async def test_one_failing_agent_does_not_fail_the_review(diff_text, settings) -> None:
    client = StubAnthropic(
        responses={"style": [finding(line=47, category="naming", title="Unclear name")]},
        errors={"security": RuntimeError("boom")},
    )

    with pytest.raises(RuntimeError):
        # A non-API error is a real bug and should surface, not be swallowed.
        await review_diff(diff_text, client, settings)


async def test_api_error_from_one_agent_degrades_gracefully(diff_text, settings) -> None:
    client = StubAnthropic(
        responses={"style": [finding(line=47, category="naming", title="Unclear name")]},
        errors={"security": overloaded_error()},
    )

    result = await review_diff(diff_text, client, settings)

    assert len(result.findings) == 1, "the surviving agents still produce a review"
    failed = next(c for c in result.calls if c.agent == "security")
    assert failed.error is not None


async def test_findings_naming_files_outside_the_diff_are_dropped(diff_text, settings) -> None:
    client = StubAnthropic(responses={"security": [finding(file="src/app/imaginary.py")]})

    result = await review_diff(diff_text, client, settings)

    assert result.findings == []


async def test_line_is_snapped_to_a_real_anchor(diff_text, settings) -> None:
    # 15 is context; 16 is an added line two away. It should snap, not be dropped.
    client = StubAnthropic(responses={"security": [finding(line=15)]})

    result = await review_diff(diff_text, client, settings)

    assert result.findings[0].line in {15, 16}


async def test_wild_line_degrades_to_file_level(diff_text, settings) -> None:
    client = StubAnthropic(responses={"security": [finding(line=9000)]})

    result = await review_diff(diff_text, client, settings)

    assert len(result.findings) == 1
    assert result.findings[0].line is None, "an unbelievable line is dropped, the finding is not"


async def test_diff_with_nothing_reviewable_skips_the_agents(settings) -> None:
    """A lockfile-only PR must cost zero tokens."""
    lockfile_only = (
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1,2 +1,2 @@\n"
        " {\n"
        '-  "version": "1.0.0"\n'
        '+  "version": "1.0.1"\n'
    )
    client = StubAnthropic()

    result = await review_diff(lockfile_only, client, settings)

    assert client.parse_calls == [], "no agent should have been called"
    assert result.findings == []
    assert any("lockfile" in entry for entry in result.skipped_files)


async def test_result_reports_what_was_not_reviewed(diff_text, settings) -> None:
    result = await review_diff(diff_text, StubAnthropic(), settings)

    assert any("package-lock.json" in s for s in result.skipped_files)
    assert any("assets/logo.png" in s for s in result.skipped_files)


async def test_posting_cap_reports_the_remainder(diff_text) -> None:
    settings = Settings(anthropic_api_key="test", max_posted_findings=2, enable_llm_merge=False)
    client = StubAnthropic(
        responses={
            "security": [
                finding(line=13, title="SQL query built by string concatenation"),
                finding(line=19, title="Hardcoded API token in source", category="secret"),
                finding(line=47, title="Password compared with equality", category="timing"),
                finding(line=None, title="Module lacks any error handling", category="errors"),
            ]
        }
    )

    result = await review_diff(diff_text, client, settings)

    assert len(result.findings) == 2
    assert result.suppressed_count == 2


async def test_low_confidence_findings_are_filtered(diff_text) -> None:
    settings = Settings(anthropic_api_key="test", min_confidence=0.6, enable_llm_merge=False)
    client = StubAnthropic(
        responses={
            "style": [
                finding(line=13, title="Confident issue", confidence=0.9),
                finding(line=47, title="Wild speculation", category="guess", confidence=0.2),
            ]
        }
    )

    result = await review_diff(diff_text, client, settings)

    assert [f.title for f in result.findings] == ["Confident issue"]


async def test_graph_is_reusable_across_reviews(diff_text, settings) -> None:
    """Compiled once per process, invoked per PR — no state leaks between runs."""
    client = StubAnthropic(responses={"security": [finding()]})
    compiled = build_review_graph(client, settings)

    first = await compiled.ainvoke({"diff_text": diff_text})
    second = await compiled.ainvoke({"diff_text": diff_text})

    assert len(first["result"].findings) == len(second["result"].findings) == 1

# reviewHive

A multi-agent AI code reviewer for GitHub pull requests. Three specialized agents
examine the diff **in parallel**, their findings are deduplicated and ranked, and
the result is posted as a single review.

> **Status: Phase 1 of 5.** The review pipeline runs end to end against a local
> diff file. GitHub webhooks, persistence, and deployment are not built yet — see
> [Roadmap](#roadmap).

## How it works

```
             ┌──────────────┐
   diff ───▶ │ prepare_diff │  parse · filter noise · truncate · fit token budget
             └──────┬───────┘
          ┌─────────┼─────────┐
          ▼         ▼         ▼        three LangGraph branches, run concurrently
      security    style    architecture
          └─────────┼─────────┘
             ┌──────▼───────┐
             │   finalize   │  deduplicate · validate line anchors · rank · cap
             └──────┬───────┘
                    ▼
              ReviewResult
```

**The agents.** Each gets the same diff and a different system prompt, and returns
findings as validated structured output (`messages.parse`), so there is no
JSON-scraping or repair path.

| Agent | Looks for |
|---|---|
| Security & Correctness | Injection, hardcoded secrets, auth gaps, edge cases, error handling |
| Style & Maintainability | Naming, complexity, readability, consistency with the surrounding diff |
| Architecture | Responsibility creep, duplication, coupling, speculative abstraction |

**The parallelism is real.** Every node is `async def`, the graph is driven with
`ainvoke`, and the fan-out comes from a router returning a list of node names.
`ReviewState.findings` carries an `operator.add` reducer, so three branches write
the same key concurrently without clobbering each other. Wall time tracks the
slowest agent, not the sum — `tests/integration/test_graph.py` asserts this rather
than assuming it.

**Deduplication is deterministic first.** Findings in the same file within a few
lines of each other, whose titles overlap above a threshold, collapse into one
finding that records which agents raised it. Only the residue that co-locates but
does not textually match is handed to an LLM merge pass (Phase 2).

Agreement is recorded but does not raise confidence. That was the original design
and measurement killed it: probing one agent at a time shows the reviewers are not
independent observers — they converge on whatever defect is most salient in the
diff, so a second report is usually the same observation restated. A reviewer
straying outside its specialty produces that shape exactly, which meant a scope
violation could promote the very finding it duplicated. Agreement now breaks ties
between equally confident findings and nothing more.

**Coverage is always disclosed.** Lockfiles, generated code, vendored trees, and
binaries are filtered out; oversized files are truncated at hunk boundaries; the
prompt is capped using `messages.count_tokens` (never a local estimator — those are
tuned for a different tokenizer). Everything dropped or shortened is listed in the
posted summary, because a bot that quietly skipped half the diff is worse than one
that reviewed nothing.

**Model-reported line numbers are verified, not trusted.** Every finding's location
is checked against the parsed diff. A near miss snaps to the real line, an
implausible one degrades the finding to file-level, and a finding naming a file
outside the diff is discarded. This matters because GitHub rejects an entire review
request if any single comment anchor is invalid.

## Running it

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -e ".[dev]"
cp .env.example .env                                 # then add your API key

python scripts/review_local.py tests/fixtures/diffs/mixed.diff
python scripts/review_local.py tests/fixtures/diffs/mixed.diff --markdown
```

Review your own working branch:

```bash
git diff main... > /tmp/pr.diff
python scripts/review_local.py /tmp/pr.diff -v
```

Iterating on a prompt? Run one agent instead of three — a third of the cost, and
only one variable moving:

```bash
python scripts/probe_agent.py style tests/fixtures/diffs/mixed_rich.diff
```

Probe against a diff carrying real material for the agent under test.
`style_only.diff` and `mixed_rich.diff` are identical but for two vulnerabilities,
so the pair separates an agent straying because security is eye-catching from one
straying because its own lane is empty.

Cost is **$0.012–$0.015 per pull request** on `claude-haiku-4-5`, measured across
several runs of `mixed.diff`; both scripts print the exact figure and per-agent
latency for every run.

## Tests

```bash
pytest        # 94 tests, no network, no API spend
ruff check .
```

The suite never contacts the Anthropic API. The graph takes its client as a
constructor argument, so tests build the real graph around an in-memory stub
(`tests/stubs.py`) with no monkeypatching — including the timing assertion that
proves the fan-out overlaps.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Review pipeline against local diffs | **Done** |
| 2 | PostgreSQL persistence, per-call cost telemetry, LLM merge pass | Next |
| 3 | Webhook endpoint, signature verification, inline review comments | |
| 4 | Full test coverage, CI | |
| 5 | Docker, temporary deploy, demo | |

## Deliberately out of scope

Whole-repository context (diffs only), a public GitHub App with per-install auth,
incremental re-review on force-push, more than three agents, and chunked review of
a single oversized file. Each would add real complexity without changing what this
project demonstrates.

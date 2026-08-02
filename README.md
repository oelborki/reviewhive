# ReviewHive

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
does not textually match is left alone for now.

An LLM merge pass is the intended second step, deferred to Phase 4 rather than
built alongside the rest of persistence. It is the one piece of this with no
objective success criterion — the point at which "is this the same finding?"
becomes a judgement call — and the honest way to build it is against duplicates
that actually survived, which the `findings` table now records. Building it first
would have meant tuning a threshold against a guess.
`settings.enable_llm_merge` reserves the name and defaults to off.

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

Cost on `claude-haiku-4-5`, measured: **$0.013** for a small single-file diff,
**$0.023** for a real six-file pull request (~6,400 input tokens per agent). It
scales with diff size, so treat the smaller figure as a floor rather than a
typical PR. Both scripts print the exact figure and per-agent latency for every
run.

## Persistence

Optional. With no `REVIEWHIVE_DATABASE_URL` set, the CLI behaves exactly as above
and stores nothing — the prompt-iteration loop should not need infrastructure.
With one set, every run is recorded.

```bash
pip install -e ".[db]"
docker compose up -d db
alembic upgrade head
```

Three tables: a `reviews` row per run, an `agent_calls` row per model request, and
a `findings` row per posted finding. The run is written **before** the review
starts, so a crash leaves a `pending` row rather than nothing — the same two-phase
path the webhook will use.

Cost is priced per model in `pricing.py` and computed once, at write time, so a
stored figure stays a snapshot of the rates in force when the run happened rather
than being silently rewritten by a later price change. A model with no published
rate stores `NULL`, which keeps "we don't know" distinguishable from "it was
free".

The diff itself is not stored — only its SHA-256 and byte count. Diffs are
unbounded, usually someone else's source, and re-fetchable by ref. The tradeoff is
that a review cannot be replayed offline from the database alone.

```sql
SELECT r.id, r.created_at, r.total_cost_usd,
       sum(c.input_tokens + c.output_tokens) AS tokens,
       count(*) FILTER (WHERE c.error IS NOT NULL) AS failed_agents
FROM reviews r JOIN agent_calls c ON c.review_id = r.id
GROUP BY r.id ORDER BY r.created_at DESC LIMIT 20;
```

Because findings are rows rather than a JSON blob, the reviewers can be measured
over time instead of eyeballed one run at a time:

```sql
-- Which categories does each reviewer actually file?
SELECT unnest(sources) AS agent, category, count(*)
FROM findings GROUP BY 1, 2 ORDER BY 3 DESC;
```

## Tests

```bash
pytest              # 160 tests, no network, no API spend, no database
ruff check .
pytest -m db        # 12 more, against the compose Postgres
```

The default run never contacts the Anthropic API and never needs infrastructure.
The graph takes its client as a constructor argument, so tests build the real
graph around an in-memory stub (`tests/stubs.py`) with no monkeypatching —
including the timing assertion that proves the fan-out overlaps. Persistence gets
the same treatment: callers depend on a `ReviewStore` protocol, and the unit suite
runs against an in-memory implementation of it.

The `db`-marked tests are the exception and are deselected by default. They build
their schema by running the migration rather than `create_all()`, so the migration
cannot drift from the models unnoticed, and they refuse to run against any
database not named `reviewhive_test`.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Review pipeline against local diffs | **Done** |
| 2 | PostgreSQL persistence, per-call cost telemetry | **Done** |
| 3 | Webhook endpoint, signature verification, inline review comments | Next |
| 4 | Full test coverage, CI, LLM merge pass | |
| 5 | Docker, temporary deploy, demo | |

## Deliberately out of scope

Whole-repository context (diffs only), a public GitHub App with per-install auth,
incremental re-review on force-push, more than three agents, and chunked review of
a single oversized file. Each would add real complexity without changing what this
project demonstrates.

# ReviewHive

A multi-agent AI code reviewer for GitHub pull requests. Three specialized agents
examine the diff **in parallel**, their findings are deduplicated and ranked, and
the result is posted as a single review.

> **Status: Phase 3 of 5.** Open a pull request on a configured repository and a
> review with inline comments appears. Runs are persisted to Postgres. Remaining:
> full CI, the LLM merge pass, and a containerised deployment — see
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

The first live review supplied both halves of that judgement call. A real
duplicate survived: the same missing auth check filed one line apart by two
agents, uncollapsed because their titles share almost no words. And a tempting
fix turned out to be a trap — two findings with an *identical* title four lines
apart were two different vulnerabilities, one injecting a path parameter and one
a database value. Widening the line tolerance to catch the first would have
merged the second pair and silently dropped a real finding. Matching text is not
matching meaning, in either direction.

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

On the first live review, 14 of 15 inline comments landed on the defect line
itself, checked against the source rather than against the validator. The
fifteenth pointed at a route decorator three lines above the condition it
described — inside the right function, and readable, but not exact. Line-perfect
anchoring is not a claim this makes.

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
**$0.023** for a real six-file pull request (~6,400 input tokens per agent), and
**$0.028 in 10.6 s** for the first end-to-end webhook review — a four-file diff
that produced 25 findings, of which 15 were posted. It scales with diff size, so
treat the smallest figure as a floor rather than a typical PR. Both scripts print
the exact figure and per-agent latency for every run.

## Reviewing real pull requests

```bash
pip install -e ".[service,db]"
uvicorn reviewhive.api.app:app --port 8000
```

Point a repository's webhook at it, subscribed to **Pull requests**, with the
content type set to **`application/json`**. The other content type sends the body
as a form field and signs *that*, so every signature fails and it reads as a
broken HMAC for an afternoon.

The endpoint verifies, records, and returns `202` with a review id; the graph
then runs as a background task and posts the result. The ordering inside the
handler is the interesting part:

1. **Read the raw bytes before parsing anything.** The signature covers exactly
   what GitHub sent, and a JSON round trip reorders keys and re-escapes unicode.
   This is not hypothetical — fetching one of our own deliveries back from the
   API returns a *parsed* payload, and re-serialising it fails against the
   signature GitHub actually sent.
2. **Verify before checking the allowlist**, so an unauthenticated caller cannot
   discover which repositories are configured. Every rejection answers the same
   generic `401`.
3. **Answer `200` for deliberate non-action.** A `4xx` marks the delivery red in
   GitHub's log, and a log where red is routine is a log nobody reads. `ping`
   especially: it is the first thing GitHub sends when a hook is created.

Reviews trigger on `opened`, `reopened` and `ready_for_review`. `synchronize` is
behind `REVIEWHIVE_REVIEW_ON_SYNCHRONIZE`, off by default: it fires once per push
with a fresh head sha, so the head-sha check cannot collapse a burst and five
quick commits would be five reviews. Drafts are skipped until marked ready.

**Idempotency uses a query and a constraint, deliberately.** GitHub redelivers on
timeout and offers a Redeliver button, so the handler looks the delivery up
first — the common path should be a clean answer, not a caught exception. But two
concurrent redeliveries both pass that lookup, so `delivery_id` is also `UNIQUE`.
The query is ergonomics; the constraint is correctness. Separately, a review is
keyed on `(repo, pr_number, head_sha)`, because a draft marked ready right after
being opened is two deliveries describing the same commit.

**A rejected anchor degrades rather than failing.** GitHub rejects an *entire*
review request if one comment is misplaced, so every line is validated against the
parsed diff before sending — and if GitHub still refuses, the review is re-posted
with no inline comments and a note saying so. The retry is guarded: a `422` from a
stale commit sha or a read-only token has nothing to remove, so it is not retried.

**Background tasks die with the process.** A deploy or a `--reload` restart
mid-review strands a `running` row. That is the honest cost of running in-process
rather than behind a queue; `mark_running` at least makes the orphan diagnosable
rather than invisible. A worker queue is the scaling path, and deliberately not
built.

### The local loop

Iterating against real pull requests is slow and costs a review each time.
Replay a captured delivery instead:

```bash
uvicorn reviewhive.api.app:app --reload --port 8000
npx smee-client --url https://smee.io/<channel> \
                --target http://127.0.0.1:8000/webhooks/github

python scripts/replay_webhook.py tests/fixtures/webhooks/pull_request_opened.json
python scripts/replay_webhook.py <fixture> --repo you/demo --pr 7 --delivery <id>
```

The script signs the exact bytes it sends with the same `sign()` the server
verifies with. A saved fixture can never carry a reusable signature — storing it
re-serialises the JSON — so re-signing is the only thing that can work, and
sharing the implementation keeps the script from drifting from the code under
test. It uses a fresh delivery id per run unless given one, so replays are not
deduplicated away while iterating.

The token needs three permissions, and the third is easy to miss: **Pull
requests: read and write** to post, **Issues: read and write** because pull
request conversation comments are gated under Issues, and **Contents: read**
because the `.diff` media type is repository content. Without the last one,
posting reviews and listing files both return `200` and only the diff fetch
`403`s — which reads as a bug in the fetch rather than a missing scope.

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
pytest              # 250 tests, no network, no API spend, no database
ruff check .
pytest -m db        # 23 more, against the compose Postgres
```

The default run never contacts the Anthropic API and never needs infrastructure.
The graph takes its client as a constructor argument, so tests build the real
graph around an in-memory stub (`tests/stubs.py`) with no monkeypatching —
including the timing assertion that proves the fan-out overlaps. Persistence gets
the same treatment: callers depend on a `ReviewStore` protocol, and the unit suite
runs against an in-memory implementation of it.

The GitHub layer gets the same treatment. `signature.py`, `positions.py`,
`client.py` and `jobs.py` import neither FastAPI nor SQLAlchemy, so their tests —
including the full fetch → review → post path — run on a bare install with an
`httpx.MockTransport` standing in for GitHub. Only the endpoint test needs the
`service` extra, and it skips rather than fails without it.

The `db`-marked tests are the exception and are deselected by default. They build
their schema by running the migration rather than `create_all()`, so the migration
cannot drift from the models unnoticed, and they refuse to run against any
database not named `reviewhive_test`.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Review pipeline against local diffs | **Done** |
| 2 | PostgreSQL persistence, per-call cost telemetry | **Done** |
| 3 | Webhook endpoint, signature verification, inline review comments | **Done** |
| 4 | Full test coverage, CI, LLM merge pass | Next |
| 5 | Docker, temporary deploy, demo | |

## Deliberately out of scope

Whole-repository context (diffs only), a public GitHub App with per-install auth,
incremental re-review on force-push, more than three agents, and chunked review of
a single oversized file. Each would add real complexity without changing what this
project demonstrates.

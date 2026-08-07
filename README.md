# ReviewHive

A multi-agent AI code reviewer for GitHub pull requests. Three specialized agents
examine the diff **in parallel**, their findings are deduplicated and ranked, and
the result is posted as a single review.

> **Status: Phase 4 of 5.** Open a pull request on a configured repository and a
> review with inline comments appears; mention the bot in a comment and it answers.
> Runs are persisted to Postgres, a critic pass checks each finding against the
> lines it is about, an LLM merge pass collapses the cross-lane duplicates title
> matching cannot see, and `docker compose up` runs the whole thing. Remaining: a temporary public deployment and a demo recording — see
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
             │   finalize   │  deduplicate · validate anchors · check claims ·
             │              │  merge cross-lane duplicates · rank · cap
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

**A critic pass then checks each finding against the lines it is about.** Nothing
before it asks whether a finding is *correct*, and two measured defects follow from
that. A reviewer writing outside its specialty rates the finding it borrowed higher
than the specialist who owns it — on one pull request the architecture agent filed a
whitelisted `ORDER BY` as high-severity SQL injection while the security agent, which
is right about that code in twelve runs of thirteen, filed it as medium, and the
reader saw the high one. Neither threshold separates those: the borrowed findings
carry high confidence *because* the vulnerabilities they copy are real, and they rate
higher rather than lower. Separately, every false positive measured so far has one
shape — a compound condition quoted correctly and then evaluated backwards.

So the pass gets each finding together with a window of the diff around its anchor,
and may lower its severity, rewrite it, or withdraw it. It cannot raise a severity,
act on a finding it was not shown, or judge one twice; those are refused in code
rather than requested in the prompt, because a prompt is a request and these are
guarantees. Withdrawals are counted and disclosed in the posted summary, separately
from the threshold count — an over-eager critic is otherwise invisible.

It runs *before* the merge pass. Merging keeps the higher of two severities, so
afterwards the borrowed rating and the specialist's are one number and there is
nothing left to reconcile. Scored against ten findings taken from the `findings`
table, 10/10 across five runs; seven of the ten must survive untouched, so a critic
that deletes everything scores 3/10. Measured at $0.0126, and
`REVIEWHIVE_ENABLE_CRITIC=false` turns it off.

**An LLM merge pass then takes the residue**, and it was deliberately built last.
It is the one piece here with no objective success criterion — the point at which
"is this the same finding?" becomes a judgement call — so it was built against
duplicates that had actually survived into the `findings` table rather than
against a guess. That evaluation set is ten cross-lane pairs at identical line
numbers, split roughly evenly between pairs that must merge and pairs that must
not; a set of positives alone would have tuned the pass toward merging
everything.

It scores 4/5 on must-merge and 4/4 on must-not-merge, identically across runs.
Zero false merges, and the one miss is in the safe direction — the prompt is
built to prefer leaving two findings apart over collapsing two real defects into
one. Findings from the *same* agent are never paired: it had the whole diff in
front of it when it decided they were two things.

The pass costs about $0.012 on a defect-dense diff, roughly 40% on top of a
review, so `REVIEWHIVE_ENABLE_LLM_MERGE=false` is a supported way to run cheaper
rather than a vestige.

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

Cost on `claude-haiku-4-5`: **$0.013 – $0.032 per review**, every figure measured
rather than estimated, across every review this project has actually recorded. The
first end-to-end webhook review was **$0.028 in 10.6 s** — a four-file diff that
produced 25 findings, of which 15 were posted.

It is quoted as a range on purpose. Cost scales with diff size but not only with
it: the four-file `demo_pr` fixture came in *above* the six-file `multi_file` one,
because output tokens vary with how much the agents find rather than with how much
they read. The same diff run twice consumed identical input tokens and differed by
15% on output alone. Both scripts print the exact figure and per-agent latency for
every run.

## Reviewing real pull requests

```bash
pip install -e ".[service,db]"
uvicorn reviewhive.api.app:app --port 8000
```

Point a repository's webhook at it, subscribed to **Pull requests**, **Issue
comments**, and **Pull request review comments**, with the content type set to
**`application/json`**. The other content type sends the body
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

**A 200 that did nothing always says why.** Most deliveries are ignored, and for
good reasons — an edited comment, a draft, a `synchronize` with the flag off, a
redelivery of something already reviewed. Every one of those exits logs the reason
it took, so the service's most common behaviour is readable in the log rather than
inferred from a bare `200 OK`:

```
13:58:50 INFO     reviewhive.api.webhook: ignored: action 'edited' is not a trigger; triggers are ['opened', 'ready_for_review', 'reopened']
13:59:16 INFO     reviewhive.api.webhook: ignored: oelborki/reviewhive-demo#1 is a draft
```

`REVIEWHIVE_LOG_LEVEL` (default `INFO`) applies to this project's loggers only —
dependencies stay at `WARNING`, so `DEBUG` gets you more of the reasoning above
rather than an httpx line per HTTP request.

**Background tasks die with the process.** A deploy or a `--reload` restart
mid-review strands a `running` row. That is the honest cost of running in-process
rather than behind a queue; `mark_running` at least makes the orphan diagnosable
rather than invisible. A worker queue is the scaling path, and deliberately not
built.

## Talking to it

Mention the bot in a comment and it works out what you want:

```
/reviewhive                                    re-review the whole thing
/reviewhive focus on error handling            re-review, narrowed
/reviewhive why is this a problem?             answers in the thread
/reviewhive this is validated upstream         re-judges that one finding
```

**The trigger is `/reviewhive`, not `@reviewhive`, and that is not a style
choice.** `ReviewHive` is a real GitHub account belonging to someone with no
connection to this project, so the `@` form rendered as a link to a stranger's
profile on every comment. It notified nobody only because the demo repository was
private — publishing it would have made each one a genuine ping. A leading slash
cannot be a GitHub username, so it cannot collide with an account that exists now
or is registered later.

**The bot cannot answer comments from the account it runs as.** The self-login
check drops any comment whose sender matches the token's owner, and it is the only
guard that works: a posted review fires one `pull_request_review_comment` delivery
per inline comment — fifteen, measured — every one of them with `sender.type:
"User"` and `author_association: "OWNER"`, because a PAT-driven bot *is* a person
as far as the payload is concerned. Neither a bot-sender filter nor an association
filter can tell the difference. The practical consequence is that the bot needs its
own machine account, or nobody but a second human can talk to it.

There is no command grammar. A cheap `claude-haiku-4-5` call classifies the
comment into one of four actions and the dispatch is ordinary code, so a misread
shows up as a logged rationale rather than as behaviour buried inside a reply. A
bare `/reviewhive` skips the classifier entirely — there is nothing to interpret,
so there is no reason to pay to interpret it.

**Ambiguity resolves toward the cheap error.** A re-review costs money and posts a
second review over the first; an answer costs little and, if it misreads, wastes a
reply. So vague text becomes a question, and so does every classifier failure.

That rule took two attempts. The first version said "if you are unsure, ask a
question" — and the model was never unsure. It confidently read *"anything else?"*
and *"thoughts?"* as re-review requests, at $0.03 each. Keying the rule on what the
comment *asks for* rather than on the model's own confidence fixed it, verified in
both directions: `scripts/probe_intent.py` scores 18/18 across four groups.

**Replies stay in their lane, and the schema enforces it.** The answer path returns
one field. There is no severity, file, or line for a new accusation to occupy, so
"while I was here" has nowhere to go even if the prose wanders. Asked *"while
you're here, is the rest of this file ok?"*, it declines and says why.

**Pushback is judged, not absorbed.** The reconsider path has two opposite failure
modes — caving because it was contradicted, and digging in because it filed the
finding — and each is invisible to the input that catches the other, so
`scripts/probe_mention.py` probes both. Bare assertion, "it's intentional", "it was
copied", "we'll fix it later", and an appeal to experience all leave findings
standing; real context the reviewer could not have seen withdraws them, with the
assumption named. Told a module was test-only when the diff shows it imported by
the application, it cites the import.

**Only the bot's own login stops the loop.** Posting one review fires one
`pull_request_review_comment` delivery per inline comment — fifteen, for the review
above — and every payload reads `sender.type: "User"` with `author_association:
"OWNER"`, because the bot acts as a personal access token belonging to a person. A
bot-type filter and an association filter are both useless against it. The
self-login comparison runs first for that reason; a rate limit caps how often any
one pull request can be made to spend money.

Mention runs are recorded like any other run, so one query still answers what a
pull request cost:

```sql
SELECT source, count(*), sum(total_cost_usd)
FROM reviews WHERE repo_full_name = 'you/demo' GROUP BY source;
```

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

## Running it in a container

```bash
cp .env.example .env        # then fill in the three GitHub values and the API key
docker compose up --build   # service on :8000, Postgres beside it
```

That is the whole setup. The container applies its own migrations before uvicorn
starts, so there is no separate schema step to forget — and forgetting it is not
loud: a schema behind the code raises inside the background task, which means the
review completes, prints, and is never stored.

The image is multi-stage, so the compiler and pip's build machinery stay in the
build stage and only a virtualenv crosses into the runtime image. It installs the
`service` extra rather than `dev`, runs as an unprivileged user, and health-checks
itself with `urllib` rather than carrying `curl` for the purpose. `.dockerignore`
excludes `.env` explicitly: it is gitignored, which says nothing about a Docker
build context, and a credential baked into a layer survives being deleted by a
later step.

Migrations run in the entrypoint rather than a release phase, which is correct for
one replica and wrong for several — concurrent `alembic upgrade head` is a race.
A worker queue and a release phase are the documented scaling path and are
deliberately not built.

To run the database alone — for the CLI, or for `pytest -m db` — `docker compose
up -d db` still works in a clone with no `.env` at all.

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
pytest              # 399 tests, no network, no API spend, no database
ruff check .
pytest -m db        # 30 more, against the compose Postgres
```

The default run never contacts the Anthropic API and never needs infrastructure.
The graph takes its client as a constructor argument, so tests build the real
graph around an in-memory stub (`tests/stubs.py`) with no monkeypatching —
including the timing assertion that proves the fan-out overlaps. Persistence gets
the same treatment: callers depend on a `ReviewStore` protocol, and the unit suite
runs against an in-memory implementation of it.

The GitHub layer gets the same treatment. `signature.py`, `positions.py`,
`client.py`, `jobs.py` and `mentions/` import neither FastAPI nor SQLAlchemy, so
their tests — including the full fetch → review → post path — run on a bare install
with an `httpx.MockTransport` standing in for GitHub. Only the endpoint tests need
the `service` extra, and they skip rather than fail without it.

What the offline suite deliberately cannot tell you is whether a *prompt* works.
That is what `scripts/probe_agent.py`, `probe_intent.py`, `probe_mention.py`,
`probe_merge.py` and `probe_critic.py` are for: each runs one thing against real
phrasings and scores itself, so a prompt change shows which readings moved rather
than whether the pipeline still runs. The two that judge other findings score
themselves against cases taken from the `findings` table, weighted so that the
destructive failure — a merge or a deletion that loses a real finding — costs more
than the harmless one.

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
| 3b | Conversational `/reviewhive` mentions | **Done** |
| 4 | Full test coverage, CI, LLM merge pass | **Done** |
| 5 | Docker, temporary deploy, demo | Container **done**; deploy and recording next |

## Deliberately out of scope

Whole-repository context (diffs only), a public GitHub App with per-install auth,
incremental re-review on force-push, more than three agents, and chunked review of
a single oversized file. Each would add real complexity without changing what this
project demonstrates.

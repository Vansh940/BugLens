# Operations: concurrency, caching and measured limits

This page is deliberately quantitative. Numbers are from `backend/bench.py`'s workloads
(synthetic repo, SQLite on Windows, single process) and exist to show where the ceilings
are, not to promise production throughput.

## The model

Single FastAPI process. The webhook verifies the signature, dedupes the delivery, claims the
`(repo, pr, head_sha)` revision, returns `202`, and runs the review in a background task.

Two independent limits apply:

- **Per review**: `CONCURRENCY = 4` bounds file×lens fan-out inside one review.
- **Per process**: `BUGLENS_MAX_CONCURRENT_REVIEWS` (default `8`) bounds how many reviews
  run at once. Callers above the limit wait rather than spawning work, and reviews of the
  same repo take a per-repo lock so ten pushes to one repo cannot stampede the LLM.

Set `BUGLENS_MAX_CONCURRENT_REVIEWS` from the LLM provider's rate limit, not from CPU: the
engine is I/O-bound on the model. `8` is a conservative default for a single free-tier key
pool.

## State store

The engine keeps its state (idempotency, findings, feedback, code index, file history) in
SQLite in **WAL** mode with a `busy_timeout`, behind a **bounded connection pool** (default
4). WAL lets readers run while a writer commits, the timeout makes a concurrent writer wait
instead of failing, and the pool means concurrent store calls reuse connections instead of
opening one each.

Measured with `backend/bench.py` (median of repeats, Windows laptop, single process):

| operation | before | after |
|---|---|---|
| store read (100 calls) | ~5.8 ms each | 1.0–2.0 ms each |
| 50 concurrent writes | ~3050 ms | ~100 ms |
| write latency p50 / p95 | 24 / 33 ms | ~1.2 / ~5 ms |
| index 300 rows | 21 ms | 5–8 ms |
| build import graph, 2000 files | 9438 ms | 30–50 ms |
| related-file selection, 2000 files | 36 ms | 15–31 ms |
| load 2000-file code index | 22 ms (every review) | 22 ms cold, ~1 ms cached |
| GitHub file fetches per review | 20–30 sequential | concurrent (bounded, 8) |

Run-to-run variance on a laptop is significant (the same measurement can differ by 2x), so
treat these as order-of-magnitude, not as a benchmark suite. Reproduce them with
`python bench.py`, which takes medians for that reason.

The graph number was a genuine algorithmic defect: import resolution rescanned every module
for every import, which is quadratic in repo size. It is now a dictionary lookup, and a
regression test asserts the fast path resolves imports identically to the old algorithm.

Parsing is linear and only ever touches **changed** files (unchanged files are skipped by
content hash), so the per-review cost does not grow with repo size once indexed.

`BUGLENS_SQLITE_POOL` is the one knob that matters if you see store contention. Raising it
helps reads; writes are serialized by SQLite itself regardless.

## Caching

| Layer | Key | TTL | Effect |
|---|---|---|---|
| File review cache | file diff + rules + lenses + context | 7 days | identical re-pushes skip the model entirely |
| Code index | `repo`, `path`, content hash | – | unchanged files are never re-parsed |
| Code graph (in-process) | database + repo + index write counter | until the next index write | avoids re-reading and JSON-parsing the whole index per review |
| LLM / GitHub transport | – | – | one shared keep-alive pool per service, not a client per call |

A sweep runs in-process every `BUGLENS_SWEEP_HOURS` (default 24h) to expire file-cache rows
and drop index history for repos untouched for 30 days, so neither table grows without bound.

Redis is **optional**. With `REDIS_URL` set, rate limits and budgets are shared across
processes; without it they are per-process, which is correct for a single-instance deploy.

## Where the real ceiling is

1. **The LLM provider.** Every finding, every lens and every verify call is a request.
   `consensus: 3` triples the lens cost, so leave it at `1` unless precision matters more
   than spend.
2. **SQLite writes.** One writer at a time. At the measured p50 of ~1–2 ms that is several
   hundred writes/second, and a review performs tens of writes, so a few hundred reviews/hour
   is not write-bound. If you exceed it, the fix is PostgreSQL, not a bigger pool.
3. **Review duration.** Model latency dominates; the verify gate for the top-N findings runs
   concurrently, so a review with 8 findings costs about one gate round-trip, not eight.
4. **Process count.** This is intentionally a single-process design. Running multiple
   workers against one SQLite file is safe (WAL + timeout) but the review admission limit is
   then *per worker*: run one worker, or set `REDIS_URL` so limits are shared.

## Failure behaviour

- Webhook **fails closed** if `GITHUB_WEBHOOK_SECRET` is unset, and rejects bad signatures.
- Deliveries and revisions are deduplicated, so retries are harmless.
- A review never blocks a PR, never posts `REQUEST_CHANGES`, and posts at most one
  consolidated review per revision.
- If the model errors, findings are dropped rather than posted unverified. The
  deterministic secret scanner still reports, because it does not depend on the model.
- **Adapters never fail a review.** Deterministic linters/SAST (`linters:` in `.buglens.yml`)
  run as **local subprocesses** (no Docker, no WSL). A missing binary is skipped, and a
  crashed or timed-out adapter (`BUGLENS_LINTER_TIMEOUT`, default 25s) contributes nothing and
  cannot affect the model findings. The adapters that ran are named in the review footer.
- **A review that could not run says so.** If *every* model call fails, BugLens posts
  "review could not run" and records the run as `incomplete`. It does not report a clean
  pass for a review that never happened.
- **Abandoned runs recover.** A run claimed but never finished (process died, deploy, OOM)
  is reclaimed after `BUGLENS_STALE_RUN_MIN` minutes, so a redelivery is not rejected
  forever as "already reviewed".
- **Feedback cannot be forged.** The hidden fingerprint marker in a comment is
  HMAC-signed with `GITHUB_WEBHOOK_SECRET`, so nobody can plant a marker and drive fake
  👎 to suppress real findings.
- `/engine/stats` answers `503` when `ADMIN_API_KEY` is unset, rather than exposing counters.

## MCP server

BugLens ships a local Model Context Protocol server over stdio, so an MCP client (Claude
Desktop, Cursor, and similar) can call BugLens tools. It is standard-library only: no extra
dependency, no web server, and no required service. It reads the same local SQLite database
as the backend.

Run it from `backend` with that directory's virtualenv:

```bash
python -m buglens.mcp_server
```

Client configuration:

```json
{
  "mcpServers": {
    "buglens": {
      "command": "python",
      "args": ["-m", "buglens.mcp_server"],
      "cwd": "/path/to/backend"
    }
  }
}
```

The server speaks JSON-RPC 2.0 over stdio, one JSON object per line, and implements
`initialize`, `tools/list`, `tools/call` and `ping`. A malformed line is answered with a
JSON-RPC parse error rather than crashing the session.

Six read-only tools:

- `review` with `{owner, repo, number}`: run a review. `dry_run` defaults to `true`, so it
  never posts a comment unless the client explicitly sets `dry_run: false`.
- `findings` with `{repo, number}`: the stored findings for a PR. `repo` is `owner/name`.
- `recall` with `{repo, path}`: prior findings recorded against a file path.
- `context` with `{repo, changed}`: code-graph neighbours of the changed paths plus a
  bounded context pack. `changed` is a list of paths.
- `stats` with `{repo}`: engine stats, verifier drop stats and per-lens outcome stats.
- `repos`: the repos BugLens has seen.

A tool error is reported in-band as `isError: true`, never by crashing the session.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `503` on `/engine/stats` | `ADMIN_API_KEY` not set (expected) |
| Reviews never post | `GITHUB_TOKEN` missing or lacking `repo` scope |
| Webhook `401`s | secret mismatch between GitHub and `GITHUB_WEBHOOK_SECRET` |
| `no such table: runs` | the app was started without its lifespan running (engine store never initialised) |
| First request slow | free-tier hosts sleep; the first request wakes the instance |
| Comment volume too high | lower `max_comments`, raise `min_severity`, or set `skip_nits: true` |
| No adapter findings | the tool is not installed, or `linters` is unset (`[]`). Set `linters: [ruff]` or `auto` and install the binary |
| A finding keeps appearing after fixes | reaction feedback never reached the server. Check the webhook receives `reaction` events |

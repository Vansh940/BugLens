# Configuration

## Backend environment

Copy `backend/.env.example` to `backend/.env`. Every value has a sane default for local
development except the LLM and GitHub credentials.

### LLM (provider-agnostic, OpenAI-compatible)

Anything that speaks `POST /chat/completions` works: Groq, OpenAI, Together, a local
server.

| Variable | Notes |
|---|---|
| `OPENAI_BASE_URL` | e.g. `https://api.groq.com/openai/v1` |
| `OPENAI_API_KEY` | provider key |
| `OPENAI_MODEL` | default `openai/gpt-oss-120b` |
| `GROQ_API_KEY_1..3` | extra keys, rotated automatically on `429` |

For offline development point `OPENAI_BASE_URL` at any local OpenAI-compatible proxy and set
a placeholder key.

### GitHub

| Variable | Notes |
|---|---|
| `GITHUB_TOKEN` | **required** for the PR bot; needs `repo` + `read:org` to read diffs, issues and to post reviews |
| `GITHUB_WEBHOOK_SECRET` | **required**; the webhook is fail-closed without it |

### Storage

| Variable | Notes |
|---|---|
| `BUGLENS_DB` | engine state file (idempotency, findings, feedback, index). Defaults next to the package. |
| `DATABASE_URL` | app database for analytics/history; SQLite by default, PostgreSQL in production |
| `REDIS_URL` | optional. Without it, rate limits and budgets run in memory / no-op |

### Auth and origins

| Variable | Notes |
|---|---|
| `ADMIN_API_KEY` | enables `/api/v1/engine/stats`; unset means that endpoint returns `503` |
| `EXTENSION_SHARED_SECRET` | if unset, the IDE endpoints are unauthenticated (fine for a single-user self-host) |
| `ALLOWED_ORIGINS` | comma-separated CORS origins |
| `ENVIRONMENT` | `production` turns config warnings into hard startup failures |

### Tuning (optional)

| Variable | Default | Effect |
|---|---|---|
| `BUGLENS_MAX_CONCURRENT_REVIEWS` | `8` | reviews running at once in the process |
| `BUGLENS_SQLITE_POOL` | `4` | SQLite connections kept for the engine store |
| `BUGLENS_SQLITE_BUSY_MS` | `5000` | how long a writer waits for the write lock |
| `BUGLENS_LLM_MAX_CONNECTIONS` | `32` | shared HTTP connection pool for LLM calls |
| `BUGLENS_VERIFY_CONCURRENCY` | `4` | model-gate calls in flight per review |
| `BUGLENS_FETCH_CONCURRENCY` | `8` | concurrent GitHub file fetches per review |
| `BUGLENS_STALE_RUN_MIN` | `30` | minutes before an unfinished run may be retried |
| `BUGLENS_SWEEP_HOURS` | `24` | interval between cache/index sweeps |
| `BUGLENS_COST_PER_1M_IN` | unset | $ per 1M input tokens; enables the cost figure in the review footer |
| `BUGLENS_COST_PER_1M_OUT` | unset | $ per 1M output tokens (used with the above) |
| `BUGLENS_LINTER_TIMEOUT` | `25` | seconds before an adapter subprocess is killed |

The footer always reports engine calls, failed calls, tokens (when the provider returns them)
and wall-clock; a **dollar figure appears only when both cost variables are set**, so the bot
never publishes a price it invented.

## Running it

```bash
cd backend
uv venv && uv pip install -r requirements.txt
.venv/Scripts/python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

On Linux/macOS use `.venv/bin/python`. The engine needs no Redis, no queue and no vector
database: SQLite in WAL mode is enough for the state it keeps. See
[operations.md](operations.md) for what that means under load, and what to change first if
you outgrow it.

## VS Code extension

Open Settings (`Ctrl+,`) and search **buglens**.

| Setting | Default | Description |
|---|---|---|
| `buglens.apiUrl` | hosted backend | Point at your own server |
| `buglens.reviewOnSave` | `false` | Review automatically on save |
| `buglens.showInlineHighlights` | `true` | Coloured underlines on problem lines |

The extension identifies itself with a key compiled into the bundle. That is deliberately not
a strong secret (it stops casual scripted abuse, not a determined attacker), so on a
self-hosted instance set `EXTENSION_SHARED_SECRET` only if you also rebuild the extension with
a matching key, or leave it unset.

## Repo rules

`.buglens.yml` at the repo root, plus optional nested files for subtrees. Custom lenses go in
`.buglens/lenses/*.md`. Both are documented in [engine.md](engine.md#repo-rules-buglensyml).

Two keys govern the re-review loop: `max_rounds` (default `10`) stops the bot on a PR that has
been reviewed that many times, and `converge_rounds` (default `3`) switches to *new-code-only*
reviews after that many rounds (deterministic: a file is only re-reviewed if its diff actually
changed). Set either to `0` to disable it.

Two keys govern the summary body and merge behaviour: `pre_merge_checks` (default `true`) adds
a deterministic table to the walkthrough (tests moved, issue linked, secrets, linters) with a
status and a fix per row, and `request_changes` (default `false`) is the only way BugLens can
ever post a blocking review. It posts `REQUEST_CHANGES` **only** when the verdict is `blocking`
(a blocker-severity finding); off, every review is `event=COMMENT`.

Two keys govern what happens to inline comments from earlier rounds (see
[engine.md](engine.md#comment-lifecycle)): `acknowledge_resolved` (default `true`) replies on the
thread and names the commit when a push fixes the finding, and `stale_comments` (default `true`)
collapses a confirmed-gone finding's comment as outdated. Both act only when the quoted evidence
is verifiably absent from the new head; a still-open finding is never touched.

Two keys govern whether a PR gets the extra pre-mortem pass (see
[engine.md](engine.md#pre-mortem-and-depth-tiers)): `depth_triggers` (default `true`) lets a risky
surface (a new route, a migration, an auth/security path, or a large diff) select the `deep` tier,
and `premortem` (default `true`) adds the bounded "what could break in production" section on that
tier. `depth_triggers: false` pins every PR to `quick`; the chosen tier is always named in the
footer.

One more key governs the advisory triage line (see
[engine.md](engine.md#risk-tiers-how-closely-should-a-human-read)): `risk_tiers` (default `true`)
prints `Review depth recommended: L0-L3` at the top of the comment from the findings, the diff
size, whether tests changed, and risky paths. It is advisory only and never blocks.

`surprise_detector` (default `true`) feeds that line: it flags a change that does not match the
kind of work the PR claims to be (a docs PR touching code, a bug fix touching a migration, a UI
change touching auth, a non-feature PR adding a dependency) and raises the tier to at least L2.

Two keys are where a human applies a learned suggestion (see
[engine.md](engine.md#learning-from-feedback)): `ignore_paths:` takes globs for a scope the team
keeps dismissing, and `focus:` takes the lens allow-list, so dropping a down-voted lens means
removing it from `focus:`. The engine only proposes these snippets; it never edits the file. A
lens is proposed for demotion at 5 raised findings with a down-rate of `0.5`, and a dismissal
scope becomes an `ignore_paths:` suggestion at 3 net dismissals.

### Deterministic linters

Run real linters/SAST locally and fold their output in as **ground-truth findings** (they skip
the model gate and are still capped to added lines). Off by default:

```yaml
linters: auto                    # [] = off; "auto" = detect installed; or [ruff, eslint]
post_check_run: true             # one check run per review (off by default)
check_run_conclusion: neutral    # a "failure" conclusion only when required_status_check: true
required_status_check: false
```

Adapters: `ruff`, `bandit` (Python), `eslint`, `node-syntax` (JS/TS), `shellcheck` (Shell),
`hadolint` (Dockerfile), `rubocop` (Ruby), `yamllint`, `php-syntax`. A missing tool is skipped,
never an error. No Docker and no WSL: they run as local subprocesses (timeout
`BUGLENS_LINTER_TIMEOUT`, default 25s). Full detail in
[engine.md](engine.md#deterministic-linters-adapters).

### Check runs and annotations

`post_check_run: true` publishes findings as check-run annotations, so they appear in the GitHub
**Files changed** tab and the Checks UI, not only as review comments. It is **off by default**
and requires installing the bot as a **GitHub App** with `checks:write`: GitHub only lets an App
create check runs, so a token (`GITHUB_TOKEN`) deployment gets a 403 and simply no check run.
Every check-run call is best-effort; a 403 is swallowed and nothing else is affected.

| Key | Default | Notes |
|---|---|---|
| `post_check_run` | `false` | needs a GitHub App with `checks:write` |
| `check_run_name` | `BugLens` | the run name; list this exact string as the required status context |
| `check_run_conclusion` | `neutral` | `failure` needs `required_status_check: true` |
| `required_status_check` | `false` | the only way a `failure` conclusion can be emitted |
| `check_run_input` | `pr_metadata` | run output text: `full_diff`, `incremental`, or `pr_metadata`; annotations are the same in all three |
| `check_run_grouping` | `single` | `single` (one run) or `per_lens` (one run per lens) |
| `check_run_max_annotations` | `200` | findings published, blockers first; the GitHub API takes at most 50 per call |

A `blocker` finding is a `failure` annotation, `major` is `warning`, and `minor` and `nit` are
`notice`; a finding with no line is skipped. `failure` is emitted only when
`required_status_check: true` and the verdict is `blocking`, and a stray `failure` falls back to
`neutral` otherwise. The bot never fails a build on its own. Full detail in
[engine.md](engine.md#check-runs-and-annotations).

### Per-lens caps

`lens_caps` maps a lens name to the maximum number of comments it may contribute to the single
review comment, so one noisy lens cannot consume the whole `max_comments` budget:

```yaml
lens_caps:
  security: 3
  ui: 2
```

It is opt-in and empty by default (`lens_caps: {}`), which means no per-lens cap. The global
`max_comments` (default 12) always applies on top. A cap is clamped to a non-negative integer and
a malformed entry is dropped. The highest-severity findings are kept first; a lens over its cap
has its lowest-ranked findings dropped, and the footer names each capped lens as
`lens caps: <lens> N`. Full detail in
[engine.md](engine.md#code-objects-and-per-lens-caps).

### External context

`external_context` pulls optional outside material into the review: ticket bodies (Jira, Linear)
and production error signals (Sentry, Datadog, PostHog). It is **optional and off by default**;
list the providers you want in `.buglens.yml`:

```yaml
external_context:
  - jira
  - sentry
```

Accepted names are `jira`, `linear`, `sentry`, `datadog` and `posthog`; any other name is
dropped, duplicates are removed and order is preserved. Each provider reads its own
environment variables, so enable one only when those are set:

| Provider | Variables |
|---|---|
| `jira` | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_TOKEN` |
| `linear` | `LINEAR_API_KEY` |
| `sentry` | `SENTRY_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT` (optional), `SENTRY_BASE_URL` (optional, self-hosted) |
| `datadog` | `DD_API_KEY`, `DD_APP_KEY`, `DD_SITE` (optional) |
| `posthog` | `POSTHOG_API_KEY`, `POSTHOG_PROJECT_ID`, `POSTHOG_BASE_URL` (optional, self-hosted) |

It adds no required service. A missing token is a silent no-op, and a failed call is skipped
without ever failing the review. Tokens are read from the environment only and are never logged.

## Supported languages

Python, JavaScript, TypeScript, Java, Go, Rust, C, C++, C#, Ruby, PHP, Swift, Dart, Kotlin,
Scala, HTML, CSS, SCSS, SQL, Bash, Shell, YAML, JSON, Elixir, Haskell, Clojure, R, Julia, Lua
and more. The diff parser is language-agnostic; the code graph uses Python's `ast` and a
regex fallback elsewhere. With `linters` enabled, the adapters above run deterministic
linters/SAST on top of the model for the languages they cover.

# The review engine

The PR bot and the IDE both call one engine (`backend/buglens/`). This page explains what
it actually does to your diff.

## Pipeline

1. **Fetch** the PR: paginated file list, base/head SHAs, linked issues, and the target
   file contents when snippets are needed.
2. **Parse** the unified diff into per-file added lines with **new-file** line numbers.
   Everything downstream refers to those numbers.
3. **Index** the repo. One JSON record per file (defs, imports, refs, doc), keyed by content
   hash, stored in SQLite. Only changed files are re-parsed.
4. **Build context**: the import graph, reverse dependencies, related tests, and a
   personalized PageRank ranking pick up to N related files into a bounded context pack.
5. **Recall** prior findings and prior PRs for the changed files, plus their outcomes.
6. **Review** through lenses. Each lens sees the diff, the repo rules, the code graph pack
   and the recall block. Findings are normalized, de-duplicated by fingerprint, and capped
   by severity budget. Deterministic **adapters** (local linters/SAST, when enabled) run
   alongside the lenses and contribute ground-truth findings with no model call.
7. **Verify**: a machine gate (the line exists in the diff, the quoted evidence is actually
   present, and a drifted anchor is snapped back to its evidence line with the `line_drift`
   recorded), then a bounded model gate (concurrent, top-N only).
8. **Post** one consolidated review: inline comments with suggestion blocks, plus a summary
   comment carrying the walkthrough.
9. **Measure** on merge: re-read the merged files and record whether each finding was fixed.

## Lenses

| Lens | Looks for |
|---|---|
| `correctness` | logic errors, null/undefined, off-by-one, resource leaks, error handling |
| `security` | injection, authz gaps, unsafe deserialization, SSRF, secrets in code |
| `performance` | N+1 queries, blocking I/O, needless allocation, hot-path work |
| `ui` | accessibility, state bugs, layout/reflow, missing loading and error states |
| `tests` | missing or weak coverage for the change, tests asserting nothing |
| `maintainability` | dead code, duplication, unclear names, but only when it matters |
| `merge` | merge-readiness and, when a ticket is linked, spec compliance |

A lens can be scoped to file extensions and severity, and you can add your own. See
**Custom lenses** below.

## Verification, or why you get fewer comments

BugLens is tuned for precision over volume. A finding is dropped unless:

- the quoted **evidence text exists in the added lines** of that file, and
- the line number refers to a line the PR actually added, and
- it passes the model gate (an independent call that argues the finding is false), and
- it survives suppression: two net 👎 from your team on that fingerprint and it never
  appears again.

Three classes bypass the model gate because they are ground truth:

- **Deterministic secret scanner**: regex hits on added lines. The value is masked in the
  output and never sent to the model.
- **Adapter hits**: local linter/SAST output (see **Deterministic linters** below) carries
  `deterministic=true` and is still capped to added lines.
- **Ticket criteria**: assertions about your own acceptance criteria, not code claims.

If a finding's line has drifted since it was anchored (force-push, rebase, retarget), the
machine gate **re-anchors it to the line that actually holds the quoted evidence** and records
the signed `line_drift`; only if the quote is not in the diff at all is the finding dropped.
A real finding is never lost and never posted on the wrong line.

**One defect, one comment.** The same problem often surfaces in more than one place: the PR-level
`merge` lens restates a finding a file lens already made, and a security-flavoured bug is raised
by both `security` and `correctness`. Before posting, findings are deduplicated by normalized
evidence (falling back to `path:line:category`, then `path:title`). The kept finding lists every
lens that found it (`lens: correctness, security`) and the other locations it appeared at
(`also reported at app/admin.py:9`); the count of merged duplicates is named in the footer as
`merged N duplicate finding(s)`. This is the difference between a review and a comment storm.

`consensus: 3` in `.buglens.yml` runs three shuffled passes and posts only what wins the
majority. It costs 3x tokens and is off by default.

## Chat and commands

The review is a broadcast; a mention is a conversation. Comment anywhere on the PR (or reply
in an inline review thread) and BugLens answers there, reusing the same context the review
used: a digest of the added lines, the code graph block and the findings it already posted.

| Comment | What happens |
|---|---|
| `@buglens review` / `@buglens re-review` / `@buglens` | runs a fresh review (on demand) |
| `@buglens explain` | a deterministic Change-Stack narrative of what the PR changes |
| `@buglens why` | why a finding matters, grounded in its evidence |
| `@buglens fix` | a concrete fix plus a copy-ready Fix-All prompt for Claude Code, Cursor or Codex |
| `@buglens <question>` | answers the question |

Routing and the Change-Stack narrative are deterministic; only the prose is one bounded model
call. If the model is unreachable the reply says so and falls back to what BugLens knows
without it, rather than inventing an answer. A known verb wins, so `@buglens why did the
review flag this` is a question, while `hey @buglens please review` re-runs the review.

## Deterministic linters (adapters)

Beyond the model, the engine can run real linters/SAST on the added lines and fold their
output in as findings. These are **ground truth**: `deterministic=true`, confidence `1.0`,
and they skip the model gate, so a real bug a tool finds is never argued away by the model.
They are still capped to lines the PR added, so nothing lands on unchanged code.

All of them are **optional and off by default**:

```yaml
linters: auto            # [] = off (default); "auto" = any that are installed; or name them
```

| Adapter | Languages | Notes |
|---|---|---|
| `ruff` | Python | lint + a broad rule set |
| `bandit` | Python | security (SAST) |
| `eslint` | JavaScript, TypeScript | lint |
| `node-syntax` | JavaScript | parse-only syntax check |
| `shellcheck` | Shell, Bash | shell correctness |
| `hadolint` | Dockerfile | Dockerfile lint |
| `rubocop` | Ruby | lint |
| `yamllint` | YAML | lint |
| `php-syntax` | PHP | parse-only syntax check |

Rules:

- **Missing tool = no-op.** If an adapter's binary is not on `PATH`, it is skipped; it never
  errors and never fails a review.
- **Bounded and shell-free.** Each run is a direct subprocess with no shell and a hard timeout
  (`BUGLENS_LINTER_TIMEOUT`, default 25s), executed off the event loop so a slow linter cannot
  stall other reviews.
- **Stable fingerprints.** An adapter hit is fingerprinted on `(file, rule, line)` so it is
  de-duplicated and suppressed like any other finding.
- The set that ran is disclosed in the footer as `linters: ruff, eslint`.

## Ticket and spec validation

If a PR says `Closes #42`, the engine resolves the issue (GitHub GraphQL
`closingIssuesReferences`, with a keyword scan of the PR body and commits as fallback),
extracts the acceptance criteria, and reports each as `addressed` / `partial` /
`not_addressed` / `unclear` with the `file:line` that proves it. Unmet criteria become at
most three findings. The verdict is in the walkthrough. Disable with `check_ticket: false`.

## Code graph

The index is deliberately boring: standard-library `ast` for Python, regex for everything
else. No tree-sitter, no external indexer, no vector database, no required service.

It gives each review:

- direct dependencies of the changed files,
- files that import them (who you just broke),
- their tests,
- related files ranked by personalized PageRank seeded on the change.

## Symbol graph and ripple analysis

The code graph says which files relate. The symbol graph says which *symbols* changed and who
calls them. When a PR edits a definition, the engine lists the in-repo callers of that name and
hands the list to the lenses, so a reviewer reasoning about a function knows it has call sites
elsewhere.

Ripple analysis is deterministic and needs no model. It fires one finding in the case that is a
genuine cross-file inconsistency rather than a hint: a changed definition that now takes a
**required** argument (no default) while callers still exist. That candidate still has to pass
the normal verify gate, so it posts only when it holds up.

To see callers, the engine fetches up to six related file bodies (the files PageRank ranked
highest) once per review and indexes their references; the fetch is skipped when the repo is not
reachable over the API. The footer reports `ripple: N changed symbol(s) with callers` when the
analysis had something to chew on. Nothing here blocks a PR or runs a new service.

## Recall: not repeating itself

For every changed file the engine looks up findings it posted before, and their outcomes:

- `fixed`: you fixed it after the review,
- `dismissed`: your team down-voted it,
- `agreed`: up-voted,
- `open`: still stands.

Open items are restated compactly; dismissed ones are suppressed; fixed ones are counted so
the review does not re-litigate. Prior PRs that touched the same files add one line each.

Recall is a **PR-bot** behaviour and only that. The IDE review of a file (`POST /api/v1/review`,
the extension) is a fresh question about the code in front of you, so it never subtracts what a
past review said: a bug that is still there is reported again.

## Learning from feedback

Reactions and IDE feedback do more than suppress one fingerprint. The engine aggregates them per
lens and per path scope, and the result shapes the next review. All of it is local and
deterministic: no model call builds any of it.

- **Learned advisories.** The review prompt gets a bounded block headed
  `Feedback on this repo (learned from reactions; do not repeat dismissed noise)`. It lists the
  scopes where the team keeps dismissing a lens and names the most down-voted lenses. The block
  is built from stored feedback, not from a model call.
- **Adaptive lens ordering.** A lens with at least 5 raised findings and a down-rate of `0.5` or
  higher is **demoted**: it moves to the end of the lens order for each file, never removed, so a
  budget-limited review prefers lenses this repo has not been rejecting. When at least one lens is
  demoted for a review, the footer says so: `adaptive: N lens(es) demoted by feedback`. Nothing
  is turned off silently.
- **Candidate rules, human-approved.** The engine proposes rule snippets: an `ignore_paths:` glob
  for a scope with repeated dismissals (at least 3 net dismissals), or dropping a down-voted lens
  from `focus:`. It never applies them. They are read back over the API (see
  [api.md](api.md#engine-introspection)), and a human copies what they agree with into
  `.buglens.yml`; both keys are documented in
  [configuration.md](configuration.md#repo-rules).
- **Verifier rationale.** Every candidate the verify gate drops is stored with a short reason
  bucket: `machine`, `model`, `suppressed by prior feedback` or `unknown`, so the drop rate can be
  explained rather than guessed. The counts are exposed as `drops` on the stats endpoint.

It stays on SQLite: no Redis, no Docker, no WSL, and it never blocks a PR.

## Check runs and annotations

The PR bot can publish its findings as a **check run** as well as review comments, so each one
shows up in the GitHub **Files changed** tab and the Checks UI. It is opt-in
(`post_check_run: true`, off by default) and needs the bot installed as a **GitHub App** with
`checks:write`: GitHub only lets an App create check runs, so a `GITHUB_TOKEN` deployment gets a
403. That 403 is swallowed, the review is unaffected, and a token-based deployment simply does
not get the check run. Every check-run call is best-effort. The keys are in
[configuration.md](configuration.md#check-runs-and-annotations).

**Annotations.** Every finding with a resolvable line becomes an annotation: the file `path`, the
finding `line` as both start and end line, a level, and a message built from the finding title
plus its body and evidence. A finding with no line (a PR-level finding) cannot be annotated and
is skipped. The level maps from severity:

| Finding severity | Annotation level |
|---|---|
| `blocker` | `failure` |
| `major` | `warning` |
| `minor` | `notice` |
| `nit` | `notice` |

An annotation level of `failure` is display only: it does not set the check conclusion and does
not block a merge. Only the run conclusion, and only when the check is required, can do that.

**Cap and chunking.** At most `check_run_max_annotations` findings are published (default 200,
blockers first). The GitHub API accepts at most 50 annotations per create or update call, so they
go out in chunks of 50: the first chunk in the create call, each later chunk in a separate update
that appends to the run rather than replacing it. Findings over the cap are counted and named in
the run summary as `annotations: N shown, M over cap`.

**One run by default.** `check_run_grouping: single` (default) publishes one run named
`check_run_name`; `per_lens` publishes one run per lens, named `<check_run_name> (<lens>)`, for
teams that want that.

**Three input modes.** `check_run_input` sets only what goes into the run's output text;
annotations are identical in all three. `pr_metadata` (default) writes a short summary of status,
score, verdict, files and findings. `incremental` adds the lines this revision added. `full_diff`
adds the whole unified diff of the changed files, truncated at the output cap with a
`diff truncated (N files)` marker when cut.

**Conclusion, never blocking by default.** `failure` is set only when
`required_status_check: true` **and** the verdict is `blocking`; otherwise the configured
`check_run_conclusion` (default `neutral`) is used, and a stray `failure` falls back to `neutral`
when `required_status_check` is false. The bot never fails a build on its own.

## Code objects and per-lens caps

**Code objects.** The engine groups each file's added lines by the symbol they belong to, using
the same signature detection as ripple analysis. Every posted finding is tagged with its
enclosing symbol, and the footer reports the depth as `code objects: N unit(s) across M file(s)`.
A line that precedes any definition belongs to the file itself, tagged `<file>`. This is
deterministic and costs no model call.

**Per-lens caps.** A `lens_caps` key in `.buglens.yml` maps a lens name to the maximum number of
comments it may contribute to the single review comment, so one noisy lens cannot consume the
whole `max_comments` budget:

```yaml
lens_caps:
  security: 3
  ui: 2
```

It is opt-in and empty by default (`lens_caps: {}`), which means no per-lens cap; the global
`max_comments` (default 12) always applies on top. A cap is clamped to a non-negative integer and
a malformed entry is dropped. The highest-severity findings are kept first (the normal
severity-then-confidence order); a lens over its cap has its lowest-ranked findings dropped, and
the footer discloses each capped lens as `lens caps: <lens> N`. Both additions are deterministic
and run offline. The key is documented in
[configuration.md](configuration.md#per-lens-caps).

## External context

The engine can fold in outside material about the change: ticket text (Jira, Linear) and
production error signals (Sentry, Datadog, PostHog). Ticket text is matched against the changed
symbols (see **Code objects** above), so the prompt can tie a reported issue to the symbols it
touches rather than treating it as free-floating prose.

The providers are listed under `external_context:` and each reads its own environment variables
(see [configuration.md](configuration.md#external-context)). It is **off by default**, adds no
required service, and is fail-soft: a missing token or a failed call is skipped and never fails
the review.

## Repo rules (`.buglens.yml`)

Rules are read from the PR's **base branch**, so a PR cannot change how it is reviewed.
Nested config files apply to their subtree and the nearest one wins.

```yaml
max_comments: 10
min_severity: low
verify: true
skip_nits: true           # no formatting / import-order noise
consensus: 1              # 2-3 = shuffled passes + majority vote
linters: auto             # [] = off; "auto" = detect installed; or [ruff, eslint]
post_check_run: true      # add a neutral BugLens check run
check_run_conclusion: neutral   # failure only when required_status_check: true
required_status_check: false
pre_merge_checks: true    # deterministic pre-merge table in the walkthrough
request_changes: false    # opt-in: REQUEST_CHANGES when the verdict is blocking
acknowledge_resolved: true  # reply on a thread when a push fixes the finding
stale_comments: true      # collapse a confirmed-gone finding's comment as outdated
check_ticket: true        # validate linked issues against the diff
max_rounds: 10            # stop after this many reviews of one PR (0 = no cap)
converge_rounds: 3        # from round 4 only re-review files whose diff changed
ignore_paths: ["*.lock", "dist/*"]
custom_rules:
  - "Never call an external API without an explicit timeout."
```

Instruction files (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.cursor/rules/*.mdc`,
`.github/copilot-instructions.md`) are also picked up, and their bullets become review
rules for the files they cover.

`request_changes` is the **only** setting that can ever post a blocking review. It defaults
to `false`, and even when on it fires only on a `blocking` verdict (a blocker-severity
finding). With it off, every review is `event=COMMENT` and never gates a merge.

## Custom lenses

Drop a markdown file in `.buglens/lenses/`. It runs only on matching files.

```markdown
# API Resilience
exts: py,ts
category: performance
Flag any HTTP call without a timeout, and retries without backoff. Cite the call line.
```

## Feedback loop

- **In the editor or on GitHub**: react 👍/👎 on a posted finding. Each comment carries a
  hidden `<!-- buglens:fp=… -->` marker, so no client is needed. The marker is
  HMAC-signed with `GITHUB_WEBHOOK_SECRET`, which means a forged marker cannot drive fake
  votes to suppress findings.
- **Programmatically**: `POST /api/v1/feedback` with the fingerprint.
- Two net 👎 suppress that fingerprint. 👍 becomes a confirmed-good example injected into
  future prompts.
- On merge, the resolution rate per repo is recorded and exposed at
  `GET /api/v1/engine/repos`.

## Walkthrough

Every PR summary includes a collapsible walkthrough: commits, review effort (1–5), findings
by severity, the ticket verdict, and **which context was used** (code graph file count, linked
ticket, prior reviews) with the base SHA reviewed.

**Change stack.** Instead of a flat file list, the changed files are grouped into ordered
chapters (Schema & migrations → Data models & types → Core logic → API & routes → UI →
Config & infra → Tests → Docs), each with a one-line note: file count, how many are new, and
any findings with their titles. This is pure path heuristics (`walkthrough.py`), no model
call, so it reads like a reviewable stack for free.

**Pre-merge checks.** A deterministic table inside the walkthrough: whether test files moved with
code changes, whether an issue is linked, whether secrets were flagged, and which linters ran.
Each row carries a status (Pass / Warn / Info) and, when a check warns, the concrete fix. It
states only what the engine actually observed. Turn it off with `pre_merge_checks: false`. It is
informational and never blocks.

**Review header.** The summary opens with the verdict and the one-line reason for it, then the
model's `confidence` (0–100) and two independent risk axes: `merge` risk (does this change break
something) and `security` risk. The reason must cite the findings and the risks must follow them;
a clean review says so rather than inventing tension. The header also names the commit the review
is scoped to (`checked up to <sha7>`), so a finding always has a claim you can re-check.

## Comment lifecycle

Inline comments from earlier rounds are tracked (fingerprint, path, comment id, node id, the SHA
they were posted at). On a new push BugLens reconciles them against the new head:

- **Addressed.** If the quoted evidence is gone from the file, the thread gets a short reply
  naming the commit (`fixed in <sha7>`); the comment is not repeated. Turn this off with
  `acknowledge_resolved: false`.
- **Stale.** Only a finding whose evidence is confirmed gone is collapsed as outdated
  (`stale_comments`, default on). A finding that is merely absent from this round (budget,
  dedup, a quieter lens) is still open and is left untouched. A failed file fetch is treated as
  unknown, never as resolved, so a network blip cannot retract a real finding.
- **Still open.** Left exactly as it is; the new review does not restate it.

The number of threads acknowledged and collapsed is named in the footer
(`addressed findings acknowledged: N`, `stale comments retired: N`). This is how a long-lived PR
stays readable instead of accumulating a wall of half-true comments.

## Pre-mortem and depth tiers

Some diffs carry more production risk than others. BugLens scores that deterministically and
spends one extra model call only where it is worth it:

- **Triggers.** `premortem.triggers` fires on a whole **path token**, never a substring, so
  `author.py` never trips the auth rule: a **new route** (`route`/`controller`/`handler`/`endpoint`,
  in a file that is added or renamed), a **migration** (`migration`/`alembic`), an
  **auth/security path** (`auth`/`token`/`session`/`password`/`permission`/`crypto`/`security`), or
  a **large change** (200+ added lines).
- **Tier.** Any trigger selects the `deep` tier, otherwise `quick`. The tier is always named in
  the footer (`review depth: deep (triggers: new route, auth/security path)`), and
  `depth_triggers: false` pins a repo to `quick`.
- **Pre-mortem.** On `deep`, one bounded call asks the model to assume the PR merged and caused an
  incident, then name 2-4 concrete failure modes. **Every scenario must cite a path and an added
  line the PR actually touched**: `premortem.select` drops anything unanchored, so a fabricated
  line never ships and a scenario is never relocated onto real code. With nothing anchored, there
  is no section at all. `premortem: false` turns it off; `MAX_ITEMS` is 4.

The section renders collapsed (`Pre-mortem: what could break in production`) under the
walkthrough, and the count is disclosed in the footer (`pre-mortem: N scenario(s)`).

## Risk tiers (how closely should a human read)

The top of a review carries one advisory line: `Review depth recommended: L3 - read closely`. It
is composed from signals the engine already has, so it costs nothing and can never contradict the
findings below it. It is **advisory**: it never blocks and never decides which passes run (that is
the depth tier above).

- **L0** nothing to read (no code changed).
- **L1** a small, low-risk change with a test change alongside it.
- **L2** a medium-severity finding, a large diff (200+ added lines), or weak evidence (code
  changed with no test change).
- **L3** a high/blocker finding, a secret the scanner flagged, a schema migration, or a
  payment/auth/security path.

The reasons are printed with the line (for example `auth/security path: src/auth/login.py,
weak evidence: code changed, no test change`). `risk_tiers: false` turns the line off; the tier is
still computed for the API surface.

### Surprises

The highest-value human review move is asking why a change does not belong. `surprise.detect`
classifies the PR from its title (`fix`, `feature`, `refactor`, `docs`, `test`, `chore`) and flags
a mismatch with the actual files, deterministically and for free:

- a docs PR that changes code, or a tests-only PR that changes product code;
- a refactor or a bug fix that touches a schema migration;
- a UI change that touches a payment/auth/security path;
- a fix/refactor/docs/test PR that **adds a dependency** manifest.

A surprise is named in the tier reasons (`surprise: UI change touches an auth/security path: ...`)
and raises the tier to at least L2. `surprise_detector: false` turns it off. It never blocks and
never changes which passes run.

## Cost and disclosure

Every consolidated review ends with a compact `Engine:` line: engine calls, failed calls,
tokens (only if the provider reported them), an optional dollar figure (only if
`BUGLENS_COST_PER_1M_IN`/`_OUT` are set) and wall-clock, plus a plain line naming every
optional layer that changed review depth (consensus passes, verify drop stats, secret scan,
adapter linters, linked-ticket check, code graph, prior-review recall, and any partial call
failures). Nothing that changes how deep we looked happens silently, and no number is invented.

Re-review discipline keeps the loop bounded: past `converge_rounds` a PR is only re-reviewed
where the diff actually changed (deterministic content hash, **no model calls** when nothing
did), and past `max_rounds` BugLens posts a round-cap note and stops.

### When a review cannot run

If the engine cannot even fetch the PR (a `403`, a rate limit, a `5xx`), BugLens posts a short
note saying the review could not run and that this is **not** a clean review, with the reason and
a retry hint (`@buglens review`). It never leaves the PR looking reviewed when nothing was read,
and it never leaves it silently blank. The note is best-effort: under a hard rate limit the post
request is refused too, so the failure is also logged. This is a distinct marker from the review
comment, so it upserts in place and never clobbers the last real review.

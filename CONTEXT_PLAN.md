# CONTEXT_PLAN.md: making BugLens codebase-aware

The gap: Bugbot/Greptile/CodeRabbit reason over a **persistent codebase graph**, **linked
tickets**, and **prior review conversations**. BugLens (v2) only had a per-run 1-hop import
scan. This is the plan to close it, constrained by the locked rules (one process, SQLite
default, no new required services, no vector DB, provider-agnostic LLM).

Research provenance: `../temp/intel/` + the deep-research reports (Bugbot blog/docs,
Greptile docs/blogs, CodeRabbit docs, GitHub REST/GraphQL docs, Aider repo-map, SCIP).

---

## What the leaders actually do (facts we will copy)

| Source | Mechanism | Copy? |
|---|---|---|
| Bugbot | agentic loop pulls context at runtime; rules from `.cursor/BUGBOT.md` found by walking **upward** from changed files; config read from **base branch**; LLM resolution judge at merge; dedupe vs prior runs | **Yes**: upward rules, base-branch config, resolution judge (done), prior-run dedupe (done) |
| Greptile | full code **graph** (files/functions/classes/vars + call/import edges) + per-node docstrings embedded; agentic retrieval loop; knowledge base incl. **reverts/incidents**; ticket ingestion (Jira/Linear) | **Partly**: graph yes (no embeddings), reverse-deps, ticket text |
| CodeRabbit | **Codegraph** = definitions/references map + co-change history; **linked-issue validation** (Addressed/Not/Unclear) wired into pre-merge checks; `path_instructions`; learnings (scoped, vector-searched); linters merged then FP-filtered; walkthrough | **Yes**: linked-issue verdicts, path instructions, recall of learnings/dismissals |

**Deliberately not copied:** embeddings/vector search (needs a new service and an embedding
provider); tree-sitter (offline-install risk); 50+ linters (each is a sandbox service);
persistent docstring generation per node (cost). Our thesis: **a cheap deterministic graph +
ticket text + our own review history captures most of the value** the leaders get from their
heavy indexers.

---

## Architecture

```
GitHub webhook
   │
   ├─ indexer.py ─────────► SQLite: repo_files / symbols / imports / refs / tests / file_prs
   │      (incremental, per file we already fetch)
   │
   ├─ tickets.py ─────────► GraphQL closingIssuesReferences → issue title/body/acceptance
   │
   ├─ recall.py ──────────► prior BugLens findings on these paths + dismissals → prompt block
   │
   └─ pipeline.review_pr ─► context pack (rules + graph neighbors + tests + ticket + recall)
                             → lenses (+ spec lens) → verify → walkthrough summary
```

Everything is one SQLite file, one process. No embeddings, no new services.

### New tables (SQLite, WAL)
```sql
CREATE TABLE repo_files   (repo TEXT, path TEXT, lang TEXT, sha256 TEXT, parsed_at TEXT,
                           PRIMARY KEY(repo, path));
CREATE TABLE repo_symbols (repo TEXT, path TEXT, name TEXT, kind TEXT,
                           start_line INT, signature TEXT);
CREATE TABLE repo_imports (repo TEXT, src TEXT, dst TEXT, raw TEXT);   -- dst NULL = external
CREATE TABLE repo_refs    (repo TEXT, src TEXT, name TEXT, line INT);
CREATE TABLE repo_tests   (repo TEXT, test_path TEXT, source_path TEXT,
                           strategy TEXT, confidence REAL);
CREATE TABLE file_prs     (repo TEXT, path TEXT, pr INT, title TEXT, merged_at TEXT);
```
Reverse deps = `SELECT src FROM repo_imports WHERE dst=?`.
Test map = filename convention + reverse-import BFS, stored with confidence.
Selection = **personalized PageRank** over the import graph (Aider's repo-map), seeded by
the diff files, pure-Python power iteration (no networkx dependency).

---

## Phases (each ships with tests + an ITERATION.md row)

**Phase A: Ticket/spec context (iteration 9)** ✅ **SHIPPED**
- `buglens/tickets.py`: `parse_closing_refs(text)` (GitHub keyword set, code-fence aware),
  `closing_issues()` via GraphQL, `get_issue()` REST, acceptance-criteria extraction.
- `github.py`: add `graphql(query, variables)` + `closing_issues`, `get_issue`,
  `get_pr_commit_messages`, `get_review_comments`, `get_commits_for_path`, `pulls_for_commit`
  (the history helpers are in place for phase C/D).
- `prompts.py`: `SPEC_SYSTEM`: verdicts `addressed | partial | not_addressed | unclear`,
  must cite file:line, forbidden to invent requirements.
- pipeline: fetch spec, add a `## Ticket` block to context, run a PR-level **spec lens**
  that emits at most 3 findings for unmet/partial criteria, and a `Ticket:` line in the
  walkthrough. Rule `check_ticket: true` (default). 80 tests pass.

**Phase B: Persistent code graph (iteration 10)** ✅ **SHIPPED**
- `buglens/indexer.py`: `parse_text(path, text)` (stdlib `ast` for Python: defs/classes/
  imports/calls; regex fallback other languages), `index_files(repo, {path: text}, db)`
  (content-hash incremental + `prune`), `build_graph`, `reverse_deps`, `tests_for`,
  `pagerank`, `related_files`, `context_pack`.
- Storage: one JSON record per file in `repo_index` (plus `file_prs` for phase C): same
  information as the planned node tables, far less churn.
- pipeline: indexes every text it fetches (changed files + their import targets), replaces
  the flat related-file block with the ranked context pack, salts the file cache with it,
  and reports `code graph (N files)` in "Context used". 90 tests pass.

**Phase C: Prior-review + prior-PR recall (iteration 11)** ✅ **SHIPPED**
- `buglens/recall.py`: `prior_findings(repo, paths)` (posted, joined to outcomes),
  `dismissed(repo)`, `remember_history(repo, paths)` → `file_prs`, `recall_block(...)`
  rendered to a ≤8-bullet block under a hard char budget.
- `github.py`: `get_commits_for_path` + `pulls_for_commit` (REST, capped at 5 files/3 PRs per
  review, skipped when history is cached).
- store: `findings_for_paths`, `record_file_pr`, `recent_prs_for_file`.
- pipeline: injects the recall block; dismissed fingerprints stay suppressed (existing);
  bonus fix: **all store keys normalized to `owner/repo`** so suppression/resolution/
  recall finally read the same rows the reaction webhook writes. 97 tests pass.

**Phase D: Nested rules + base-branch config (iteration 12)** ✅ **SHIPPED**
- `rules.py`: `config_candidates(changed_paths)` discovers `.buglens.yml`/`AGENTS.md`
  **upward** from the changed files (root first, nearest last → nearest wins);
  `load_rules` applies configs in order; instruction files become `custom_rules`.
- pipeline: `load_rules_for_pr(ref=base)`, config is read from the **base commit**, so a
  PR cannot change how it is reviewed; two passes (root, then nested once paths are known)
  with a per-run cache. Explicit `rules=` still bypasses loading.

**Phase E: IDE repo-awareness + dashboard (iteration 13)** ✅ **SHIPPED**
- extension: parses `.git/config` for `repo`, `.git/HEAD` for `branch`, extracts the file's
  import specs, and reads up to 5 local imported modules; all four are sent with `/review`.
- backend: `review_snippet(repo=, related=, imports=)` indexes them and injects the ranked
  code-graph pack + prior-review block; `ReviewRequest` gained the optional fields.
- `store.known_repos()` + `GET /api/v1/engine/repos` (runs, posted, resolution) for the
  dashboard repo picker. 105 tests pass, `tsc` clean.

---

## Risks / mitigations
- **Comment volume** (MSR'26: signal-to-noise drives abandonment) → every phase adds
  context but no new *comment category* except the ticket section, which is summary-only.
- **Cost** → graph parse is CPU-only; context packs are budget-capped; file cache (iter 7)
  already skips the LLM on identical diffs.
- **Offline** → stdlib `ast` + regex only; no tree-sitter, no embeddings.
- **Privacy** → we index only files we already fetch to review; delete on
  `repo_files` invalidation when files change; nothing leaves the process except the prompt.
- **Token budget** → `context.prompt_block` hard cap (6k) stays; recall ≤800 chars;
  ticket ≤1200 chars.

## Definition of done (per phase)
pytest green (new unit + integration fakes), `tsc` green (if touched), boot smoke green,
`ITERATION.md` row added, `AGENTS.md` architecture table updated.

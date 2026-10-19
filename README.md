<div align="center">

<img src="vscode-extension/icon.png" width="112" alt="BugLens"/>

# BugLens

**AI code review that survives scrutiny.** Diff-native, evidence-gated, and it remembers
what it already told you.

[Install](https://marketplace.visualstudio.com/items?itemName=VanshDev.buglens) ·
[API docs](https://buglens-api.onrender.com/docs) ·
[Engine](docs/engine.md) · [Self-hosting](docs/configuration.md) · [Report a bug](https://github.com/Vansh940/buglens/issues)

</div>

---

Three surfaces, one review engine:

- **VS Code extension**: `Ctrl+Shift+R` reviews the current file or selection, with inline
  highlights and hover fixes.
- **GitHub PR bot**: reviews every pull request, posts one consolidated review with
  apply-in-one-click suggestion blocks. It never blocks a PR.
- **Dashboard + API**: `/api/v1/review`, history, and engine stats.
- **Local MCP server**: read-only tools over stdio for MCP clients such as Claude Desktop and
  Cursor. See [operations.md](docs/operations.md#mcp-server).

## Install

```bash
code --install-extension VanshDev.buglens
```

Or: `Ctrl+Shift+X` → search **BugLens** → Install. Then open a file and press `Ctrl+Shift+R`.

## Use

| Action | Shortcut |
|---|---|
| Review file or selection | `Ctrl+Shift+R` / `Cmd+Shift+R` |
| Toolbar | magnifier icon in the editor title bar |
| Right-click | **BugLens: Review This Code** |
| Re-review a PR on demand | comment `@buglens review` |
| Ask about the PR | comment `@buglens why`, `@buglens explain`, or any question |
| Get a fix | comment `@buglens fix` for a copy-ready Fix-All prompt |
| See what BugLens learned for a repo | `GET /api/v1/engine/learn?repo=owner/name` |

Point the extension at your own backend with the `buglens.apiUrl` setting.

## Why the output is trustworthy

- **Diff-native.** Every finding is anchored to an added line and quoted with evidence. No
  line, no finding.
- **Two-gate verification.** A machine gate (line exists in the diff, evidence actually
  present) plus a bounded model gate. Deterministic hits (secrets) skip both gates.
- **Real tools, not vibes.** Turn on `linters:` and local linters/SAST (ruff, bandit, eslint,
  shellcheck, hadolint and more) fold their output in as ground-truth findings the model cannot
  argue away. No Docker, no WSL, missing tools are simply skipped.
- **Reads as a stack, not a dump.** The walkthrough groups the diff into ordered chapters
  (schema, models, logic, api, ui, config, tests, docs) with a deterministic pre-merge table
  (status + fix per check), so you review the change bottom-up.
- **Says what it thinks, in one line.** Every review opens with the verdict and why, a
  confidence score, and separate merge and security risk, scoped to a named commit. Findings
  carry a P1/P2/P3 priority and quote the evidence above the prose.
- **One review per PR.** Never a comment storm. The same defect found by several lenses folds
  into one comment that lists the lenses and the other places it shows up; pushes don't
  duplicate it, a fixed finding's thread gets a "fixed in `<sha>`" note, and only a
  confirmed-gone comment is collapsed as outdated. A still-open finding is never touched.
- **Learns.** 👍/👎 reactions suppress repeated noise, inject learned advisories into future
  prompts, demote a lens this repo keeps rejecting, and propose `.buglens.yml` rules a human
  applies by hand. On merge it checks whether each finding was actually fixed.
- **Codebase-aware.** A persistent local index of the repo brings dependencies, importers,
  tests and earlier reviews into context, and ripple analysis names the callers of every
  function that changed.
- **Findings in the Checks UI.** Opt-in check-run annotations surface every finding in the
  Checks tab and the Files changed diff. They need a GitHub App install and never block a
  merge by default.
- **Symbol-aware and budgeted.** Findings are grouped by the symbol they change, and per-lens
  caps stop one lens from dominating the review comment.

Full detail: **[docs/engine.md](docs/engine.md)**.

## GitHub PR bot

Repo → **Settings → Webhooks → Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `https://your-host/api/v1/webhook` |
| Content type | `application/json` |
| Secret | the same string as `GITHUB_WEBHOOK_SECRET` |
| Events | **Pull requests**, **Issue comments**, **Pull request review comments**, **Reactions** |

Host it yourself with SQLite: no Redis, no queue, no vector DB required. See
[docs/configuration.md](docs/configuration.md).

## Docs

| | |
|---|---|
| [engine.md](docs/engine.md) | lenses, verify gate, tickets, code graph, recall, repo rules |
| [api.md](docs/api.md) | endpoints and request/response examples |
| [configuration.md](docs/configuration.md) | settings, `.buglens.yml`, env vars, deployment |
| [operations.md](docs/operations.md) | concurrency, caching, measured limits, troubleshooting |
| [faq.md](docs/faq.md) | common questions |
| [contributing.md](docs/contributing.md) | dev setup, tests, architecture pointers |

PRs to this repo are reviewed by BugLens itself.

---

MIT licensed. Built by [Vansh](https://github.com/Vansh940).

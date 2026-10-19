# Contributing

## Layout

```
backend/
  buglens/        the review engine (diff, lenses, verify, indexer, recall, store, pipeline)
  routers/        HTTP surface: review, webhook, feedback, history, analytics
  core/           config validation, security, budgets
  tests/          pytest suite (offline; no network)
vscode-extension/ the IDE client
docs/             this documentation
```

Architecture and the reasoning behind each module are in
[../AGENTS.md](../AGENTS.md); the improvement log is in [../ITERATION.md](../ITERATION.md).

## Dev setup

```bash
cd backend
uv venv
uv pip install -r requirements.txt
```

There is no `pip` in the workflow. Use `uv`. On Windows the interpreter is
`.venv/Scripts/python.exe`; elsewhere `.venv/bin/python`.

## Tests

```bash
cd backend
.venv/Scripts/python -m pytest -q
```

The suite is hermetic: no network, a temporary SQLite file, Redis unconfigured, and the LLM
is monkeypatched. Run it before opening a PR.

Extension build:

```bash
cd vscode-extension
npm run compile
```

## Benchmarks

```bash
cd backend
.venv/Scripts/python bench.py
```

Covers code-graph construction, related-file retrieval and store round-trips. Add a
measurement before and after any performance change; the numbers in
[docs/operations.md](operations.md) come from here.

## Ground rules

- **Never block a PR.** Comments only, `event=COMMENT`, no build failures.
- **One consolidated review per revision.** Do not add per-finding comment streams.
- **Evidence or it doesn't ship.** A finding that cannot cite an added line is dropped.
- **Idempotent.** Redelivery must be harmless.
- **No new required services.** SQLite and no Redis must remain sufficient for local use.
- **Store keys are `owner/repo`.** See the note in `AGENTS.md` before touching persistence.

## Pull requests

Push a branch and open a PR. BugLens reviews this repository too, so you will see the engine
in action on your own change.

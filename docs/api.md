# API

Base path `/api/v1`. Interactive schema at `/docs` on any running instance.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/review` | Review a code snippet or file (IDE) |
| `POST` | `/api/v1/webhook` | GitHub webhook receiver |
| `POST` | `/api/v1/feedback` | 👍/👎 on a finding fingerprint |
| `GET` | `/api/v1/engine` | Active lenses + effective repo rules |
| `GET` | `/api/v1/engine/repos` | Repos reviewed, with resolution rate |
| `GET` | `/api/v1/engine/stats` | Runs / findings / outcomes / drops (admin) |
| `GET` | `/api/v1/engine/learn` | Learned scopes and candidate rules for a repo (admin) |
| `GET` | `/api/v1/history` | Review history |
| `GET` | `/api/v1/stats` | Analytics and stats |
| `GET` | `/health` | Liveness + dependency check |

Authentication uses `X-Extension-Key` for `/review` and `X-Admin-Key` for
`/engine/stats`. `POST /feedback` is unauthenticated by design (it is driven by GitHub
reactions) and is rate limited.

Alongside the HTTP API, BugLens ships a local stdio MCP server (not an HTTP endpoint), run
with `python -m buglens.mcp_server`. See
[operations.md](operations.md#mcp-server) for the tool list and client configuration.

## POST /api/v1/review

```bash
curl -X POST https://your-host/api/v1/review \
  -H "Content-Type: application/json" \
  -H "X-Extension-Key: $EXTENSION_KEY" \
  -d '{
    "code": "def divide(a, b):\n    return a / b",
    "language": "python",
    "filename": "math.py"
  }'
```

The extension also sends `repo`, `branch`, `imports` and `related` (a map of local
dependency contents) so the engine can index the surrounding code before reviewing.

```json
{
  "summary": "Simple function missing division by zero protection.",
  "score": 52,
  "issues": [
    {
      "severity": "critical",
      "category": "bug",
      "line_number": 2,
      "title": "Division by zero not handled",
      "description": "If b is 0 this raises ZeroDivisionError and crashes.",
      "fix": "if b == 0: raise ValueError('Divisor cannot be zero')\nreturn a / b"
    }
  ],
  "positive_aspects": ["Concise and readable"],
  "model_used": "openai/gpt-oss-120b"
}
```

## POST /api/v1/webhook

Requires a valid `X-Hub-Signature-256` (HMAC-SHA256 over the raw body using
`GITHUB_WEBHOOK_SECRET`). The signature check is **fail-closed**: if the secret is unset the
endpoint refuses the request.

Deliveries are deduplicated by `X-GitHub-Delivery`, and a review is only started once per
`(repo, pr, head_sha)`, so a redelivery or a retried POST cannot double-post. The endpoint
returns `202` as soon as the review is queued; reviews run in the background.

Events handled:

| Event | Trigger | Effect |
|---|---|---|
| `pull_request` | `opened` / `synchronize` / `reopened` / `ready_for_review` | review the PR |
| `pull_request` | `closed` + merged | measure how many findings were fixed |
| `issue_comment` | `created` with an `@buglens` mention | `review` runs a review; anything else is answered in-thread | 
| `pull_request_review_comment` | `created` with an `@buglens` mention | same as above, in the review thread |
| `reaction` | `+1` / `-1` on a BugLens finding | record feedback (suppression / confirmed-good) |

See **Chat and commands** in [engine.md](engine.md#chat-and-commands) for the verbs.

## POST /api/v1/feedback

```bash
curl -X POST https://your-host/api/v1/feedback \
  -H "Content-Type: application/json" \
  -d '{"repo": "owner/name", "fingerprint": "ab12…", "verdict": "down"}'
```

`verdict` is `up` or `down`. Two net `down` suppresses the fingerprint; `up` records it as a
confirmed-good example.

## Engine introspection

```bash
# which lenses are active for a repo, and the rules that resulted
curl "$HOST/api/v1/engine?repo=owner/name"

# repos the engine has reviewed, with run counts and resolution rate
curl "$HOST/api/v1/engine/repos"

# learned feedback for a repo: per-lens stats, dismissal scopes, candidate rules (admin)
curl -H "X-Admin-Key: $ADMIN_API_KEY" "$HOST/api/v1/engine/learn?repo=owner/name"

# totals (admin); now includes a "drops" object keyed by verifier reason bucket
curl -H "X-Admin-Key: $ADMIN_API_KEY" "$HOST/api/v1/engine/stats?repo=owner/name"
```

`/engine/stats` returns `503` when `ADMIN_API_KEY` is not configured. That is deliberate, so
an unconfigured deployment never exposes counters.

`/engine/learn` is read-only and requires the admin key. It returns `{"repo": str, "lens_stats":
{...}, "scopes": [...], "candidates": [...]}`. `repo` is the `owner/name` slug; `lens_stats` maps
each lens to its raised count and its up/down feedback; `scopes` lists the path scopes where the
team repeatedly dismissed a lens; `candidates` lists proposed `.buglens.yml` snippets (an
`ignore_paths:` glob or a `focus:` change). Suggestions are never applied automatically.

`/engine/stats` now also returns `drops`, an object mapping each verifier reason bucket
(`machine`, `model`, `suppressed by prior feedback`, `unknown`) to a count.

Repo parameters always take the `owner/repo` form.

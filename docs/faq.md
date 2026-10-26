# FAQ

**Is it free?**
The extension and PR bot are open source and run against your own backend or the hosted
instance. Model usage is billed by whichever provider you configure.

**Is my code stored?**
Diffs are processed to produce findings. The engine persists findings, fingerprints,
outcomes and a local code index (function and symbol names, per-file hashes) so it can avoid
repeating itself. It does not persist your source files. Operate it yourself if you need the
data to stay inside your network.

**How accurate is it?**
The design targets precision over volume: findings must be anchored to added lines with
quoted evidence and pass an independent verification call. It is a strong first pass, not a
replacement for human review.

**Why did it not comment on an obvious style problem?**
Style nits are deliberately skipped (Cursor Bugbot takes the same position) and severity
floors or `ignore_paths` may exclude the file. Both are configurable.

**Can I run it without Redis, Postgres or Docker?**
Yes. SQLite in WAL mode is the default for engine state. Redis is optional and only shares
rate limits across processes.

**Can I review with a local model?**
Yes. Set `OPENAI_BASE_URL` to any OpenAI-compatible endpoint, including a local server, and
`OPENAI_MODEL` to the model name.

**Why is the first request slow?**
Free-tier hosts suspend idle instances. A periodic ping keeps it warm.

**How do I stop a specific comment from coming back?**
React 👎 on it twice (or `POST /api/v1/feedback` twice). Suppression is keyed to the finding
fingerprint, which is stable across pushes.

**How do I re-review a commit that was already reviewed?**
Comment `@buglens review` on the PR.

**Does it block merges or fail CI?**
No. Reviews are posted as comments and, if enabled, a neutral check run. It never requests
changes and never fails a build.

"""Prior-review and prior-PR recall (phase C of CONTEXT_PLAN.md).

Greptile keeps a knowledge base of past reviews; CodeRabbit remembers past comments so it
does not repeat itself. We get the same effect from data we already persist:

* ``prior_findings``, findings we posted on the **same files** in earlier PRs, with the
  outcome verdicts joined in, so we can tell *fixed*, *dismissed* and *still open* apart.
* ``dismissed``, titles the team down-voted twice; never raise them again.
* ``prior_prs``, recent PRs that touched the changed files (from ``file_prs``, filled by
  ``remember_history`` using the GitHub commits-per-path API, capped and cached).

``recall_block`` renders all of it as a small, hard-budgeted set of bullets that the
pipeline injects into the review context. Everything here is best-effort: no DB or no
GitHub simply means an empty block.
"""
from __future__ import annotations

from . import github, store

MAX_BULLETS = 8
HISTORY_PATHS = 5          # cap GitHub calls: only look up history for the first N files
HISTORY_PRS = 3


async def remember_history(repo: str, paths: list[str], head_sha: str = "",
                           db: str | None = None) -> int:
    """Fill ``file_prs`` from GitHub: recent PRs that touched the changed files.

    Skipped for files we already have history for, so each file costs at most one lookup
    and repeated reviews of the same file are free.
    """
    if not db or not github.configured():
        return 0
    recorded = 0
    for path in paths[:HISTORY_PATHS]:
        if await store.recent_prs_for_file(repo, path, 1, db):
            continue
        try:
            shas = await github.get_commits_for_path(repo.split("/")[0], repo.split("/")[-1], path, HISTORY_PRS)
            for sha in shas:
                for pr in (await github.pulls_for_commit(repo.split("/")[0], repo.split("/")[-1], sha))[:HISTORY_PRS]:
                    if not pr.get("number"):
                        continue
                    await store.record_file_pr(repo, path, int(pr["number"]),
                                               sha, pr.get("merged_at") or "", db)
                    recorded += 1
        except Exception:
            continue
    return recorded


async def prior_findings(repo: str, paths: list[str], db: str | None = None,
                         limit: int = MAX_BULLETS) -> list[dict]:
    return await store.findings_for_paths(repo, paths, limit, db) if db else []


async def dismissed(repo: str, db: str | None = None, threshold: int = 2) -> set[str]:
    """Fingerprints the team has down-voted enough times to be silenced."""
    return await store.suppressed(repo, threshold, db) if db else set()


def _verdict_of(row: dict) -> str:
    verdicts = (row.get("verdicts") or "").split(",")
    if "resolved" in verdicts:
        return "fixed"
    if verdicts.count("down") >= 2:
        return "dismissed"
    if "up" in verdicts:
        return "agreed"
    return "open"


async def recall_block(repo: str, paths: list[str], db: str | None = None,
                       budget_chars: int = 1500) -> str:
    """A ≤8-bullet digest of prior reviews and history for the changed files."""
    if not db or not paths:
        return ""
    bullets: list[str] = []

    rows = await prior_findings(repo, paths, db)
    seen_titles: set[str] = set()
    fixed = dismissed_n = 0
    for row in rows:
        key = (row.get("title") or "").strip().lower()
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        state = _verdict_of(row)
        if state == "fixed":
            fixed += 1
            continue
        if state == "dismissed":
            dismissed_n += 1
            continue
        bullets.append(f"- [{state}] {row.get('path')}: {row.get('title')} (PR #{row.get('pr')})")
        if len(bullets) >= MAX_BULLETS - 2:
            break
    if fixed:
        bullets.append(f"- {fixed} earlier finding(s) on these files were fixed after review")
    if dismissed_n:
        bullets.append(f"- {dismissed_n} earlier finding(s) on these files were dismissed by the team")

    history = await store.recent_prs_for_paths(repo, paths[:3], HISTORY_PRS, db)
    for path in paths[:3]:
        prs = history.get(path)
        if prs:
            nums = ", ".join(f"#{p['pr']}" for p in prs if p.get("pr"))
            bullets.append(f"- `{path}` was last touched by PR {nums}")

    if not bullets:
        return ""
    body = "\n".join(f"- {b[2:]}" if b.startswith("- ") else b for b in bullets[:MAX_BULLETS])
    return ("## Prior reviews on these files (do not repeat these; note if still open)\n"
            + body)[:budget_chars]

"""Durable review state: idempotency, dedupe, and the feedback loop.

Backed by SQLite (aiosqlite) so the engine runs with zero infrastructure and the
tests can use a temp file. Three things live here that make the bot iterative:

* **deliveries / runs**, a webhook redelivery or a second push of the same
  revision cannot re-post the same review (v1 had no memory at all).
* **findings**, every finding is stored with a stable fingerprint, so the same
  comment is not re-posted on the next push.
* **outcomes**, 👍/👎 from comment reactions and the IDE feed a suppression set:
  a finding fingerprint down-voted twice is never posted again, and up-voted
  patterns are injected into the prompt as "this team likes this". That is the
  cheap version of Greptile's "stops commenting on things you don't care about".
"""
from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import os
import time
import uuid
from pathlib import Path

import aiosqlite

_BUSY_MS = int(os.getenv("BUGLENS_SQLITE_BUSY_MS", "5000"))
_POOL_SIZE = int(os.getenv("BUGLENS_SQLITE_POOL", "4"))
_pools: dict[str, asyncio.Queue] = {}
_sems: dict[str, asyncio.Semaphore] = {}


def _pool(path: str) -> tuple[asyncio.Queue, asyncio.Semaphore]:
    q = _pools.get(path)
    if q is None:
        q = _pools[path] = asyncio.Queue()
        _sems[path] = asyncio.Semaphore(_POOL_SIZE)
    return q, _sems[path]


async def _new_conn(path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path, timeout=_BUSY_MS / 1000)
    # journal_mode=WAL is persisted in the file itself (set once in init()); only the
    # per-connection settings need to be re-applied here.
    await db.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


@contextlib.asynccontextmanager
async def _open(path: str):
    """Borrow one of at most ``_POOL_SIZE`` connections for this DB file.

    aiosqlite multiplexes operations onto a private thread per connection, but a *new*
    connection means a new thread plus handshakes, the engine used to pay that on every
    store call (~5ms/op). Connections are now reused from a per-file queue, and the
    semaphore bounds how many exist: concurrent callers **wait** for a free connection
    instead of each spawning a thread. WAL lets readers proceed while a writer commits.
    """
    q, sem = _pool(path)
    await sem.acquire()
    try:
        try:
            db = q.get_nowait()
        except asyncio.QueueEmpty:
            db = await _new_conn(path)
        ok = False
        try:
            yield db
            ok = True
        finally:
            if not ok:
                with contextlib.suppress(Exception):
                    await db.rollback()
            q.put_nowait(db)
    finally:
        sem.release()


async def close_pool() -> None:
    """Close every pooled connection (app shutdown / tests) so no worker threads linger."""
    for path, q in list(_pools.items()):
        while not q.empty():
            db = q.get_nowait()
            with contextlib.suppress(Exception):
                await db.close()
        _pools.pop(path, None)
        _sems.pop(path, None)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT,
    verdict TEXT,
    score INTEGER,
    findings_count INTEGER DEFAULT 0,
    posted INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(repo, pr, head_sha)
);
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    repo TEXT, pr INTEGER, head_sha TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    path TEXT, line INTEGER, severity TEXT, category TEXT, lens TEXT,
    title TEXT, body TEXT, fix TEXT, evidence TEXT, confidence REAL,
    posted INTEGER DEFAULT 0, comment_id TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
    fingerprint TEXT NOT NULL,
    repo TEXT NOT NULL,
    verdict TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_fp ON findings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_outcomes_fp ON outcomes(repo, fingerprint);
CREATE TABLE IF NOT EXISTS file_cache (
    repo TEXT NOT NULL, key TEXT NOT NULL, findings TEXT,
    created_at TEXT NOT NULL, PRIMARY KEY (repo, key)
);
CREATE TABLE IF NOT EXISTS repo_index (
    repo TEXT NOT NULL, path TEXT NOT NULL, sha TEXT NOT NULL, lang TEXT,
    data TEXT NOT NULL, indexed_at TEXT NOT NULL, PRIMARY KEY (repo, path)
);
CREATE INDEX IF NOT EXISTS idx_repo_index_repo ON repo_index(repo);
CREATE TABLE IF NOT EXISTS file_prs (
    repo TEXT NOT NULL, path TEXT NOT NULL, pr INTEGER NOT NULL, sha TEXT,
    merged_at TEXT, PRIMARY KEY (repo, path, pr)
);
CREATE TABLE IF NOT EXISTS index_meta (
    repo TEXT PRIMARY KEY, writes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pr_round_files (
    repo TEXT NOT NULL, pr INTEGER NOT NULL, path TEXT NOT NULL,
    content_sha TEXT NOT NULL, first_round INTEGER NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (repo, pr, path, content_sha)
);
CREATE TABLE IF NOT EXISTS drops (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    lens TEXT,
    reason TEXT,
    title TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drops_repo ON drops(repo);
CREATE TABLE IF NOT EXISTS posted_comments (
    repo        TEXT NOT NULL,
    pr          INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    path        TEXT,
    line        INTEGER,
    comment_id  TEXT,
    node_id     TEXT,
    head_sha    TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (repo, pr, fingerprint)
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def db_path() -> str:
    return os.getenv("BUGLENS_DB") or str(Path(__file__).resolve().parent / "buglens_state.sqlite")


async def init(path: str | None = None) -> str:
    path = path or db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with _open(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")     # persisted in the file; set once
        await db.executescript(_SCHEMA)
        # Migration for stores created before the evidence column existed.
        try:
            await db.execute("ALTER TABLE findings ADD COLUMN evidence TEXT")
        except Exception:
            pass
        await db.commit()
    return path


STALE_RUN_MIN = int(os.getenv("BUGLENS_STALE_RUN_MIN", "30"))


def _age_minutes(stamp: str) -> float:
    """Minutes since an ISO-8601 UTC stamp. ``calendar.timegm`` is required here: the stamp
    is UTC, and ``time.mktime`` would read it as local time and mis-age it by the offset."""
    try:
        return (time.time() - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))) / 60.0
    except Exception:
        return 0.0


async def claim_run(repo: str, pr: int, head_sha: str, base_sha: str | None,
                    path: str | None = None) -> str | None:
    """Reserve this (repo, pr, head_sha). Returns run id, or None if already done.

    A run that was claimed but never finished (process died, deploy, OOM) is *reclaimed*
    after ``BUGLENS_STALE_RUN_MIN`` minutes. Without this a crashed review left a permanent
    ghost row, and GitHub's redelivery was then rejected as "already reviewed", so the PR
    silently never got a comment.
    """
    path = path or db_path()
    async with _open(path) as db:
        existing = await (await db.execute(
            "SELECT id, verdict, created_at FROM runs WHERE repo=? AND pr=? AND head_sha=?",
            (repo, pr, head_sha),
        )).fetchone()
        if existing:
            run_id, verdict, created_at = existing
            if verdict is not None or _age_minutes(created_at) < STALE_RUN_MIN:
                return None
            await db.execute("UPDATE runs SET created_at=? WHERE id=?", (_now(), run_id))
            await db.commit()
            return run_id
        run_id = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO runs (id, repo, pr, head_sha, base_sha, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, repo, pr, head_sha, base_sha, _now()),
        )
        await db.commit()
        return run_id


async def count_rounds(repo: str, pr: int, path: str | None = None) -> int:
    """How many reviews this PR has accumulated (every claimed run counts, this one too).

    The re-review cap reads this: a PR that has been reviewed ten times does not need an
    eleventh automated pass (the agent-writes / agent-reviews loop is the #1 cost sink).
    """
    path = path or db_path()
    async with _open(path) as db:
        row = await (await db.execute(
            "SELECT COUNT(*) FROM runs WHERE repo=? AND pr=?", (repo, pr),
        )).fetchone()
    return int(row[0] or 0)


async def reviewed_contents(repo: str, pr: int, before_round: int,
                            path: str | None = None) -> dict[str, set[str]]:
    """{path: {content_sha, ...}} for files already reviewed in *earlier* rounds.

    ``first_round < before_round``, a file whose reviewed content hash is in here has not
    changed since a prior round and does not need to be reviewed again."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT path, content_sha FROM pr_round_files WHERE repo=? AND pr=? AND first_round<?",
            (repo, pr, before_round),
        )).fetchall()
    out: dict[str, set[str]] = {}
    for p, sha in rows:
        out.setdefault(p, set()).add(sha)
    return out


async def record_round_files(repo: str, pr: int, round_no: int,
                             items: list[tuple[str, str]], path: str | None = None) -> None:
    """Ledger of reviewed (path, content-hash) pairs, one row per pair ever seen.

    ``INSERT OR IGNORE`` keeps the *first* round a hash was seen, so ``first_round`` marks
    when the content was actually reviewed. Empty content hashes are skipped (a file we did
    not really read must not count as reviewed)."""
    rows = [(repo, pr, p, sha, round_no, _now()) for p, sha in items if p and sha]
    if not rows:
        return
    path = path or db_path()
    async with _open(path) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO pr_round_files "
            "(repo, pr, path, content_sha, first_round, created_at) VALUES (?,?,?,?,?,?)", rows,
        )
        await db.commit()


async def finish_run(run_id: str, verdict: str, score: int, findings_count: int, posted: bool,
                     path: str | None = None) -> None:
    path = path or db_path()
    async with _open(path) as db:
        await db.execute(
            "UPDATE runs SET verdict=?, score=?, findings_count=?, posted=? WHERE id=?",
            (verdict, score, findings_count, 1 if posted else 0, run_id),
        )
        await db.commit()


async def save_findings(run_id: str, findings: list[dict], path: str | None = None) -> None:
    path = path or db_path()
    async with _open(path) as db:
        for f in findings:
            await db.execute(
                "INSERT INTO findings (id, run_id, fingerprint, path, line, severity, category, lens,"
                " title, body, fix, evidence, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, run_id, f["fingerprint"], f["path"], f.get("line"),
                 f.get("severity"), f.get("category"), f.get("lens"), f.get("title"),
                 f.get("body"), f.get("fix"), f.get("evidence"), f.get("confidence")),
            )
        await db.commit()


async def mark_posted(run_id: str, path: str | None = None) -> None:
    """Mark a run's findings as posted, so they are never re-posted on this repo."""
    path = path or db_path()
    async with _open(path) as db:
        await db.execute("UPDATE findings SET posted=1 WHERE run_id=?", (run_id,))
        await db.commit()


async def posted_fingerprints(repo: str, path: str | None = None) -> set[str]:
    """Fingerprints already posted on this repo (across runs), cross-push dedupe."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT f.fingerprint FROM findings f JOIN runs r ON f.run_id=r.id "
            "WHERE r.repo=? AND f.posted=1", (repo,)
        )).fetchall()
    return {r[0] for r in rows}


async def record_delivery(delivery_id: str, repo: str, pr: int, head_sha: str,
                          path: str | None = None) -> bool:
    """False if this X-GitHub-Delivery was already seen."""
    if not delivery_id:
        return True
    path = path or db_path()
    async with _open(path) as db:
        try:
            await db.execute(
                "INSERT INTO deliveries (id, repo, pr, head_sha, created_at) VALUES (?,?,?,?,?)",
                (delivery_id, repo, pr, head_sha, _now()),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def findings_for_pr(repo: str, pr: int, path: str | None = None) -> list[dict]:
    """Findings we posted on this PR (used to measure resolution after merge)."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT f.fingerprint, f.path, f.line, f.severity, f.title, f.evidence "
            "FROM findings f JOIN runs r ON f.run_id=r.id "
            "WHERE r.repo=? AND r.pr=? AND f.posted=1", (repo, pr)
        )).fetchall()
    keys = ("fingerprint", "path", "line", "severity", "title", "evidence")
    return [dict(zip(keys, row)) for row in rows]


async def findings_for_paths(repo: str, paths: list[str], limit: int = 20,
                             path: str | None = None) -> list[dict]:
    """Posted findings we raised on these files in earlier PRs (prior-review recall).

    Carries the outcome verdicts so a caller can tell "already fixed", "dismissed by the
    team" and "still open" apart.
    """
    path = path or db_path()
    if not paths:
        return []
    marks = ",".join("?" for _ in paths)
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT f.fingerprint, f.path, f.severity, f.category, f.title, r.pr, r.created_at,"
            " (SELECT group_concat(verdict) FROM outcomes o WHERE o.repo=r.repo"
            "  AND o.fingerprint=f.fingerprint)"
            f" FROM findings f JOIN runs r ON f.run_id=r.id"
            f" WHERE r.repo=? AND f.posted=1 AND f.path IN ({marks})"
            " ORDER BY r.created_at DESC LIMIT ?",
            (repo, *paths, limit),
        )).fetchall()
    keys = ("fingerprint", "path", "severity", "category", "title", "pr", "at", "verdicts")
    return [dict(zip(keys, row)) for row in rows]


async def resolution_stats(repo: str, path: str | None = None) -> dict:
    path = path or db_path()
    async with _open(path) as db:
        row = (await (await db.execute(
            "SELECT SUM(CASE WHEN verdict='resolved' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN verdict='unresolved' THEN 1 ELSE 0 END) "
            "FROM outcomes WHERE repo=?", (repo,)
        )).fetchone())
    resolved, unresolved = (row[0] or 0), (row[1] or 0)
    total = resolved + unresolved
    return {"resolved": resolved, "unresolved": unresolved,
            "rate": round(resolved / total, 3) if total else None}


async def get_file_cache(repo: str, key: str, max_age_s: int = 604800,
                         path: str | None = None) -> list[dict] | None:
    """Findings from an identical (file diff + rules) reviewed recently. Cuts tokens on re-pushes."""
    path = path or db_path()
    async with _open(path) as db:
        row = (await (await db.execute(
            "SELECT findings, created_at FROM file_cache WHERE repo=? AND key=?", (repo, key)
        )).fetchone())
    if not row or not row[0]:
        return None
    try:
        age = time.time() - float(row[1])
    except (TypeError, ValueError):
        return None
    if age > max_age_s:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


async def put_file_cache(repo: str, key: str, findings: list[dict], path: str | None = None) -> None:
    path = path or db_path()
    async with _open(path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO file_cache (repo, key, findings, created_at) VALUES (?,?,?,?)",
            (repo, key, json.dumps(findings), str(time.time())),
        )
        await db.commit()


async def get_index_state(repo: str, path: str | None = None) -> dict[str, str]:
    """path -> sha for everything already indexed in this repo."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT path, sha FROM repo_index WHERE repo=?", (repo,)
        )).fetchall()
    return {r[0]: r[1] for r in rows}


async def get_index(repo: str, path: str | None = None) -> dict[str, dict]:
    """The whole code graph for a repo: path -> parsed record (see indexer.parse_text)."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT path, data FROM repo_index WHERE repo=?", (repo,)
        )).fetchall()
    out: dict[str, dict] = {}
    for p, data in rows:
        try:
            out[p] = json.loads(data)
        except Exception:
            continue
    return out


async def _bump_index(db, repo: str) -> None:
    """Bump a repo's index write counter (in the same transaction as the write).

    The pipeline caches the deserialized index + import graph, and a counter is the only
    exact invalidation key: timestamps are second-resolution, so a same-second update would
    otherwise look unchanged, and a row count alone misses in-place updates.
    """
    await db.execute(
        "INSERT INTO index_meta (repo, writes) VALUES (?, 1) "
        "ON CONFLICT(repo) DO UPDATE SET writes = writes + 1", (repo,)
    )


async def put_index(repo: str, file_path: str, sha: str, lang: str, data: dict,
                    db_file: str | None = None) -> None:
    db_file = db_file or db_path()
    async with _open(db_file) as db:
        await db.execute(
            "INSERT OR REPLACE INTO repo_index (repo, path, sha, lang, data, indexed_at) "
            "VALUES (?,?,?,?,?,?)",
            (repo, file_path, sha, lang, json.dumps(data), _now()),
        )
        await _bump_index(db, repo)
        await db.commit()


async def put_index_many(repo: str, records: list[dict], db_file: str | None = None) -> None:
    """Write many parsed files in one transaction (index_files used to open one
    connection, and therefore one aiosqlite thread, per file)."""
    if not records:
        return
    now = _now()
    rows = [(repo, r["path"], r["sha"], r.get("lang", ""), json.dumps(r), now) for r in records]
    db_file = db_file or db_path()
    async with _open(db_file) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO repo_index (repo, path, sha, lang, data, indexed_at) "
            "VALUES (?,?,?,?,?,?)", rows,
        )
        await _bump_index(db, repo)
        await db.commit()

async def prune_index(repo: str, keep: set[str], path: str | None = None) -> int:
    """Drop index rows for files that no longer exist in the repo tree."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute("SELECT path FROM repo_index WHERE repo=?", (repo,))).fetchall()
        stale = [r[0] for r in rows if r[0] not in keep]
        if stale:                                  # one executemany, not a query per row
            await db.executemany("DELETE FROM repo_index WHERE repo=? AND path=?",
                                 [(repo, p) for p in stale])
            await _bump_index(db, repo)
            await db.commit()
    return len(stale)


async def record_file_pr(repo: str, file_path: str, pr: int, sha: str = "",
                         merged_at: str = "", db_file: str | None = None) -> None:
    db_file = db_file or db_path()
    async with _open(db_file) as db:
        await db.execute(
            "INSERT OR REPLACE INTO file_prs (repo, path, pr, sha, merged_at) VALUES (?,?,?,?,?)",
            (repo, file_path, pr, sha, merged_at),
        )
        await db.commit()


async def recent_prs_for_file(repo: str, file_path: str, limit: int = 5,
                              db_file: str | None = None) -> list[dict]:
    db_file = db_file or db_path()
    async with _open(db_file) as db:
        rows = await (await db.execute(
            "SELECT pr, sha, merged_at FROM file_prs WHERE repo=? AND path=? "
            "ORDER BY merged_at DESC LIMIT ?", (repo, file_path, limit)
        )).fetchall()
    return [{"pr": r[0], "sha": r[1], "merged_at": r[2]} for r in rows]


async def recent_prs_for_paths(repo: str, paths: list[str], limit: int = 5,
                               db_file: str | None = None) -> dict[str, list[dict]]:
    """Batch form of ``recent_prs_for_file``, one query for every changed file."""
    if not paths:
        return {}
    db_file = db_file or db_path()
    marks = ",".join("?" * len(paths))
    async with _open(db_file) as db:
        rows = await (await db.execute(
            f"SELECT path, pr, sha, merged_at FROM file_prs WHERE repo=? AND path IN ({marks}) "
            "ORDER BY merged_at DESC", (repo, *paths)
        )).fetchall()
    out: dict[str, list[dict]] = {}
    for path, pr, sha, merged_at in rows:          # rows arrive newest-first per file
        bucket = out.setdefault(path, [])
        if len(bucket) < limit:
            bucket.append({"pr": pr, "sha": sha, "merged_at": merged_at})
    return out


async def record_outcome(repo: str, fingerprint: str, verdict: str, title: str = "",
                         path: str | None = None) -> None:
    path = path or db_path()
    async with _open(path) as db:
        await db.execute(
            "INSERT INTO outcomes (fingerprint, repo, verdict, title, created_at) VALUES (?,?,?,?,?)",
            (fingerprint, repo, verdict, title, _now()),
        )
        await db.commit()


async def suppressed(repo: str, threshold: int = 2, path: str | None = None) -> set[str]:
    """Fingerprints down-voted at least `threshold` times net."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT fingerprint,"
            " SUM(CASE WHEN verdict='down' THEN 1 ELSE 0 END) AS downs,"
            " SUM(CASE WHEN verdict='up' THEN 1 ELSE 0 END) AS ups"
            " FROM outcomes WHERE repo=? GROUP BY fingerprint", (repo,)
        )).fetchall()
    return {r[0] for r in rows if (r[1] or 0) - (r[2] or 0) >= threshold}


async def confirmed_good(repo: str, limit: int = 10, path: str | None = None) -> list[str]:
    """Titles of findings this team has confirmed as genuinely good to raise."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT title FROM outcomes WHERE repo=? AND verdict='up' AND title != '' "
            "ORDER BY created_at DESC LIMIT ?", (repo, limit)
        )).fetchall()
    return [r[0] for r in rows]


async def known_repos(path: str | None = None, limit: int = 50) -> list[dict]:
    """Repos this store has reviewed, with their run/resolution counters.

    Powers the dashboard repo picker and the IDE's "is this repo known?" check.
    """
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT repo, COUNT(*) AS runs, MAX(created_at) AS last_at,"
            " SUM(CASE WHEN posted=1 THEN 1 ELSE 0 END) AS posted"
            " FROM runs GROUP BY repo ORDER BY last_at DESC LIMIT ?", (limit,)
        )).fetchall()
        # resolution counters for all repos in one query (was one query per repo)
        res_rows = await (await db.execute(
            "SELECT repo,"
            " SUM(CASE WHEN verdict='resolved' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN verdict='unresolved' THEN 1 ELSE 0 END)"
            " FROM outcomes GROUP BY repo"
        )).fetchall()
    resolutions = {r[0]: (r[1] or 0, r[2] or 0) for r in res_rows}
    out = []
    for repo, runs, last_at, posted in rows:
        resolved, unresolved = resolutions.get(repo, (0, 0))
        total = resolved + unresolved
        out.append({"repo": repo, "runs": runs, "posted": posted, "last_at": last_at,
                    "resolution": {"resolved": resolved, "unresolved": unresolved,
                                   "rate": round(resolved / total, 3) if total else None}})
    return out


async def index_version(repo: str, path: str | None = None) -> str:
    """Exact version of a repo's code index (a write counter, bumped on every index write).

    Lets the pipeline cache the deserialized index + import graph in-process instead of
    re-reading and JSON-parsing every row on every review of the same repo.
    """
    path = path or db_path()
    async with _open(path) as db:
        row = (await (await db.execute(
            "SELECT writes FROM index_meta WHERE repo=?", (repo,)
        )).fetchone())
    return str(row[0]) if row else "0"


async def sweep(days_cache: int = 7, days_repo: int = 30, path: str | None = None) -> dict:
    """Drop expired file-cache rows and index history for repos we have not seen in a while.

    Neither table was ever cleaned, so a long-running instance accumulated them without
    bound. Note this deletes whole repos' index rows, never individual paths, pruning
    individual paths is unsafe because a review only knows about the files it fetched.
    """
    path = path or db_path()
    cutoff_cache = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_cache * 86400))
    cutoff_repo = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_repo * 86400))
    async with _open(path) as db:
        cur = await db.execute("DELETE FROM file_cache WHERE created_at < ?", (cutoff_cache,))
        cache_rows = cur.rowcount or 0
        cur = await db.execute(
            "DELETE FROM repo_index WHERE repo IN ("
            "  SELECT repo FROM (SELECT repo, MAX(created_at) AS last_at FROM runs GROUP BY repo)"
            "  WHERE last_at < ?)", (cutoff_repo,)
        )
        index_rows = cur.rowcount or 0
        cur = await db.execute("DELETE FROM file_prs WHERE merged_at <> '' AND merged_at < ?",
                               (cutoff_repo,))
        pr_rows = cur.rowcount or 0
        await db.commit()
    return {"file_cache": cache_rows, "repo_index": index_rows, "file_prs": pr_rows}


async def stats(path: str | None = None) -> dict:
    path = path or db_path()
    async with _open(path) as db:
        runs = (await (await db.execute("SELECT COUNT(*) FROM runs")).fetchone())[0]
        findings = (await (await db.execute("SELECT COUNT(*) FROM findings")).fetchone())[0]
        outcomes = (await (await db.execute("SELECT COUNT(*) FROM outcomes")).fetchone())[0]
    return {"runs": runs, "findings": findings, "outcomes": outcomes}


async def lens_outcome_stats(repo: str, path: str | None = None) -> dict[str, dict]:
    """Per-lens raised/feedback counts for a repo.

    Returns {lens_name: {"raised": int, "up": int, "down": int,
                          "fixed": int, "unfixed": int}}.
    Raised = rows in `findings` for that lens belonging to this repo's runs
    (join findings -> runs on run_id, filter runs.repo = ?).
    up/down/fixed/unfixed come from LEFT JOIN outcomes on
    (o.fingerprint = f.fingerprint AND o.repo = ?), counting verdicts
    'up', 'down', 'resolved', 'unresolved'. A lens with no feedback still appears
    with 0s. Rows with a NULL/empty lens are grouped under "unknown".
    """
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT COALESCE(NULLIF(f.lens, ''), 'unknown') AS lens,"
            " COUNT(DISTINCT f.id) AS raised,"
            " SUM(CASE WHEN o.verdict='up' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN o.verdict='down' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN o.verdict='resolved' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN o.verdict='unresolved' THEN 1 ELSE 0 END)"
            " FROM findings f JOIN runs r ON f.run_id=r.id"
            " LEFT JOIN outcomes o ON o.fingerprint=f.fingerprint AND o.repo=?"
            " WHERE r.repo=?"
            " GROUP BY COALESCE(NULLIF(f.lens, ''), 'unknown')",
            (repo, repo),
        )).fetchall()
    return {
        r[0]: {"raised": int(r[1] or 0), "up": int(r[2] or 0), "down": int(r[3] or 0),
               "fixed": int(r[4] or 0), "unfixed": int(r[5] or 0)}
        for r in rows
    }


async def dismissal_scopes(repo: str, path: str | None = None,
                           threshold: int = 2) -> list[dict]:
    """Scopes where the team repeatedly dismissed findings.

    Group findings (for this repo's runs) by (scope, lens), where scope is derived from the
    finding path: if os.path.dirname(path) is non-empty -> "<dirname>/*" else "*".
    For each group compute downs = sum of 'down' outcomes and ups = sum of 'up' outcomes
    over the group's findings (LEFT JOIN outcomes as above).
    Keep groups where (downs - ups) >= threshold and downs >= 2.
    Return a list of dicts, sorted by (downs - ups) desc then scope asc:
      {"scope": str, "lens": str, "downs": int, "ups": int, "sample": str}
    where sample is one representative finding title from the group (any non-empty title).
    Empty list when nothing qualifies.
    """
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT f.path, COALESCE(NULLIF(f.lens, ''), 'unknown'), f.title, o.verdict"
            " FROM findings f JOIN runs r ON f.run_id=r.id"
            " LEFT JOIN outcomes o ON o.fingerprint=f.fingerprint AND o.repo=?"
            " WHERE r.repo=?",
            (repo, repo),
        )).fetchall()
    groups: dict[tuple[str, str], dict] = {}
    for f_path, lens, title, verdict in rows:
        parent = os.path.dirname(f_path) if f_path else ""
        scope = f"{parent}/*" if parent else "*"
        g = groups.setdefault((scope, lens), {"scope": scope, "lens": lens,
                                              "downs": 0, "ups": 0, "sample": ""})
        if verdict == "down":
            g["downs"] += 1
        elif verdict == "up":
            g["ups"] += 1
        if not g["sample"] and title:
            g["sample"] = title
    out = [g for g in groups.values()
           if (g["downs"] - g["ups"]) >= threshold and g["downs"] >= 2]
    out.sort(key=lambda g: (-(g["downs"] - g["ups"]), g["scope"]))
    return out


async def save_drops(run_id: str, repo: str, dropped: list[dict],
                     path: str | None = None) -> int:
    """Persist verifier rationales so outcome metrics can explain why candidates vanish.

    For each dropped finding, store: id (uuid4 hex), run_id, repo, lens (f.get("lens")),
    reason = (f.get("drop_reason") or "unknown").split(":", 1)[0].strip(),
    title (f.get("title")), created_at (_now()). Single connection, single commit.
    Returns the number of rows written.
    """
    path = path or db_path()
    if not dropped:
        return 0
    now = _now()
    rows = [
        (uuid.uuid4().hex, run_id, repo, f.get("lens"),
         (f.get("drop_reason") or "unknown").split(":", 1)[0].strip(),
         f.get("title"), now)
        for f in dropped
    ]
    async with _open(path) as db:
        await db.executemany(
            "INSERT INTO drops (id, run_id, repo, lens, reason, title, created_at)"
            " VALUES (?,?,?,?,?,?,?)", rows,
        )
        await db.commit()
    return len(rows)


async def drop_stats(repo: str, path: str | None = None) -> dict[str, int]:
    """Reason bucket -> count of drops for a repo (e.g. {"machine": 3, "model": 5})."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT reason, COUNT(*) FROM drops WHERE repo=? GROUP BY reason", (repo,)
        )).fetchall()
    return {r[0] or "unknown": int(r[1]) for r in rows}


async def save_posted_comments(repo: str, pr: int, head_sha: str,
                               items: list[dict], path: str | None = None) -> None:
    """Upsert the inline comment posted for each finding fingerprint on this PR.

    The ledger maps a finding fingerprint to the GitHub comment id/node id we created
    for it, so a later push can retire or reply to a stale comment. Items without a
    fingerprint are skipped. A repeat fingerprint updates the tracked fields in place,
    so one row per (repo, pr, fingerprint) is kept.
    """
    path = path or db_path()
    now = _now()
    rows = []
    for item in items:
        fingerprint = item.get("fingerprint")
        if not fingerprint:
            continue
        comment_id = item.get("comment_id")
        rows.append((
            repo, pr, fingerprint, item.get("path"), item.get("line"),
            None if comment_id is None else str(comment_id), item.get("node_id"), head_sha, now,
        ))
    if not rows:
        return
    async with _open(path) as db:
        await db.executemany(
            "INSERT INTO posted_comments "
            "(repo, pr, fingerprint, path, line, comment_id, node_id, head_sha, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(repo, pr, fingerprint) DO UPDATE SET"
            " path=excluded.path, line=excluded.line, comment_id=excluded.comment_id,"
            " node_id=excluded.node_id, head_sha=excluded.head_sha, created_at=excluded.created_at",
            rows,
        )
        await db.commit()


async def posted_comments(repo: str, pr: int, path: str | None = None) -> list[dict]:
    """Inline comments posted on this (repo, pr), oldest first, as row dicts."""
    path = path or db_path()
    async with _open(path) as db:
        rows = await (await db.execute(
            "SELECT fingerprint, path, line, comment_id, node_id, head_sha, created_at"
            " FROM posted_comments WHERE repo=? AND pr=? ORDER BY created_at ASC", (repo, pr)
        )).fetchall()
    keys = ("fingerprint", "path", "line", "comment_id", "node_id", "head_sha", "created_at")
    return [dict(zip(keys, row)) for row in rows]


async def forget_posted_comment(repo: str, pr: int, fingerprint: str,
                                path: str | None = None) -> None:
    """Drop the ledger row for (repo, pr, fingerprint). Absent rows are a silent no-op."""
    path = path or db_path()
    async with _open(path) as db:
        await db.execute(
            "DELETE FROM posted_comments WHERE repo=? AND pr=? AND fingerprint=?",
            (repo, pr, fingerprint),
        )
        await db.commit()

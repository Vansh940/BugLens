import pytest

from buglens import github, recall, store


async def _seed(repo, db, *, posts=None, outcomes=None, file_prs=None):
    await store.init(db)
    for i, f in enumerate(posts or []):
        run_id = await store.claim_run(repo, f["pr"], f"sha{i}", "base", db)
        await store.save_findings(run_id, [f], db)
        await store.mark_posted(run_id, db)
    for fp, verdict in outcomes or []:
        await store.record_outcome(repo, fp, verdict, "t", db)
    for path, pr in file_prs or []:
        await store.record_file_pr(repo, path, pr, "sha", "2026-01-01T00:00:00Z", db)


def _finding(fp, path="a.py", title="eval on user input", pr=1):
    return {"fingerprint": fp, "path": path, "line": 2, "severity": "major",
            "category": "security", "lens": "security", "title": title,
            "body": "b", "fix": "f", "evidence": "e", "confidence": 0.9, "pr": pr}


async def test_prior_findings_and_states(db):
    await _seed("o/r", db,
                posts=[_finding("fp1", pr=3), _finding("fp2", title="missing timeout", pr=4),
                       _finding("fp3", title="noisy nit", pr=5)],
                outcomes=[("fp1", "resolved"), ("fp3", "down"), ("fp3", "down")])

    rows = await recall.prior_findings("o/r", ["a.py"], db)
    states = {r["title"]: recall._verdict_of(r) for r in rows}
    assert states["eval on user input"] == "fixed"
    assert states["missing timeout"] == "open"
    assert states["noisy nit"] == "dismissed"

    block = await recall.recall_block("o/r", ["a.py"], db)
    assert "missing timeout" in block
    assert "noisy nit" not in block          # dismissed findings are never re-raised
    assert "were fixed after review" in block
    assert "dismissed by the team" in block


async def test_recall_block_lists_recent_prs(db):
    await _seed("o/r", db, file_prs=[("a.py", 11), ("a.py", 12)])
    block = await recall.recall_block("o/r", ["a.py"], db)
    assert "#11" in block and "#12" in block


async def test_recall_block_empty_without_data(db):
    await store.init(db)
    assert await recall.recall_block("o/r", ["a.py"], db) == ""
    assert await recall.prior_findings("o/r", ["a.py"], db) == []


async def test_dismissed_only_counts_pathways(db):
    await _seed("o/r", db, posts=[_finding("fp9", title="x")],
                outcomes=[("fp9", "down"), ("fp9", "down"), ("fp9", "up")])
    assert await recall.dismissed("o/r", db) == set()      # 2 down - 1 up = 1 < threshold


async def test_remember_history_caches_and_bounds(monkeypatch, db):
    await store.init(db)
    calls = {"commits": 0, "pulls": 0}

    monkeypatch.setattr(github, "configured", lambda: True)

    async def get_commits_for_path(owner, repo, path, limit):
        calls["commits"] += 1
        return ["sha1"]

    async def pulls_for_commit(owner, repo, sha):
        calls["pulls"] += 1
        return [{"number": 7, "title": "t", "merged_at": "2026-02-02T00:00:00Z"}]

    monkeypatch.setattr(github, "get_commits_for_path", get_commits_for_path)
    monkeypatch.setattr(github, "pulls_for_commit", pulls_for_commit)

    assert await recall.remember_history("o/r", ["a.py", "b.py"], "sha", db) == 2
    assert calls == {"commits": 2, "pulls": 2}

    calls["commits"] = 0
    await recall.remember_history("o/r", ["a.py", "b.py"], "sha", db)   # cached now
    assert calls["commits"] == 0

    prs = await store.recent_prs_for_file("o/r", "a.py", 5, db)
    assert prs and prs[0]["pr"] == 7


async def test_remember_history_without_github_is_noop(monkeypatch, db):
    await store.init(db)
    monkeypatch.setattr(github, "configured", lambda: False)
    assert await recall.remember_history("o/r", ["a.py"], "sha", db) == 0

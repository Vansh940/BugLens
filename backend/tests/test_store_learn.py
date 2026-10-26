import pytest

from buglens import store


@pytest.fixture(autouse=True)
async def _schema(db):
    await store.init(db)
    return db


def _finding(fingerprint, path, lens, title="t"):
    return {"fingerprint": fingerprint, "path": path, "line": 1, "severity": "major",
            "category": "bug", "lens": lens, "title": title, "body": "b",
            "fix": "", "confidence": 0.7}


async def test_lens_outcome_stats_shape(db):
    run1 = await store.claim_run("o/r", 1, "sha1", None, db)
    run2 = await store.claim_run("o/r", 2, "sha2", None, db)

    await store.save_findings(run1, [
        _finding("fpA", "a.py", "correctness", "A"),
        _finding("fpB", "b.py", "correctness", "B"),
    ], db)
    await store.save_findings(run2, [
        _finding("fpC", "c.py", "security", "C"),
        _finding("fpD", "d.py", None, "D"),
    ], db)

    await store.record_outcome("o/r", "fpA", "up", path=db)
    await store.record_outcome("o/r", "fpB", "down", path=db)
    await store.record_outcome("o/r", "fpB", "down", path=db)

    stats = await store.lens_outcome_stats("o/r", db)
    assert stats["correctness"] == {"raised": 2, "up": 1, "down": 2,
                                    "fixed": 0, "unfixed": 0}
    assert stats["security"] == {"raised": 1, "up": 0, "down": 0,
                                  "fixed": 0, "unfixed": 0}
    assert stats["unknown"] == {"raised": 1, "up": 0, "down": 0,
                                 "fixed": 0, "unfixed": 0}
    assert await store.lens_outcome_stats("other/repo", db) == {}


async def test_lens_outcome_stats_counts_resolved(db):
    run = await store.claim_run("o/r", 1, "sha1", None, db)
    await store.save_findings(run, [_finding("fpX", "x.py", "tests", "X")], db)
    await store.record_outcome("o/r", "fpX", "resolved", path=db)
    await store.record_outcome("o/r", "fpX", "unresolved", path=db)

    stats = await store.lens_outcome_stats("o/r", db)
    assert stats["tests"] == {"raised": 1, "up": 0, "down": 0,
                              "fixed": 1, "unfixed": 1}


async def test_dismissal_scopes_groups_by_directory(db):
    run = await store.claim_run("o/r", 1, "sha1", None, db)
    await store.save_findings(run, [
        _finding("fp1", "app/api/users.py", "correctness", "users bug"),
        _finding("fp2", "app/api/orders.py", "correctness", "orders bug"),
    ], db)
    await store.record_outcome("o/r", "fp1", "down", path=db)
    await store.record_outcome("o/r", "fp2", "down", path=db)

    scopes = await store.dismissal_scopes("o/r", db)
    assert len(scopes) == 1
    assert scopes[0]["scope"] == "app/api/*"
    assert scopes[0]["lens"] == "correctness"
    assert scopes[0]["downs"] == 2
    assert scopes[0]["ups"] == 0
    assert scopes[0]["sample"]


async def test_dismissal_scopes_root_scope_for_no_directory(db):
    run = await store.claim_run("o/r", 1, "sha1", None, db)
    await store.save_findings(run, [_finding("fp1", "solo.py", "tests", "solo")], db)
    await store.record_outcome("o/r", "fp1", "down", path=db)
    await store.record_outcome("o/r", "fp1", "down", path=db)

    scopes = await store.dismissal_scopes("o/r", db)
    assert scopes[0]["scope"] == "*"
    assert scopes[0]["downs"] == 2


async def test_dismissal_scopes_respects_threshold(db):
    run = await store.claim_run("o/r", 1, "sha1", None, db)
    await store.save_findings(run, [
        _finding("fp1", "app/api/users.py", "correctness"),
        _finding("fp2", "app/api/orders.py", "correctness"),
    ], db)
    await store.record_outcome("o/r", "fp1", "down", path=db)
    await store.record_outcome("o/r", "fp2", "down", path=db)

    assert await store.dismissal_scopes("o/r", db) != []
    assert await store.dismissal_scopes("o/r", db, threshold=3) == []


async def test_save_drops_buckets_reasons(db):
    run = await store.claim_run("o/r", 1, "sha1", None, db)
    dropped = [
        {"lens": "correctness", "drop_reason": "machine: line not in diff", "title": "x"},
        {"lens": "security", "title": "y"},
    ]
    written = await store.save_drops(run, "o/r", dropped, db)
    assert written == 2
    assert await store.drop_stats("o/r", db) == {"machine": 1, "unknown": 1}


async def test_save_drops_empty_returns_zero(db):
    assert await store.save_drops("run", "o/r", [], db) == 0


async def test_drop_stats_unknown_repo_is_empty(db):
    assert await store.drop_stats("nope/repo", db) == {}

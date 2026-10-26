import asyncio

from fastapi.testclient import TestClient

from buglens import store
from main import app

REPO = "o/r"
KEY = "sekret"


def _seed(db_path: str) -> None:
    async def go() -> None:
        await store.init(db_path)
        run = await store.claim_run(REPO, 1, "sha1", None, db_path)
        await store.save_findings(run, [
            {"fingerprint": "fp1", "path": "app/api/users.py", "line": 1,
             "severity": "major", "category": "bug", "lens": "correctness",
             "title": "users bug", "body": "b", "fix": "", "confidence": 0.7},
            {"fingerprint": "fp2", "path": "app/api/orders.py", "line": 1,
             "severity": "major", "category": "bug", "lens": "correctness",
             "title": "orders bug", "body": "b", "fix": "", "confidence": 0.7},
            {"fingerprint": "fp3", "path": "app/api/reports.py", "line": 1,
             "severity": "major", "category": "bug", "lens": "correctness",
             "title": "reports bug", "body": "b", "fix": "", "confidence": 0.7},
        ], db_path)
        for fp in ("fp1", "fp2", "fp3"):
            await store.record_outcome(REPO, fp, "down", path=db_path)
        await store.record_outcome(REPO, "fpX", "down", path=db_path)
        await store.save_drops("run1", REPO, [
            {"lens": "correctness", "drop_reason": "machine: line not in diff", "title": "x"},
            {"lens": "security", "drop_reason": "model: false positive", "title": "y"},
        ], db_path)

    asyncio.run(go())


def _client():
    return TestClient(app)


def test_learn_requires_admin_key(db, monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    with _client() as c:
        assert c.get("/api/v1/engine/learn", params={"repo": REPO}).status_code == 503


def test_learn_rejects_wrong_key(db, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", KEY)
    with _client() as c:
        r = c.get("/api/v1/engine/learn", params={"repo": REPO},
                  headers={"X-Admin-Key": "nope"})
        assert r.status_code == 401


def test_learn_returns_stats_scopes_and_candidates(db, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", KEY)
    _seed(db)
    with _client() as c:
        r = c.get("/api/v1/engine/learn", params={"repo": REPO},
                  headers={"X-Admin-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["repo"] == REPO
    assert body["lens_stats"]["correctness"]["raised"] == 3
    assert body["lens_stats"]["correctness"]["down"] == 3
    assert any(s["scope"] == "app/api/*" for s in body["scopes"])
    assert body["candidates"] and "ignore_paths:" in body["candidates"][0]["yaml"]


def test_engine_stats_includes_drops(db, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", KEY)
    _seed(db)
    with _client() as c:
        r = c.get("/api/v1/engine/stats", params={"repo": REPO},
                  headers={"X-Admin-Key": KEY})
    assert r.status_code == 200
    assert r.json()["drops"] == {"machine": 1, "model": 1}

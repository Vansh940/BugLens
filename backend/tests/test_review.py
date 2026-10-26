from fastapi.testclient import TestClient

from buglens import pipeline
from buglens.pipeline import ReviewResult
from main import app


def _snippet_result():
    return ReviewResult(
        status="ok", summary="one blocking issue", score=64, verdict="blocking",
        findings=[{"severity": "blocker", "category": "security", "line": 1, "title": "eval",
                   "body": "rce", "fix": "ast.literal_eval", "fingerprint": "fp",
                   "suggestion": None, "confidence": 0.9, "lens": "security", "path": "snippet"}],
        positives=["clear naming"], model_used="test-model",
    )


def test_review_endpoint_returns_legacy_shape(monkeypatch):
    async def fake_snippet(code, language, filename="snippet", focus=None, rules=None,
                           db=None, repo=None, related=None, imports=None):
        return _snippet_result()

    monkeypatch.setattr(pipeline, "review_snippet", fake_snippet)
    with TestClient(app) as client:
        r = client.post("/api/v1/review", json={"code": "eval(x)\n" * 3, "language": "python"})
    assert r.status_code == 200
    body = r.json()
    assert body["issues"][0]["severity"] == "critical"      # blocker -> critical for the extension
    assert body["issues"][0]["line_number"] == 1
    assert body["positive_aspects"] == ["clear naming"]
    assert body["review_id"]


def test_review_endpoint_rejects_too_short_code():
    with TestClient(app) as client:
        r = client.post("/api/v1/review", json={"code": "x", "language": "python"})
    assert r.status_code == 400


def test_review_endpoint_503_without_llm(monkeypatch):
    from buglens import llm
    monkeypatch.setattr(llm, "configured", lambda: False)
    with TestClient(app) as client:
        r = client.post("/api/v1/review", json={"code": "print('hello world')\n", "language": "python"})
    assert r.status_code == 503


def test_feedback_endpoint_records_and_suppresses(monkeypatch, tmp_path):
    monkeypatch.setenv("BUGLENS_DB", str(tmp_path / "fb.sqlite"))
    with TestClient(app) as client:
        first = client.post("/api/v1/feedback", json={"repo": "o/r", "fingerprint": "fp1", "verdict": "bad"})
        second = client.post("/api/v1/feedback", json={"repo": "o/r", "fingerprint": "fp1", "verdict": "bad"})
    assert first.status_code == 200
    assert second.json()["now_suppressed"] is True


def test_engine_repos_lists_reviewed_repos(monkeypatch, tmp_path):
    import asyncio

    from buglens import store

    db = str(tmp_path / "repos.sqlite")
    monkeypatch.setenv("BUGLENS_DB", db)

    async def seed():
        await store.init(db)
        run_id = await store.claim_run("o/r", 1, "sha", "base", db)
        await store.finish_run(run_id, "looks-good", 90, 1, True, db)
        await store.record_outcome("o/r", "fp", "resolved", "t", db)

    asyncio.run(seed())

    with TestClient(app) as client:
        r = client.get("/api/v1/engine/repos")
    assert r.status_code == 200
    repos = r.json()["repos"]
    assert repos and repos[0]["repo"] == "o/r"
    assert repos[0]["resolution"]["resolved"] == 1


def test_review_accepts_repo_awareness_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("BUGLENS_DB", str(tmp_path / "snip.sqlite"))

    async def fake_snippet(code, language, filename="snippet", focus=None, rules=None,
                           db=None, repo=None, related=None, imports=None):
        assert repo == "o/r" and imports == ["pkg.util"]
        return _snippet_result()

    monkeypatch.setattr(pipeline, "review_snippet", fake_snippet)
    with TestClient(app) as client:
        r = client.post("/api/v1/review", json={
            "code": "eval(x)\n" * 3, "language": "python", "filename": "a.py",
            "repo": "o/r", "branch": "main", "imports": ["pkg.util"],
            "related": {"pkg/util.py": "def f(): ..."},
        })
    assert r.status_code == 200

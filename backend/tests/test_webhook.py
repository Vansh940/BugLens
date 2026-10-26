import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from buglens import chat, pipeline
from main import app

SECRET = "test-secret"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(payload: dict, event: str, *, secret: str = SECRET, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": signature or _sig(body, secret)}
    with TestClient(app) as client:
        return client.post("/api/v1/webhook", content=body, headers=headers)


def test_invalid_signature_is_rejected():
    r = _post({"action": "opened"}, "pull_request", signature="sha256=deadbeef")
    assert r.status_code == 401


def test_missing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    r = _post({"action": "opened"}, "pull_request")
    assert r.status_code == 401


def test_ping_returns_pong():
    r = _post({"zen": "keep it simple"}, "ping")
    assert r.status_code == 200 and r.json()["status"] == "pong"


def test_irrelevant_event_is_ignored():
    r = _post({"action": "opened"}, "push")
    assert r.json()["status"] == "ignored"


def _record(monkeypatch):
    seen = {}

    async def fake_review_pr(owner, repo, number, **kwargs):
        seen.update(owner=owner, repo=repo, number=number, **kwargs)
        return SimpleNamespace(status="ok", score=95, findings=[], posted=True)

    monkeypatch.setattr(pipeline, "review_pr", fake_review_pr)
    return seen


def _pr_payload(**pr_over):
    pr = {"number": 5, "draft": False, "user": {"login": "dev"},
          "base": {"sha": "base"}, "head": {"sha": "head"}}
    pr.update(pr_over)
    return {"action": "opened", "repository": {"name": "r", "owner": {"login": "o"}}, "pull_request": pr}


def test_pr_opened_triggers_review(monkeypatch):
    seen = _record(monkeypatch)
    r = _post(_pr_payload(), "pull_request")
    assert r.json()["status"] == "processing"
    assert seen["number"] == 5 and seen["force"] is False


def test_draft_pr_is_ignored(monkeypatch):
    _record(monkeypatch)
    r = _post(_pr_payload(draft=True), "pull_request")
    assert r.json()["reason"] == "draft"


def test_bot_author_is_ignored(monkeypatch):
    _record(monkeypatch)
    r = _post(_pr_payload(user={"login": "dependabot[bot]", "type": "Bot"}), "pull_request")
    assert r.json()["reason"] == "bot-author"


def test_unreview_action_is_ignored(monkeypatch):
    _record(monkeypatch)
    payload = _pr_payload()
    payload["action"] = "labeled"
    r = _post(payload, "pull_request")
    assert r.json()["reason"].startswith("action:")


def test_mention_triggers_on_demand_review(monkeypatch):
    seen = _record(monkeypatch)
    payload = {
        "action": "created",
        "repository": {"name": "r", "owner": {"login": "o"}},
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "comment": {"body": "hey @BugLens please review", "user": {"login": "dev"}},
    }
    r = _post(payload, "issue_comment")
    assert r.json()["status"] == "processing" and r.json()["trigger"] == "mention"
    assert seen["number"] == 7 and seen["force"] is True


def test_reaction_records_feedback_and_suppresses(db):
    from buglens import pipeline, store

    asyncio.run(store.init(db))
    fp = "abcdefabcdefabcdefabcdefabcdef12"
    payload = {
        "action": "created", "reaction": {"content": "-1"},
        "comment": {"body": f"some bug\n\n{pipeline.fingerprint_marker(fp)}"},
        "repository": {"full_name": "o/r"},
    }
    assert _post(payload, "reaction").json()["status"] == "feedback-recorded"
    assert _post(payload, "reaction").json()["verdict"] == "down"
    assert fp in asyncio.run(store.suppressed("o/r"))


def test_forged_fingerprint_marker_is_rejected(db):
    """Anyone can comment on a PR; without the HMAC tag they must not drive outcomes."""
    from buglens import pipeline, store

    asyncio.run(store.init(db))
    fp = "abcdefabcdefabcdefabcdefabcdef12"
    forged = {
        "action": "created", "reaction": {"content": "-1"},
        "comment": {"body": f"not ours\n\n<!-- buglens:fp={fp} -->"},
        "repository": {"full_name": "o/r"},
    }
    assert _post(forged, "reaction").json()["status"] == "ignored"
    assert asyncio.run(store.suppressed("o/r")) == set()
    assert pipeline.extract_fingerprint(forged["comment"]["body"]) is None


def test_reaction_without_marker_is_ignored(db):
    payload = {"action": "created", "reaction": {"content": "+1"},
               "comment": {"body": "just a comment"}, "repository": {"full_name": "o/r"}}
    assert _post(payload, "reaction").json()["reason"] == "reaction"


def test_merged_pr_measures_resolution(monkeypatch):
    seen = {}

    async def fake_measure(owner, repo, number, head_sha, db=None):
        seen.update(owner=owner, number=number, head_sha=head_sha)

    monkeypatch.setattr(pipeline, "measure_resolution", fake_measure)
    payload = _pr_payload()
    payload["action"] = "closed"
    payload["pull_request"]["merged"] = True
    payload["pull_request"]["merge_commit_sha"] = "merge123"
    r = _post(payload, "pull_request")
    assert r.json()["status"] == "measuring-resolution"
    assert seen == {"owner": "o", "number": 5, "head_sha": "merge123"}


def test_comment_without_mention_is_ignored(monkeypatch):
    _record(monkeypatch)
    payload = {
        "action": "created",
        "repository": {"name": "r", "owner": {"login": "o"}},
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "comment": {"body": "nice work", "user": {"login": "dev"}},
    }
    assert _post(payload, "issue_comment").json()["reason"] == "no-mention"


def test_mention_chat_command_routes_to_chat(monkeypatch):
    seen = {}
    called = {"review": False}

    async def fake_handle(owner, repo, number, body, db=None, head_sha=""):
        seen.update(owner=owner, number=number, body=body)
        return {"command": "explain", "posted": True}

    async def fake_review(*a, **k):
        called["review"] = True

    monkeypatch.setattr(chat, "handle_command", fake_handle)
    monkeypatch.setattr(pipeline, "review_pr", fake_review)
    payload = {
        "action": "created",
        "repository": {"name": "r", "owner": {"login": "o"}},
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "comment": {"body": "@buglens explain the change", "user": {"login": "dev"}},
    }
    r = _post(payload, "issue_comment")
    assert r.json()["status"] == "chatting" and r.json()["command"] == "explain"
    assert seen["number"] == 7 and called["review"] is False


def test_mention_in_review_comment_routes_to_chat(monkeypatch):
    seen = {}

    async def fake_handle(owner, repo, number, body, db=None, head_sha=""):
        seen.update(number=number, body=body)
        return {"command": "why", "posted": True}

    monkeypatch.setattr(chat, "handle_command", fake_handle)
    payload = {
        "action": "created",
        "repository": {"name": "r", "owner": {"login": "o"}},
        "pull_request": {"number": 11},
        "comment": {"body": "@buglens why does this matter?", "user": {"login": "dev"}},
    }
    r = _post(payload, "pull_request_review_comment")
    assert r.json()["status"] == "chatting" and r.json()["command"] == "why"
    assert seen["number"] == 11

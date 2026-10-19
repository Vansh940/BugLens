from buglens import chat, github, llm
from buglens.github import PRInfo
from buglens.llm import LLMResult

PATCH = "@@ -0,0 +1,2 @@\n+import os  # sys path setup\n+value = compute_something()\n"


def _patch_github(monkeypatch):
    monkeypatch.setattr(github, "configured", lambda: True)

    async def get_pr(*a, **k):
        return PRInfo("o", "r", 1, "base", "head", False, "Add user model", "body", "dev", "open")

    async def get_files(*a, **k):
        return [
            {"filename": "src/models/user.py", "status": "added", "patch": PATCH},
            {"filename": "tests/test_user.py", "status": "added", "patch": PATCH},
        ]

    monkeypatch.setattr(github, "get_pr", get_pr)
    monkeypatch.setattr(github, "get_pr_files", get_files)


def test_parse_command_routes():
    assert chat.parse_command("@buglens review") == ("review", "review")
    assert chat.parse_command("@buglens re-review please") == ("review", "re-review please")
    assert chat.parse_command("hey @BugLens please review") == ("review", "please review")
    assert chat.parse_command("@buglens") == ("review", "")
    assert chat.parse_command("@buglens explain the data flow") == ("explain", "the data flow")
    assert chat.parse_command("@buglens why") == ("why", "")
    assert chat.parse_command("@buglens why did the review flag this") == ("why", "did the review flag this")
    assert chat.parse_command("@buglens fix") == ("fix", "")
    assert chat.parse_command("@BugLens: what does this touch?") == ("ask", "what does this touch?")
    assert chat.parse_command("no mention here") == ("ask", "")


def test_handoff_prompts_are_copy_ready():
    findings = [{"path": "src/a.py", "line": 3, "title": "NPE on empty cart",
                 "severity": "major", "evidence": "cart.items[0]"}]
    out = chat.handoff_prompts(findings, "o", "r", 9, "headsha")
    assert "src/a.py:3" in out and "NPE on empty cart" in out
    assert out.count("```") == 2
    assert "#9" in out


def test_handoff_empty_is_honest():
    assert "No open BugLens findings" in chat.handoff_prompts([], "o", "r", 1)


async def test_explain_is_deterministic_change_stack(monkeypatch):
    _patch_github(monkeypatch)
    out = await chat.answer("o", "r", 1, "explain", "")
    assert "What this PR changes" in out
    assert "Data models & types" in out
    assert "Tests" in out


async def test_ask_returns_the_model_answer(monkeypatch):
    _patch_github(monkeypatch)

    async def fake(messages, **k):
        return LLMResult(data={}, text="It adds a User model.", model="m")

    monkeypatch.setattr(llm, "complete_text", fake)
    assert await chat.answer("o", "r", 1, "ask", "what changed?") == "It adds a User model."


async def test_why_falls_back_honestly_when_the_model_is_down(monkeypatch):
    _patch_github(monkeypatch)

    async def boom(messages, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm, "complete_text", boom)
    out = await chat.answer("o", "r", 1, "why", "")
    assert "unreachable" in out
    assert "What this PR changes" in out


async def test_handle_command_posts_a_threaded_reply(monkeypatch):
    _patch_github(monkeypatch)
    posted = {}

    async def post(owner, repo, number, body):
        posted.update(number=number, body=body)
        return {}

    monkeypatch.setattr(github, "post_issue_comment", post)
    res = await chat.handle_command("o", "r", 5, "@buglens explain")
    assert res == {"command": "explain", "posted": True}
    assert posted["number"] == 5
    assert posted["body"].startswith("<!-- buglens:reply:5 -->")


async def test_handle_command_hands_review_back_to_the_pipeline(monkeypatch):
    res = await chat.handle_command("o", "r", 5, "@buglens re-review")
    assert res == {"command": "review", "handed": "review_pipeline"}


async def test_fix_with_no_findings_is_honest(monkeypatch):
    _patch_github(monkeypatch)
    out = await chat.answer("o", "r", 1, "fix", "")
    assert "No open BugLens findings" in out

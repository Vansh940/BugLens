from buglens import github, llm, pipeline, secretscan
from buglens.diff import parse_patch
from buglens.github import PRInfo
from buglens.llm import LLMResult

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
PATCH = f'@@ -1,1 +1,2 @@\n x = 1\n+AWS_KEY = "{AWS_KEY}"\n'


def test_scanner_finds_and_masks_aws_key():
    fd = parse_patch(PATCH, "config.py")
    found = secretscan.scan(fd)
    assert len(found) == 1
    assert found[0]["severity"] == "blocker" and found[0]["line"] == 2
    assert found[0]["deterministic"] is True
    assert AWS_KEY not in found[0]["evidence"]      # never reprint a live credential
    assert "****" in found[0]["evidence"]


def test_scanner_ignores_placeholders():
    fd = parse_patch('@@ -1,1 +1,2 @@\n x = 1\n+password = "changeme_placeholder"\n', "a.py")
    assert secretscan.scan(fd) == []


async def _empty_llm(messages, **kwargs):
    system = messages[0]["content"]
    if "verifying findings" in system:
        return LLMResult(data={"verdicts": []})
    if "PR-level summary" in system:
        return LLMResult(data={"summary": "s", "score": 90, "verdict": "needs-attention",
                               "highlights": [], "positives": []})
    return LLMResult(data={"findings": [], "positives": []})


async def test_deterministic_secret_survives_verify_and_posts(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _empty_llm)
    monkeypatch.setattr(github, "configured", lambda: True)

    async def get_pr(*a, **k):
        return PRInfo("o", "r", 1, "base", "head", False, "t", "b", "a", "open")

    async def get_files(*a, **k):
        return [{"filename": "config.py", "status": "modified", "patch": PATCH}]

    async def get_tree(*a, **k):
        return ["config.py"]

    async def get_content(*a, **k):
        return "x = 1\n"

    monkeypatch.setattr(github, "get_pr", get_pr)
    monkeypatch.setattr(github, "get_pr_files", get_files)
    monkeypatch.setattr(github, "get_repo_tree", get_tree)
    monkeypatch.setattr(github, "get_file_content", get_content)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert any(f["lens"] == "secrets" and f["severity"] == "blocker" for f in res.findings)
    assert "secrets" in res.lenses_run

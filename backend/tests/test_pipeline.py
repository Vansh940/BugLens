import re

import pytest

from buglens import github, llm, pipeline, store
from buglens.github import PRInfo
from buglens.llm import LLMResult
from buglens.rules import load_rules

PATCH = "@@ -1,3 +1,4 @@\n x = 1\n+bad = eval(user_input)\n print(x)\n"


async def _fake_llm(messages, **kwargs):
    system = messages[0]["content"]
    if "verifying findings" in system:
        return LLMResult(data={"verdicts": [{"verdict": "keep", "decidable": True, "reason": "ok"}]})
    if "PR-level summary" in system:
        return LLMResult(data={"summary": "sum", "score": 88, "verdict": "needs-attention",
                               "highlights": ["h"], "positives": ["p"]})
    matched = re.search(r"LENS:\s*([^\s|(]+)", messages[1]["content"])
    lens = matched.group(1) if matched else ""
    if lens == "security":
        return LLMResult(data={"findings": [{
            "severity": "major", "category": "security", "line": 2, "title": "eval on user input",
            "body": "remote code execution", "fix": "use ast.literal_eval",
            "evidence": "bad = eval(user_input)", "confidence": 0.9,
            "suggestion": "safe = ast.literal_eval(user_input)",
        }], "positives": ["tidy"]})
    return LLMResult(data={"findings": [], "positives": []})


def _patch_github(monkeypatch, files, *, capture=None):
    monkeypatch.setattr(github, "configured", lambda: True)

    async def get_pr(*a, **k):
        return PRInfo("o", "r", 1, "basesha", "headsha", False, "title", "body", "author", "open")

    async def get_pr_files(*a, **k):
        return files

    async def get_tree(*a, **k):
        return ["a.py", "README.md"]

    async def get_content(*a, **k):
        return "x = 1\nbad = eval(user_input)\nprint(x)\n"

    async def post_review(owner, repo, number, commit_id, body, comments, event="COMMENT"):
        if capture is not None:
            capture["comments"] = comments
            capture["body"] = body
            capture["event"] = event
        return {"id": 1}

    async def no_issues(*a, **k):
        return []

    monkeypatch.setattr(github, "list_review_comments", no_issues)

    monkeypatch.setattr(github, "get_pr", get_pr)
    monkeypatch.setattr(github, "get_pr_files", get_pr_files)
    monkeypatch.setattr(github, "get_repo_tree", get_tree)
    monkeypatch.setattr(github, "get_file_content", get_content)
    monkeypatch.setattr(github, "post_review", post_review)
    monkeypatch.setattr(github, "closing_issues", no_issues)
    monkeypatch.setattr(github, "get_pr_commit_messages", no_issues)
    monkeypatch.setattr(github, "get_commits_for_path", no_issues)
    monkeypatch.setattr(github, "pulls_for_commit", no_issues)


async def test_review_pr_dry_run_keeps_only_verified_findings(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert res.status == "dry-run"
    assert [f["title"] for f in res.findings] == ["eval on user input"]
    assert res.findings[0]["fingerprint"]
    assert "security" in res.lenses_run and "merge" in res.lenses_run
    assert res.score == 88


async def test_review_pr_merges_deterministic_linter_findings(monkeypatch, db):
    import json

    from buglens import adapters

    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    monkeypatch.setattr(adapters, "_installed", lambda a: True)
    monkeypatch.setattr(adapters, "_run", lambda *a, **k: json.dumps([
        {"code": "F821", "message": "undefined name", "location": {"row": 2, "column": 1}},
    ]))
    rules = load_rules()
    rules.linters = ["ruff"]

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=rules)

    assert "lint" in res.lenses_run
    assert any(f.get("linter") == "ruff" and f.get("deterministic") for f in res.findings)
    assert any("linters: ruff" in n for n in res.engine_notes)


async def test_walkthrough_renders_change_stack_and_pre_merge(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)
    md = pipeline.render_summary_md(res, load_rules())

    assert "Change stack" in md
    assert "**Core logic**" in md
    assert "Pre-merge checks" in md
    assert "| Check | Status | Detail |" in md
    assert "| Secrets |" in md

    rules = load_rules()
    rules.pre_merge_checks = False
    assert "Pre-merge checks" not in pipeline.render_summary_md(res, rules)


async def test_post_requests_changes_is_opt_in_and_only_when_blocking(monkeypatch, db):
    cap = {}
    _patch_github(monkeypatch, [], capture=cap)
    blocking = pipeline.ReviewResult(verdict="blocking", findings=[
        {"path": "a.py", "line": 2, "severity": "blocker", "title": "t", "body": "b"},
    ])

    rules = load_rules()
    assert await pipeline._post("o", "r", 1, "sha", blocking, rules)
    assert cap["event"] == "COMMENT"                      # default never blocks

    rules.request_changes = True
    assert await pipeline._post("o", "r", 1, "sha", blocking, rules)
    assert cap["event"] == "REQUEST_CHANGES"

    calm = pipeline.ReviewResult(verdict="needs-attention", findings=[
        {"path": "a.py", "line": 2, "severity": "major", "title": "t", "body": "b"},
    ])
    assert await pipeline._post("o", "r", 1, "sha", calm, rules)
    assert cap["event"] == "COMMENT"                      # only for a blocking verdict


async def test_review_pr_is_idempotent_per_revision(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    first = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)
    second = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert first.status == "dry-run"
    assert second.status == "skipped"


async def test_review_pr_force_allows_re_review(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)
    again = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, force=True)
    assert again.status == "dry-run"


async def test_posting_uses_inline_comments_with_suggestion(monkeypatch, db):
    capture = {}
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}], capture=capture)

    res = await pipeline.review_pr("o", "r", 1, db=db)

    assert res.posted is True
    assert capture["comments"][0]["path"] == "a.py"
    assert capture["comments"][0]["line"] == 2
    assert "```suggestion" in capture["comments"][0]["body"]
    assert "BugLens" in capture["body"]


async def test_second_review_reuses_file_cache(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    calls = {"n": 0}
    real = pipeline._review_file

    async def counting(*a, **k):
        calls["n"] += 1
        return await real(*a, **k)

    monkeypatch.setattr(pipeline, "_review_file", counting)

    await pipeline.review_pr("o", "r", 1, db=db)
    first = calls["n"]
    await pipeline.review_pr("o", "r", 1, db=db, force=True)
    assert first >= 1 and calls["n"] == first   # identical diff → cache hit, no second lens run


async def test_summary_includes_walkthrough_and_context(monkeypatch, db):
    capture = {}
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}], capture=capture)

    res = await pipeline.review_pr("o", "r", 1, db=db)

    assert "Walkthrough" in capture["body"]
    assert "Review effort" in capture["body"]
    assert "Context used" in capture["body"] and "README.md" in capture["body"]
    assert res.review_effort >= 1 and res.context_sources


async def _seed_feedback(db_path):
    await store.init(db_path)
    for i in range(5):
        run = await store.claim_run("o/r", 100 + i, f"seedsha{i}", None, db_path)
        await store.save_findings(run, [{
            "fingerprint": f"seedfp{i}", "path": f"app/api/f{i}.py", "line": 1,
            "severity": "major", "category": "bug", "lens": "correctness",
            "title": f"seed{i}", "body": "b", "fix": "", "confidence": 0.7}], db_path)
        await store.record_outcome("o/r", f"seedfp{i}", "down", path=db_path)


async def test_learn_advisory_reaches_prompt_and_demotes_lens(monkeypatch, db):
    await _seed_feedback(db)
    prompts: list[str] = []

    async def capturing(messages, **kwargs):
        if len(messages) > 1:
            prompts.append(messages[1]["content"])
        return await _fake_llm(messages, **kwargs)

    monkeypatch.setattr(llm, "complete_json", capturing)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    res = await pipeline.review_pr("o", "r", 1, db=db)

    assert any("Feedback on this repo" in p for p in prompts)
    assert any("app/api/*" in p for p in prompts)
    assert any(n.startswith("adaptive:") for n in res.engine_notes)
    assert any(n.startswith("learned from feedback:") for n in res.engine_notes)


async def test_verify_drops_are_persisted(monkeypatch, db):
    async def dropping(messages, **kwargs):
        system = messages[0]["content"]
        if "verifying findings" in system:
            return LLMResult(data={"verdicts": [{"verdict": "drop", "decidable": True,
                                                 "reason": "false positive"}]})
        if "PR-level summary" in system:
            return LLMResult(data={"summary": "s", "score": 90, "verdict": "needs-attention",
                                   "highlights": [], "positives": []})
        matched = re.search(r"LENS:\s*([^\s|(]+)", messages[1]["content"])
        if matched and matched.group(1) == "security":
            return LLMResult(data={"findings": [{
                "severity": "major", "category": "security", "line": 2, "title": "eval",
                "body": "rce", "fix": "f", "evidence": "bad = eval(user_input)",
                "confidence": 0.9,
            }], "positives": []})
        return LLMResult(data={"findings": [], "positives": []})

    monkeypatch.setattr(llm, "complete_json", dropping)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    await pipeline.review_pr("o", "r", 1, db=db)

    assert await store.drop_stats("o/r", db) == {"model": 1}


async def test_publish_check_run_passes_annotations(monkeypatch, db):
    cap = {}
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    async def fake_create(owner, repo, head_sha, name, conclusion, title, summary, **kw):
        cap.update({"name": name, "conclusion": conclusion, "title": title,
                    "annotations": kw.get("annotations"), "text": kw.get("text")})
        return {"id": 9}

    monkeypatch.setattr(github, "create_check_run", fake_create)
    rules = load_rules()
    rules.post_check_run = True

    res = await pipeline.review_pr("o", "r", 1, db=db, rules=rules)

    assert cap["name"] == "BugLens"
    assert cap["conclusion"] == "neutral"
    anns = cap["annotations"] or []
    assert anns and anns[0]["annotation_level"] == "warning"
    assert anns[0]["path"] == "a.py" and anns[0]["start_line"] == 2
    assert cap["text"].startswith("r#1")
    assert any(n.startswith("check run:") for n in res.engine_notes)


async def test_check_run_disabled_makes_no_call(monkeypatch, db):
    called = {}
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    async def fake_create(*a, **k):
        called["yes"] = True
        return {"id": 1}

    monkeypatch.setattr(github, "create_check_run", fake_create)

    await pipeline.review_pr("o", "r", 1, db=db)

    assert "yes" not in called


async def test_check_run_403_is_swallowed(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    async def forbidden(*a, **k):
        raise github.GitHubError("403: resource not accessible by personal access token")

    monkeypatch.setattr(github, "create_check_run", forbidden)
    rules = load_rules()
    rules.post_check_run = True

    res = await pipeline.review_pr("o", "r", 1, db=db, rules=rules)

    assert res is not None
    assert not any(n.startswith("check run:") for n in res.engine_notes)


async def test_check_run_per_lens_grouping(monkeypatch, db):
    names = []
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    async def fake_create(owner, repo, head_sha, name, conclusion, title, summary, **kw):
        names.append(name)
        return {"id": 1}

    monkeypatch.setattr(github, "create_check_run", fake_create)
    rules = load_rules()
    rules.post_check_run = True
    rules.check_run_grouping = "per_lens"

    await pipeline.review_pr("o", "r", 1, db=db, rules=rules)

    assert names and all(n.startswith("BugLens:") for n in names)


def test_review_effort_is_bounded():
    assert pipeline._review_effort([]) == 1
    assert 1 <= pipeline._review_effort([type("F", (), {"added_chars": 10 ** 6})()]) <= 5


async def test_summary_only_posts_no_inline_comments(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    called = {}

    async def post_review(*a, **k):
        called["inline"] = True

    async def upsert(owner, repo, number, marker, body):
        called["summary"] = body

    monkeypatch.setattr(github, "post_review", post_review)
    monkeypatch.setattr(github, "upsert_marked_comment", upsert)
    rules = load_rules()
    rules.summary_only = True

    res = await pipeline.review_pr("o", "r", 1, db=db, rules=rules)

    assert res.posted is True
    assert "summary" in called and "inline" not in called


async def test_char_budget_skips_oversized_files(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    big = "@@ -1,1 +1,2 @@\n x = 1\n+" + "a" * 600 + "\n"
    files = [{"filename": "a.py", "status": "modified", "patch": PATCH},
             {"filename": "big.py", "status": "modified", "patch": big}]
    _patch_github(monkeypatch, files)
    rules = load_rules()
    rules.max_file_chars = 100

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=rules)

    assert any(s["path"] == "big.py" and "char budget" in s["reason"] for s in res.skipped)
    assert "big.py" not in res.files


async def test_snippet_review_maps_findings(monkeypatch):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    res = await pipeline.review_snippet("bad = eval(user_input)\n", "python", "a.py")
    assert res.findings
    payload = pipeline.to_review_response(res)
    assert payload["issues"][0]["severity"] in {"critical", "warning", "suggestion"}
    assert payload["issues"][0]["line_number"] == 1


async def test_finding_markdown_carries_hidden_fingerprint():
    md = pipeline.render_finding_md({
        "severity": "major", "category": "security", "title": "eval", "body": "b",
        "lens": "security", "confidence": 0.9, "fingerprint": "a" * 32,
    })
    assert pipeline.extract_fingerprint(md) == "a" * 32
    assert pipeline.extract_fingerprint("no marker here") is None


async def test_consensus_drops_a_finding_only_one_pass_saw(monkeypatch):
    import asyncio
    from buglens import lenses as L
    from buglens.diff import parse_patch

    rules = load_rules()
    rules.consensus = 3
    calls = {"n": 0}

    async def fake(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResult(data={"findings": [{
                "severity": "major", "category": "security", "line": 2, "title": "eval on user input",
                "body": "rce", "fix": "ast", "evidence": "bad = eval(user_input)", "confidence": 0.9,
            }], "positives": []})
        return LLMResult(data={"findings": [], "positives": []})

    monkeypatch.setattr(llm, "complete_json", fake)
    fd = parse_patch(PATCH, "a.py")
    kept, _, _ = await pipeline._run_lens(fd, L.BY_NAME["security"], rules, "", "", [], asyncio.Semaphore(3))
    assert kept == []          # 1/3 votes is not a majority


async def test_consensus_keeps_a_finding_all_passes_saw(monkeypatch):
    import asyncio
    from buglens import lenses as L
    from buglens.diff import parse_patch
    from buglens.rules import load_rules as _load

    rules = _load()
    rules.consensus = 3
    async def fake(messages, **kw):
        return LLMResult(data={"findings": [{
            "severity": "major", "category": "security", "line": 2, "title": "eval on user input",
            "body": "rce", "fix": "ast", "evidence": "bad = eval(user_input)", "confidence": 0.9,
        }], "positives": []})

    monkeypatch.setattr(llm, "complete_json", fake)
    fd = parse_patch(PATCH, "a.py")
    kept, _, _ = await pipeline._run_lens(fd, L.BY_NAME["security"], rules, "", "", [], asyncio.Semaphore(3))
    assert [f["title"] for f in kept] == ["eval on user input"]


async def test_measure_resolution_records_outcomes(monkeypatch, db):
    from buglens import store

    await store.init(db)
    run_id = await store.claim_run("o/r", 1, "sha", "base", db)
    finding = {
        "fingerprint": "fp-res", "path": "a.py", "line": 2, "severity": "major",
        "category": "security", "lens": "security", "title": "eval on user input",
        "body": "rce", "fix": "ast", "evidence": "bad = eval(user_input)", "confidence": 0.9,
    }
    await store.save_findings(run_id, [finding], db)
    await store.mark_posted(run_id, db)

    async def get_content(*a, **k):
        return "print('rewritten safely')\n"      # evidence is gone -> resolved

    monkeypatch.setattr(github, "get_file_content", get_content)
    monkeypatch.setattr(github, "configured", lambda: True)

    out = await pipeline.measure_resolution("o", "r", 1, "sha", db)
    assert out == {"resolved": 1, "unresolved": 0, "checked": 1}
    assert (await store.resolution_stats("o/r", db))["rate"] == 1.0


async def test_spec_lens_flags_unmet_ticket_criterion(monkeypatch, db):
    async def fake(messages, **kwargs):
        system = messages[0]["content"]
        if "validating a pull request against the ticket" in system:
            return LLMResult(data={
                "overall": "partial",
                "criteria": [
                    {"criterion": "reject empty input", "verdict": "not_addressed",
                     "evidence": None, "note": "no guard added"},
                    {"criterion": "log the retry count", "verdict": "addressed",
                     "evidence": "a.py:2", "note": "ok"},
                ],
                "unrelated_files": ["README.md"],
            })
        if "verifying findings" in system:
            return LLMResult(data={"verdicts": [{"verdict": "keep", "decidable": True, "reason": "ok"}]})
        if "PR-level summary" in system:
            return LLMResult(data={"summary": "s", "score": 80, "verdict": "needs-attention",
                                   "highlights": [], "positives": []})
        return LLMResult(data={"findings": [], "positives": []})

    async def closing_issues(*a, **k):
        return [{"number": 7, "title": "Harden input handling", "repo": "o/r",
                 "body": "## Acceptance criteria\n- [ ] reject empty input\n- [ ] log the retry count"}]

    monkeypatch.setattr(llm, "complete_json", fake)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    monkeypatch.setattr(github, "closing_issues", closing_issues)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)
    assert "spec" in res.lenses_run
    assert "Ticket: **partial**" in res.ticket_summary
    ticket_findings = [f for f in res.findings if f.get("ticket")]
    assert len(ticket_findings) == 1
    assert "reject empty input" in ticket_findings[0]["title"]


async def test_spec_lens_skipped_when_no_ticket(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)
    assert "spec" not in res.lenses_run
    assert res.ticket_summary == ""


async def test_recall_block_reaches_review_context(monkeypatch, db):
    from buglens import store

    await store.init(db)
    prior = {"fingerprint": "fp-old", "path": "a.py", "line": 2, "severity": "major",
             "category": "security", "lens": "security", "title": "missing timeout",
             "body": "b", "fix": "f", "evidence": "e", "confidence": 0.9}
    run_id = await store.claim_run("o/r", 9, "oldsha", "base", db)
    await store.save_findings(run_id, [prior], db)
    await store.mark_posted(run_id, db)
    await store.record_file_pr("o/r", "a.py", 9, "oldsha", "2026-01-01T00:00:00Z", db)

    seen: dict = {}

    async def capture_review(fd, lenses, rules, context_block, related_block, prior_good, sem):
        seen["context"] = context_block
        return [], [], {}, []

    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    monkeypatch.setattr(pipeline, "_review_file", capture_review)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)
    assert "prior reviews" in res.context_sources
    assert "Prior reviews on these files" in seen["context"]
    assert "missing timeout" in seen["context"]


async def test_rules_read_from_base_branch_and_nested(monkeypatch, db):
    seen: dict = {}
    served = {
        ".buglens.yml": "min_severity: major\n",
        "src/api/.buglens.yml": "max_comments: 3\n",
    }
    refs: list[tuple[str, str]] = []

    async def get_content(owner, repo, path, ref):
        if path in served:
            refs.append((path, ref))
            return served[path]
        return None

    async def capture_review(fd, lenses, rules, context_block, related_block, prior_good, sem):
        seen["rules"] = rules
        return [], [], {}, []

    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "src/api/h.py", "status": "modified", "patch": PATCH}])
    monkeypatch.setattr(github, "get_file_content", get_content)
    monkeypatch.setattr(pipeline, "_review_file", capture_review)

    await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    rules = seen["rules"]
    assert rules.max_comments == 3                 # nearest nested config wins
    assert rules.min_severity == "major"           # root config still applies
    assert {p for p, _ref in refs} >= {".buglens.yml", "src/api/.buglens.yml"}
    assert all(ref == "basesha" for _p, ref in refs)   # never the PR head


async def test_explicit_rules_skip_config_fetch(monkeypatch, db):
    from buglens.rules import load_rules

    fetched: list[str] = []
    seen: dict = {}

    async def get_content(owner, repo, path, ref):
        fetched.append(path)
        return "min_severity: major\n"

    async def capture_review(fd, lenses, rules, context_block, related_block, prior_good, sem):
        seen["rules"] = rules
        return [], [], {}, []

    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    monkeypatch.setattr(github, "get_file_content", get_content)
    monkeypatch.setattr(pipeline, "_review_file", capture_review)

    mine = load_rules({".buglens.yml": "max_comments: 1\n"})
    await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=mine)
    assert seen["rules"] is mine
    assert not [p for p in fetched if ".buglens" in p]


async def test_code_graph_feeds_related_context(monkeypatch, db):
    util = "def parse(value):\n    return int(value)\n"
    main = "from app.util import parse\n\n\ndef run(data):\n    return parse(data)\n"
    seen: dict = {}

    async def get_content(owner, repo, path, ref):
        return {"app/util.py": util, "app/main.py": main}.get(path)

    async def capture_review(fd, lenses, rules, context_block, related_block, prior_good, sem):
        seen["related"] = related_block
        return [], [], {}, []

    async def get_tree(*a, **k):
        return ["app/main.py", "app/util.py", "README.md"]

    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "app/main.py", "status": "modified", "patch": PATCH}],
                  capture=None)
    monkeypatch.setattr(github, "get_file_content", get_content)
    monkeypatch.setattr(github, "get_repo_tree", get_tree)
    monkeypatch.setattr(pipeline, "_review_file", capture_review)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)
    assert any("code graph" in s for s in res.context_sources)
    assert "app/util.py" in seen["related"]


# ── iteration 14: a review that could not run must not look clean ──────────────
async def test_all_lenses_failing_reports_incomplete_not_looks_good(monkeypatch, db):
    from buglens import store

    _patch_github(monkeypatch, [{"filename": "a.py", "patch": PATCH, "status": "modified"}])

    async def broken(messages, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm, "complete_json", broken)
    res = await pipeline.review_pr("o", "r", 1, db=db, rules=load_rules(), force=True)

    assert res.status == "incomplete"
    assert res.verdict == "incomplete"
    assert res.score == 0
    assert res.posted is False
    assert "could not run" in res.summary
    # and the run is recorded, so it is not re-claimed forever
    stats = await store.stats(db)
    assert stats["runs"] >= 1


async def test_one_working_lens_is_not_incomplete(monkeypatch, db):
    calls = {"n": 0}

    _patch_github(monkeypatch, [{"filename": "a.py", "patch": PATCH, "status": "modified"}])

    async def flaky(messages, **kwargs):
        calls["n"] += 1
        if "verifying findings" in messages[0]["content"] or "PR-level summary" in messages[0]["content"]:
            return await _fake_llm(messages, **kwargs)
        if calls["n"] % 2 == 0:
            raise RuntimeError("transient")
        return await _fake_llm(messages, **kwargs)

    monkeypatch.setattr(llm, "complete_json", flaky)
    res = await pipeline.review_pr("o", "r", 1, db=db, rules=load_rules(), force=True)
    assert res.status == "ok"


def test_graph_cache_reuses_and_invalidates(db):
    from buglens import store
    import asyncio

    async def flow():
        await store.init(db)
        await store.put_index_many("o/r", [{"path": "a.py", "sha": "s1", "lang": "python"}], db)
        first_index, first_graph = await pipeline._load_graph("o/r", db)
        again_index, again_graph = await pipeline._load_graph("o/r", db)
        assert first_index is again_index and first_graph is again_graph   # served from cache

        await store.put_index_many("o/r", [{"path": "b.py", "sha": "s2", "lang": "python"}], db)
        after_index, _ = await pipeline._load_graph("o/r", db)
        assert set(after_index) == {"a.py", "b.py"}                        # stale entry replaced

    asyncio.run(flow())


def test_repo_locks_are_released_when_idle(db):
    import asyncio

    async def flow():
        entry = pipeline._repo_lock("o/r")
        assert "o/r" in pipeline._repo_locks
        pipeline._release_repo_lock("o/r", entry)
        assert "o/r" not in pipeline._repo_locks          # no unbounded growth

    asyncio.run(flow())


def test_fetch_texts_runs_concurrently(monkeypatch):
    """Sequential fetches were ~20-30 round-trips before the first model call."""
    import asyncio
    import time

    in_flight = {"now": 0, "peak": 0}

    async def slow_content(owner, repo, path, ref):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0.05)
        in_flight["now"] -= 1
        return "content"

    monkeypatch.setattr(github, "get_file_content", slow_content)
    start = time.perf_counter()
    out = asyncio.run(pipeline._fetch_texts("o", "r", [f"f{i}.py" for i in range(8)], "sha"))
    elapsed = time.perf_counter() - start

    assert len(out) == 8
    assert in_flight["peak"] > 1                 # actually parallel
    assert elapsed < 0.3                         # 8 x 50ms sequential would be 0.4s


# ── iteration 16: cost/activation disclosure footer + re-review discipline ─────
def _res_with(**kw):
    from buglens.pipeline import ReviewResult
    base = dict(summary="s", score=90, verdict="looks-good", files=["a.py"], lenses_run=["security"])
    base.update(kw)
    return ReviewResult(**base)


def test_engine_footer_reports_calls_tokens_and_wall(monkeypatch):
    monkeypatch.delenv("BUGLENS_COST_PER_1M_IN", raising=False)
    monkeypatch.delenv("BUGLENS_COST_PER_1M_OUT", raising=False)
    res = _res_with(usage={"calls": 46, "errors": 8, "prompt_tokens": 38000,
                           "completion_tokens": 3200, "total_tokens": 41200},
                    wall_seconds=185.0)
    md = pipeline.render_summary_md(res, load_rules())
    assert "46 engine call(s), 8 failed" in md
    assert "~41.2k tokens (38.0k in / 3.2k out)" in md
    assert "3m05s" in md
    assert "$" not in md                      # no rate configured -> no invented price


def test_engine_footer_shows_cost_only_when_priced(monkeypatch):
    res = _res_with(usage={"calls": 2, "prompt_tokens": 1_000_000, "completion_tokens": 0})
    monkeypatch.setenv("BUGLENS_COST_PER_1M_IN", "3.0")
    monkeypatch.setenv("BUGLENS_COST_PER_1M_OUT", "15.0")
    assert "~$3.000" in pipeline.render_summary_md(res, load_rules())


def test_engine_footer_is_quiet_when_provider_reports_nothing():
    res = _res_with(usage={"calls": 2})       # calls but no token fields
    md = pipeline.render_summary_md(res, load_rules())
    assert "provider reported no token usage" in md
    assert "0 tokens" not in md


def test_engine_notes_disclose_depth_changes():
    rules = load_rules()
    rules.consensus = 3

    class V:                                   # stand-in for verify.Verified
        kept = [1, 2, 3, 4, 5, 6, 7]
        dropped = [1, 2]

    notes = pipeline._engine_notes(rules, V, [{"lens": "secrets", "deterministic": True}], True, 120, True, 8, 2, True)
    blob = " | ".join(notes)
    assert "consensus 3 passes" in blob
    assert "verify dropped 2 of 9" in blob
    assert "secret scan flagged" in blob
    lint_notes = pipeline._engine_notes(rules, V, [], False, 0, False, 1, 0, False,
                                        linters_run=["ruff", "eslint"])
    assert any("linters: ruff, eslint" in n for n in lint_notes)
    assert "linked-ticket check" in blob
    assert "code graph: 120" in blob
    assert "partial: 2/8" in blob
    assert "rounds without convergence" in blob


async def test_review_footer_end_to_end_discloses_activation(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    capture = {}
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}], capture=capture)
    rules = load_rules()
    rules.consensus = 3

    res = await pipeline.review_pr("o", "r", 1, db=db, rules=rules)

    assert "Engine:" in capture["body"]
    assert "consensus 3 passes" in capture["body"]
    assert res.wall_seconds > 0 and res.round_no == 1


async def test_round_cap_stops_re_review(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    caps = {}

    async def upsert(owner, repo, number, marker, body):
        if "round-cap" in marker:
            caps["body"] = body

    monkeypatch.setattr(github, "upsert_marked_comment", upsert)
    rules = load_rules()
    rules.max_rounds = 2

    await pipeline.review_pr("o", "r", 1, db=db, rules=rules)
    second = await pipeline.review_pr("o", "r", 1, db=db, rules=rules, force=True)
    third = await pipeline.review_pr("o", "r", 1, db=db, rules=rules, force=True)

    assert second.status != "capped"
    assert third.status == "capped"
    assert "round limit reached" in caps["body"]


async def test_convergence_skips_unchanged_files_without_calling_model(monkeypatch, db):
    calls = {"n": 0}

    async def counting(messages, **kw):
        calls["n"] += 1
        return await _fake_llm(messages, **kw)

    monkeypatch.setattr(llm, "complete_json", counting)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    async def upsert(owner, repo, number, marker, body):
        return None

    monkeypatch.setattr(github, "upsert_marked_comment", upsert)
    rules = load_rules()
    rules.converge_rounds = 1

    await pipeline.review_pr("o", "r", 1, db=db, rules=rules)
    after_first = calls["n"]
    assert after_first > 0

    second = await pipeline.review_pr("o", "r", 1, db=db, rules=rules, force=True)

    assert second.status == "converged"
    assert calls["n"] == after_first          # unchanged content -> zero model calls


async def test_convergence_reviews_new_code_only(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    monkeypatch.setattr(github, "upsert_marked_comment", lambda *a, **k: None)
    rules = load_rules()
    rules.converge_rounds = 1

    await pipeline.review_pr("o", "r", 1, db=db, rules=rules)

    # a second, genuinely different hunk on the same path must still be reviewed
    # (>20 added chars, else choose_paths treats it as a trivial change)
    new_patch = "@@ -1,3 +1,4 @@\n x = 1\n+bad = eval(other_input)\n print(x)\n"
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": new_patch}])
    res = await pipeline.review_pr("o", "r", 1, db=db, rules=rules, force=True)

    assert res.status != "converged"
    assert res.files == ["a.py"]
    assert any("no convergence" in n or "without convergence" in n for n in res.engine_notes)

async def test_lens_caps_limit_comments_per_lens(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    rules = load_rules()
    rules.lens_caps = {"security": 0}

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=rules)

    assert res.findings == []
    assert any(n.startswith("lens caps: security") for n in res.engine_notes)


async def test_code_objects_note_and_symbol_tagging(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert any(n.startswith("code objects:") for n in res.engine_notes)
    assert res.findings[0]["symbol"] == "<file>"

async def test_external_context_surfaces_and_is_disclosed(monkeypatch, db):
    from buglens import integrations

    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    async def fake_collect(providers, repo, query=""):
        return {"tickets": ["eval is unsafe"], "signals": [
            {"source": "sentry", "title": "boom", "count": 3, "url": "u"}],
            "used": ["jira", "sentry"]}

    monkeypatch.setattr(integrations, "collect", fake_collect)
    rules = load_rules()
    rules.external_context = ["jira", "sentry"]

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=rules)

    assert any(n == "external context: jira, sentry" for n in res.engine_notes)
    assert [f["title"] for f in res.findings] == ["eval on user input"]


async def test_external_context_failure_is_swallowed(monkeypatch, db):
    from buglens import integrations

    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    async def boom(providers, repo, query=""):
        raise RuntimeError("no network")

    monkeypatch.setattr(integrations, "collect", boom)
    rules = load_rules()
    rules.external_context = ["sentry"]

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=rules)

    assert res.status == "dry-run"
    assert not any("external context" in n for n in res.engine_notes)


async def test_fetch_failure_posts_could_not_run_notice(monkeypatch, db):
    posted = {}

    async def boom(*a, **k):
        raise RuntimeError("403 rate limit exceeded")

    async def upsert(owner, repo, number, marker, body, *a, **k):
        posted["marker"] = marker
        posted["body"] = body
        return {"id": 1}

    _patch_github(monkeypatch, [])
    monkeypatch.setattr(github, "get_pr_files", boom)
    monkeypatch.setattr(github, "upsert_marked_comment", upsert)

    res = await pipeline.review_pr("o", "r", 1, db=db)

    assert res.status == "error"
    assert res.posted is True
    assert posted["marker"] == pipeline.MARKER_ERROR
    assert "could not review" in posted["body"]
    assert "not a clean review" in posted["body"]
    assert "403 rate limit exceeded" in posted["body"]


async def test_fetch_failure_notice_is_best_effort(monkeypatch, db):
    async def boom(*a, **k):
        raise RuntimeError("403 rate limit exceeded")

    async def upsert(*a, **k):
        raise RuntimeError("rate limited anyway")

    _patch_github(monkeypatch, [])
    monkeypatch.setattr(github, "get_pr_files", boom)
    monkeypatch.setattr(github, "upsert_marked_comment", upsert)

    res = await pipeline.review_pr("o", "r", 1, db=db)

    assert res.status == "error"
    assert res.posted is False


async def test_fetch_failure_posts_nothing_in_dry_run(monkeypatch, db):
    called = {"n": 0}

    async def boom(*a, **k):
        raise RuntimeError("boom")

    async def upsert(*a, **k):
        called["n"] += 1

    _patch_github(monkeypatch, [])
    monkeypatch.setattr(github, "get_pr_files", boom)
    monkeypatch.setattr(github, "upsert_marked_comment", upsert)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert res.status == "error"
    assert called["n"] == 0


async def test_error_note_flattens_and_strips_backticks():
    md = pipeline.render_error_md("bad\n```\nthing`   here")
    assert pipeline.MARKER_ERROR in md
    assert "```" not in md
    assert "`here" not in md.replace("`bad", "")


async def test_adapters_run_off_the_event_loop(monkeypatch, db):
    from buglens import adapters

    monkeypatch.setattr(llm, "complete_json", _fake_llm)
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])
    monkeypatch.setattr(adapters, "scan", lambda *a, **k: ([], []))
    real = pipeline.asyncio.to_thread
    seen = []

    async def spy(fn, *a, **k):
        seen.append(fn)
        return await real(fn, *a, **k)

    monkeypatch.setattr(pipeline.asyncio, "to_thread", spy)

    await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert adapters.scan in seen


def _premortem_llm(items):
    async def fake(messages, **kwargs):
        if "pre-mortem on a pull request" in messages[0]["content"]:
            return LLMResult(data={"items": items})
        return await _fake_llm(messages, **kwargs)
    return fake


AUTH_PATCH = "@@ -0,0 +1,3 @@\n+def login(user, pw):\n+    return check(pw)\n+\n"


async def test_risky_path_selects_deep_tier_and_renders_premortem(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _premortem_llm([
        {"title": "Auth bypass via empty password", "why": "check(pw) may accept ''",
         "path": "src/auth/login.py", "line": 2},
    ]))
    _patch_github(monkeypatch, [{"filename": "src/auth/login.py", "status": "added", "patch": AUTH_PATCH}])

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=load_rules())

    assert any(n.startswith("review depth: deep") and "auth/security path" in n for n in res.engine_notes)
    assert any(n.startswith("pre-mortem: 1") for n in res.engine_notes)
    assert res.premortem == [{"title": "Auth bypass via empty password", "why": "check(pw) may accept ''",
                              "path": "src/auth/login.py", "line": 2}]
    md = pipeline.render_summary_md(res, load_rules())
    assert "Pre-mortem: what could break in production" in md
    assert "`src/auth/login.py:2`" in md


async def test_plain_path_stays_quick_with_no_premortem(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _premortem_llm([
        {"title": "never used", "why": "w", "path": "a.py", "line": 2},
    ]))
    _patch_github(monkeypatch, [{"filename": "a.py", "status": "modified", "patch": PATCH}])

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert any(n == "review depth: quick" for n in res.engine_notes)
    assert res.premortem == []
    assert not any(n.startswith("pre-mortem:") for n in res.engine_notes)


async def test_premortem_drops_a_scenario_not_anchored_on_an_added_line(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _premortem_llm([
        {"title": "hallucinated", "why": "w", "path": "src/auth/login.py", "line": 99},
    ]))
    _patch_github(monkeypatch, [{"filename": "src/auth/login.py", "status": "added", "patch": AUTH_PATCH}])

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=load_rules())

    assert res.premortem == []
    assert not any(n.startswith("pre-mortem:") for n in res.engine_notes)


async def test_depth_triggers_off_reports_no_tier(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _premortem_llm([]))
    _patch_github(monkeypatch, [{"filename": "src/auth/login.py", "status": "added", "patch": AUTH_PATCH}])
    rules = load_rules()
    rules.depth_triggers = False

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=rules)

    assert not any(n.startswith("review depth:") for n in res.engine_notes)
    assert res.premortem == []


async def test_review_header_shows_an_advisory_risk_tier(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _premortem_llm([]))
    _patch_github(monkeypatch, [{"filename": "src/auth/login.py", "status": "added", "patch": AUTH_PATCH}])

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=load_rules())

    assert res.risk_tier == "L3"
    md = pipeline.render_summary_md(res, load_rules())
    assert "**Review depth recommended: L3**" in md
    assert "read closely" in md


async def test_surprise_raises_the_tier_and_is_named_in_the_header(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _premortem_llm([]))
    _patch_github(monkeypatch, [{"filename": "app/service.py", "status": "modified", "patch": PATCH}])

    async def get_pr(*a, **k):
        return PRInfo("o", "r", 1, "basesha", "headsha", False, "docs: update the readme", "", "a", "open")

    monkeypatch.setattr(github, "get_pr", get_pr)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=load_rules())

    assert res.risk_tier == "L2"
    assert any(r.startswith("surprise: docs PR changes code") for r in res.risk_reasons)
    assert "surprise: docs PR changes code" in pipeline.render_summary_md(res, load_rules())


async def test_risk_tiers_off_removes_the_advisory(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _premortem_llm([]))
    _patch_github(monkeypatch, [{"filename": "src/auth/login.py", "status": "added", "patch": AUTH_PATCH}])
    rules = load_rules()
    rules.risk_tiers = False

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db, rules=rules)

    assert res.risk_tier == ""
    assert "Review depth recommended" not in pipeline.render_summary_md(res, rules)




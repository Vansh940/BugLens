"""Review-grade output: verdict header, priority labels, pre-merge table, dedup wiring.

These are the presentation and precision changes that make a BugLens review readable the way
the hosted reviewers are, without adding a required service or letting a bad month of model
outputs double-count a defect.
"""
import re
import sqlite3

from buglens import github, llm, pipeline, severity as sev, store
from buglens.github import PRInfo
from buglens.llm import LLMResult
from buglens.rules import load_rules


# ── priority labels ───────────────────────────────────────────────────────────
def test_priority_labels_cover_every_shipped_severity():
    assert sev.priority("blocker") == "P1"
    assert sev.priority("critical") == "P1"
    assert sev.priority("major") == "P2"
    assert sev.priority("minor") == "P3"
    assert sev.priority("nit") is None
    assert sev.priority("high") == "P2"
    assert sev.priority("") == "P3"


# ── verdict header (K2) ───────────────────────────────────────────────────────
def test_summary_header_shows_verdict_reason_confidence_risk_and_commit_scope():
    res = pipeline.ReviewResult(
        score=12, verdict="blocking", summary="s",
        verdict_reason="three confirmed blockers", confidence=88,
        merge_risk="high", security_risk="high", head_sha="abcdef1234567890",
    )
    md = pipeline.render_summary_md(res, load_rules())
    assert "three confirmed blockers" in md
    assert "**Confidence:** `88/100`" in md
    assert "**Risk:** merge `high` · security `high`" in md
    assert "checked up to `abcdef1`" in md


def test_summary_header_omits_empty_meta():
    res = pipeline.ReviewResult(score=100, verdict="looks-good", summary="clean")
    md = pipeline.render_summary_md(res, load_rules())
    assert "Confidence" not in md
    assert "Risk:" not in md


# ── finding header (K3) ───────────────────────────────────────────────────────
def test_finding_markdown_shows_priority_lenses_evidence_first_and_locations():
    md = pipeline.render_finding_md({
        "severity": "blocker", "category": "security", "title": "SQL injection",
        "body": "b", "evidence": "query = build(name)", "fix": "parameterize",
        "lens": "security", "lenses": ["correctness", "security"], "confidence": 0.95,
        "path": "app/users.py", "line": 14,
        "additional": [{"path": "app/admin.py", "line": 9}],
        "fingerprint": "b" * 32,
    })
    assert "**P1 · Blocker · security**" in md
    assert md.index("query = build(name)") < md.index("b")           # evidence above the body
    assert "lens: correctness, security" in md
    assert "also reported at `app/admin.py:9`" in md
    assert pipeline.extract_fingerprint(md) == "b" * 32


def test_finding_markdown_without_priority_for_a_nit():
    md = pipeline.render_finding_md({
        "severity": "nit", "category": "style", "title": "naming", "body": "b", "lens": "style",
    })
    assert "**Nit · style**" in md
    assert "lens: style" in md


# ── pre-merge checks table (K4) ───────────────────────────────────────────────
def test_pre_merge_checks_are_rows_with_status_and_fix():
    fd = pipeline._snippet_to_filediff("x = 1\n", "python", "app/users.py")
    rows = pipeline._pre_merge_checks([fd], "", [], True, 1, load_rules())
    by_check = {r["check"]: r for r in rows}
    assert by_check["Tests"]["status"] == "warn"
    assert by_check["Tests"]["fix"]
    assert by_check["Secrets"]["status"] == "warn"
    assert by_check["Linked issue"]["status"] == "info"
    assert all(set(r) == {"check", "status", "detail", "fix"} for r in rows)


def test_walkthrough_renders_pre_merge_as_a_table():
    fd = pipeline._snippet_to_filediff("x = 1\n", "python", "app/users.py")
    res = pipeline.ReviewResult(
        change_stack=[{"title": "Core logic", "note": "1 file"}],
        pre_merge=pipeline._pre_merge_checks([fd], "", [], False, 1, load_rules()),
    )
    md = pipeline.render_summary_md(res, load_rules())
    assert "| Check | Status | Detail |" in md
    assert "| Tests | ⚠️ Warn | code changed, no test files touched." in md
    assert "| Secrets | ✅ Pass | none found |" in md


# ── merge re-anchoring + semantic dedup (K1) ──────────────────────────────────
def test_reanchor_merge_findings_maps_shadow_lines_to_real_anchors():
    fd = pipeline._snippet_to_filediff("a = 1\nb = 2\n", "python", "app/x.py")
    shadow, line_map = pipeline._pr_shadow([fd], {fd.path: "added"})
    real_line = next(iter(line_map))
    findings = [
        {"path": "<pull-request>", "line": real_line, "title": "dup", "evidence": "b = 2"},
        {"path": "<pull-request>", "line": 1, "title": "pr-level concern", "evidence": ""},
    ]
    out = pipeline._reanchor_merge_findings(findings, line_map)
    assert out[0]["path"] == "app/x.py" and out[0]["line"] == line_map[real_line][1]
    assert out[1]["path"] == "<pull-request>"


async def _dup_llm(messages, **kwargs):
    system = messages[0]["content"]
    if "verifying findings" in system:
        return LLMResult(data={"verdicts": [{"verdict": "keep", "decidable": True, "reason": "ok"}]})
    if "PR-level summary" in system:
        return LLMResult(data={"summary": "s", "score": 50, "verdict": "needs-attention"})
    lens = re.search(r"LENS:\s*([^\s|(]+)", messages[1]["content"])
    name = lens.group(1) if lens else ""
    if name in ("security", "correctness"):
        return LLMResult(data={"findings": [{
            "severity": "major", "category": "bug", "line": 2,
            "title": f"eval via {name}", "body": "rce", "evidence": "bad = eval(user_input)",
            "confidence": 0.9,
        }], "positives": []})
    return LLMResult(data={"findings": [], "positives": []})


def _patch_github_min(monkeypatch):
    monkeypatch.setattr(github, "configured", lambda: True)

    async def get_pr(*a, **k):
        return PRInfo("o", "r", 1, "basesha", "headsha", False, "t", "b", "a", "open")

    async def files(*a, **k):
        return [{"filename": "a.py", "status": "modified",
                 "patch": "@@ -1,3 +1,4 @@\n x = 1\n+bad = eval(user_input)\n print(x)\n"}]

    async def tree(*a, **k):
        return ["a.py"]

    async def content(*a, **k):
        return "x = 1\nbad = eval(user_input)\nprint(x)\n"

    async def no(*a, **k):
        return []

    for name in ("get_pr", "get_pr_files", "get_repo_tree", "get_file_content",
                 "closing_issues", "get_pr_commit_messages", "get_commits_for_path",
                 "pulls_for_commit", "list_review_comments"):
        monkeypatch.setattr(github, name, {"get_pr": get_pr, "get_pr_files": files,
                                           "get_repo_tree": tree, "get_file_content": content}.get(name, no))


async def test_review_merges_the_same_defect_from_two_lenses(monkeypatch, db):
    monkeypatch.setattr(llm, "complete_json", _dup_llm)
    _patch_github_min(monkeypatch)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert len(res.findings) == 1
    assert res.findings[0]["lenses"] == ["correctness", "security"]
    assert any("merged 1 duplicate finding(s)" in n for n in res.engine_notes)
    assert any("verdict meta: not provided by the model" in n for n in res.engine_notes)


async def _meta_llm(messages, **kwargs):
    system = messages[0]["content"]
    if "verifying findings" in system:
        return LLMResult(data={"verdicts": [{"verdict": "keep", "decidable": True, "reason": "ok"}]})
    if "PR-level summary" in system:
        return LLMResult(data={"summary": "s", "score": 90, "verdict": "looks-good",
                               "verdict_reason": "one minor naming issue, nothing blocking",
                               "confidence": 77, "merge_risk": "low", "security_risk": "none"})
    return LLMResult(data={"findings": [], "positives": []})


async def test_header_meta_is_never_silently_absent(monkeypatch, db):
    """When the model does supply the header, no honesty note is added."""
    monkeypatch.setattr(llm, "complete_json", _meta_llm)
    _patch_github_min(monkeypatch)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True, db=db)

    assert res.confidence == 77 and res.merge_risk == "low" and res.security_risk == "none"
    assert res.verdict_reason.startswith("one minor naming")
    assert not any("verdict meta" in n for n in res.engine_notes)


async def test_ide_review_is_not_hidden_behind_prior_recall(monkeypatch, db):
    """The IDE path reviews the file as it is.

    Feeding prior-review recall into a snippet review demoted known bugs to a prose aside, so
    the panel scored a file full of them as clean.
    """
    called: list = []

    async def recall_block(*a, **k):
        called.append(a)
        return "PRIOR-REVIEW RECALL: this was already reported"

    monkeypatch.setattr(pipeline.recall, "recall_block", recall_block)
    monkeypatch.setattr(llm, "complete_json", _dup_llm)

    res = await pipeline.review_snippet("x = 1\nbad = eval(user_input)\nprint(x)\n", "python",
                                       "a.py", repo="o/r", db=db)

    assert called == []
    assert [f["title"] for f in res.findings] == ["eval via correctness"]
    assert res.lenses_run


async def test_review_without_a_db_argument_still_uses_the_store(monkeypatch, db):
    """A webhook call passes db=None; every persisted-context feature must still run.

    Leaving db None silently skipped round counting, recall, suppression and the cross-push
    ledger in production while the tests, which always pass a path, stayed green.
    """
    monkeypatch.setenv("BUGLENS_DB", db)
    monkeypatch.setattr(llm, "complete_json", _meta_llm)
    _patch_github_min(monkeypatch)

    res = await pipeline.review_pr("o", "r", 1, dry_run=True)

    assert res.status == "dry-run"
    assert await store.count_rounds("o/r", 1, db) == 1
    rows = sqlite3.connect(db).execute("select count(*) from pr_round_files").fetchone()[0]
    assert rows >= 1


# ── stale-comment reconciliation (K5/K6) ──────────────────────────────────────
async def _reconcile_env(monkeypatch, db, *, file_text, reply, minimize):
    monkeypatch.setattr(github, "configured", lambda: True)

    async def content(*a, **k):
        return file_text

    async def _reply(*a, **k):
        reply.append(a)
        return {"id": 1}

    async def _minimize(*a, **k):
        return minimize

    monkeypatch.setattr(github, "get_file_content", content)
    monkeypatch.setattr(github, "reply_review_comment", _reply)
    monkeypatch.setattr(github, "minimize_comment", _minimize)

    async def findings(*a, **k):
        return [{"fingerprint": "fp1", "path": "a.py", "evidence": "bad = eval(x)"}]

    monkeypatch.setattr(store, "findings_for_pr", findings)
    await store.init(db)
    await store.save_posted_comments("o/r", 1, "oldsha", [
        {"fingerprint": "fp1", "path": "a.py", "line": 2, "comment_id": "555", "node_id": "N1"},
    ], db)


async def test_reconcile_acknowledges_a_finding_whose_evidence_is_gone(monkeypatch, db):
    reply: list = []
    await _reconcile_env(monkeypatch, db, file_text="x = 1\n", reply=reply, minimize=True)
    res = pipeline.ReviewResult(findings=[], head_sha="newsha")

    ack, retired = await pipeline._reconcile_prior_comments("o", "r", 1, "newsha", res, load_rules(), db)

    assert (ack, retired) == (1, 0)
    assert reply and reply[0][3] == 555
    assert await store.posted_comments("o/r", 1, db) == []


async def test_reconcile_leaves_a_still_open_finding_alone(monkeypatch, db):
    reply: list = []
    await _reconcile_env(monkeypatch, db, file_text="q\nbad = eval(x)\n", reply=reply, minimize=True)
    res = pipeline.ReviewResult(findings=[], head_sha="newsha")

    ack, retired = await pipeline._reconcile_prior_comments("o", "r", 1, "newsha", res, load_rules(), db)

    assert (ack, retired) == (0, 0)
    assert not reply
    assert len(await store.posted_comments("o/r", 1, db)) == 1


async def test_reconcile_retires_when_acknowledgement_is_off(monkeypatch, db):
    reply: list = []
    await _reconcile_env(monkeypatch, db, file_text="x = 1\n", reply=reply, minimize=True)
    rules = load_rules()
    rules.acknowledge_resolved = False
    res = pipeline.ReviewResult(findings=[], head_sha="newsha")

    ack, retired = await pipeline._reconcile_prior_comments("o", "r", 1, "newsha", res, rules, db)

    assert (ack, retired) == (0, 1)
    assert not reply


async def test_reconcile_is_a_noop_without_a_sha_or_a_configured_host(monkeypatch, db):
    res = pipeline.ReviewResult(findings=[])
    monkeypatch.setattr(github, "configured", lambda: True)
    assert await pipeline._reconcile_prior_comments("o", "r", 1, "", res, load_rules(), db) == (0, 0)
    monkeypatch.setattr(github, "configured", lambda: False)
    assert await pipeline._reconcile_prior_comments("o", "r", 1, "sha", res, load_rules(), db) == (0, 0)


async def test_posted_comment_ledger_records_without_an_explicit_db(monkeypatch):
    """The webhook path calls with db=None and the store falls back to BUGLENS_DB.

    A `not db` guard here silently disabled the whole ledger in production, so this pins it.
    """
    fp = "a" * 32
    saved: dict = {}

    async def list_comments(*a, **k):
        return [{"id": 77, "node_id": "N77", "path": "a.py", "line": 2,
                 "body": "fix this\n\n" + pipeline.fingerprint_marker(fp)}]

    async def save(slug, pr, head, items, db=None):
        saved.update(slug=slug, pr=pr, head=head, items=items, db=db)

    monkeypatch.setattr(github, "list_review_comments", list_comments)
    monkeypatch.setattr(store, "save_posted_comments", save)
    res = pipeline.ReviewResult(findings=[{"fingerprint": fp, "path": "a.py", "line": 2}],
                                head_sha="sha1")

    await pipeline._record_posted_comments("o", "r", 1, res, {"id": 9}, None)

    assert saved["items"] == [{"fingerprint": fp, "path": "a.py", "line": 2,
                               "comment_id": 77, "node_id": "N77"}]
    assert saved["slug"] == "o/r" and saved["db"] is None

"""Pure finding-to-check-run mapping and input modes (Wave F)."""
from __future__ import annotations

from types import SimpleNamespace

from buglens import checks
from buglens.diff import parse_patch


def test_annotation_level_mapping():
    assert checks.annotation_level("blocker") == "failure"
    assert checks.annotation_level("major") == "warning"
    assert checks.annotation_level("minor") == "notice"
    assert checks.annotation_level("nit") == "notice"
    assert checks.annotation_level("info") == "notice"
    assert checks.annotation_level("unknown-severity") == "notice"
    assert checks.annotation_level(None) == "notice"


def test_finding_to_annotation_without_anchor_is_none():
    assert checks.finding_to_annotation({"line": None, "path": "a.py"}) is None
    assert checks.finding_to_annotation({"line": 3}) is None
    assert checks.finding_to_annotation({"path": "a.py"}) is None


def test_finding_to_annotation_full_mapping():
    long_title = "X" * 300
    f = {"path": "a.py", "line": 42, "severity": "major", "title": long_title,
         "body": "the body", "evidence": "quote"}
    ann = checks.finding_to_annotation(f)
    assert ann["path"] == "a.py"
    assert ann["start_line"] == 42 and ann["end_line"] == 42
    assert ann["annotation_level"] == "warning"
    assert ann["title"] == long_title[:255] and len(ann["title"]) == 255
    assert "the body" in ann["message"]
    assert "Evidence: quote" in ann["message"]


def test_nit_maps_to_notice():
    ann = checks.finding_to_annotation({"path": "a.py", "line": 1, "severity": "nit", "title": "t"})
    assert ann["annotation_level"] == "notice"


def test_ordering_blocker_before_minor_and_confidence_desc():
    findings = [
        {"path": "a.py", "line": 1, "severity": "minor", "confidence": 0.9, "title": "m1"},
        {"path": "a.py", "line": 2, "severity": "blocker", "confidence": 0.1, "title": "b1"},
        {"path": "a.py", "line": 3, "severity": "blocker", "confidence": 0.8, "title": "b2"},
    ]
    anns, dropped = checks.annotations_from_findings(findings, 10)
    assert [a["title"] for a in anns] == ["b2", "b1", "m1"]
    assert dropped == 0


def test_dropped_counts_pr_level_and_overflow():
    findings = [
        {"path": "a.py", "line": 1, "severity": "major", "title": "k1"},
        {"path": "a.py", "line": None, "severity": "major", "title": "pr-level"},
        {"path": "a.py", "line": 3, "severity": "major", "title": "k2"},
    ]
    anns, dropped = checks.annotations_from_findings(findings, 1)
    assert len(anns) == 1
    assert dropped == 2
    assert dropped == len(findings) - len(anns)


def test_zero_cap_yields_empty_and_all_dropped():
    findings = [{"path": "a.py", "line": 1, "severity": "major", "title": "x"}]
    assert checks.annotations_from_findings(findings, 0) == ([], 1)


def test_annotation_chunks_at_50():
    anns = [{"path": "a.py", "line": i} for i in range(120)]
    chunks = checks.annotation_chunks(anns)
    assert [len(c) for c in chunks] == [50, 50, 20]
    assert [a for c in chunks for a in c] == anns


def test_annotation_chunks_empty_and_zero_size():
    assert checks.annotation_chunks([]) == []
    assert checks.annotation_chunks([{"a": 1}], 0) == []


def test_conclusion_for_rules():
    assert checks.conclusion_for("blocking", "neutral", True) == "failure"
    assert checks.conclusion_for("blocking", "neutral", False) == "neutral"
    assert checks.conclusion_for("blocking", "failure", False) == "neutral"
    assert checks.conclusion_for("needs-attention", "failure", True) == "failure"
    assert checks.conclusion_for("needs-attention", "action_required", True) == "action_required"
    assert checks.conclusion_for("looks-good", "success", False) == "success"
    assert checks.conclusion_for("blocking", "bogus", True) == "failure"
    assert checks.conclusion_for("needs-attention", "bogus", False) == "neutral"


def test_input_text_pr_metadata_and_unknown_mode():
    res = SimpleNamespace(repo="o/r", pr=7, status="ok", score=90, verdict="needs-attention")
    text = checks.input_text("pr_metadata", res, [])
    assert "o/r#7" in text
    assert "Files changed: 0" in text and "Findings: 0" in text
    assert checks.input_text("bogus-mode", res, []) == text


def test_input_text_incremental_lists_added_lines():
    fd = parse_patch("@@ -0,0 +1,2 @@\n+a\n+b\n", path="a.py")
    text = checks.input_text("incremental", None, [fd])
    assert "a.py:1 a" in text
    assert "a.py:2 b" in text


def test_input_text_full_diff_contains_file_text():
    fd = parse_patch("@@ -0,0 +1,2 @@\n+a\n+b\n", path="a.py")
    text = checks.input_text("full_diff", None, [fd])
    assert "### a.py" in text
    assert "a" in text and "b" in text


def test_input_text_is_deterministic():
    res = SimpleNamespace(repo="o/r", pr=7, status="ok", score=90, verdict="needs-attention")
    fd = parse_patch("@@ -0,0 +1,2 @@\n+a\n+b\n", path="a.py")
    for mode in ("pr_metadata", "incremental", "full_diff"):
        assert checks.input_text(mode, res, [fd]) == checks.input_text(mode, res, [fd])

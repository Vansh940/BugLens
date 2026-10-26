"""Cross-lens defect dedupe: one real defect, one comment, regardless of lens."""
from __future__ import annotations

from buglens import dedupe


def _finding(**over) -> dict:
    finding = {
        "path": "svc/handler.py",
        "line": 12,
        "severity": "major",
        "category": "bug",
        "title": "token defaults to None",
        "body": "The token is not validated before use.",
        "fix": "Reject a missing token before continuing.",
        "evidence": "token = request.args.get('token')",
        "confidence": 0.8,
        "lens": "correctness",
    }
    finding.update(over)
    return finding


def test_same_evidence_two_lenses_merge():
    """The same evidence quote from two lenses collapses to one kept finding."""
    security = _finding(lens="security", category="security", title="unvalidated token",
                        severity="major", confidence=0.8)
    correctness = _finding(lens="correctness", category="bug", title="token read then trusted",
                           severity="major", confidence=0.8)
    kept, dropped = dedupe.dedupe([correctness, security])
    assert len(kept) == 1 and len(dropped) == 1
    assert kept[0]["lenses"] == ["correctness", "security"]
    assert kept[0]["lens"] == "correctness"
    assert dropped[0]["lens"] == "security"
    assert "additional" not in kept[0]


def test_pr_level_finding_survives_apart_from_file_finding():
    """A PR-level finding has no anchor, so it never merges with a file finding."""
    pr = _finding(path="<pull-request>", line=None, evidence="",
                  title="No tests cover the new branch", lens="merge", category="test")
    file_finding = _finding(evidence="")
    kept, dropped = dedupe.dedupe([pr, file_finding])
    assert len(kept) == 2 and dropped == []
    assert {f["path"] for f in kept} == {"<pull-request>", "svc/handler.py"}


def test_same_line_different_evidence_stays_separate():
    """Two defects on one line with distinct evidence are two findings, not one."""
    one = _finding(line=12, evidence="token = request.args.get('token')")
    two = _finding(line=12, evidence="return handler(token=token)")
    kept, dropped = dedupe.dedupe([one, two])
    assert len(kept) == 2 and dropped == []


def test_same_evidence_two_lines_merges_and_records_additional():
    """One defect quoted at two lines merges; the loser location is recorded."""
    later = _finding(line=40, severity="minor", evidence="token = request.args.get('token')")
    earlier = _finding(line=10, severity="blocker", evidence="token = request.args.get('token')")
    kept, dropped = dedupe.dedupe([later, earlier])
    assert len(kept) == 1 and len(dropped) == 1
    assert kept[0]["line"] == 10
    assert kept[0]["additional"] == [{"path": "svc/handler.py", "line": 40}]
    assert "additional" not in dropped[0]


def test_severity_beats_confidence_for_winner():
    """A lower severity rank wins even when the other finding is far more confident."""
    blocker = _finding(severity="blocker", confidence=0.5)
    minor = _finding(severity="minor", confidence=0.99)
    kept, dropped = dedupe.dedupe([minor, blocker])
    assert len(kept) == 1
    assert kept[0]["severity"] == "blocker"


def test_normalize_evidence_and_defect_key_stability():
    """Evidence normalization collapses whitespace and lowercases; keys are stable."""
    assert dedupe.normalize_evidence("  Hello\n\tWORLD  ") == "hello world"
    assert dedupe.normalize_evidence(None) == ""
    assert dedupe.normalize_evidence("   ") == ""
    finding = _finding(evidence="  Token\n =  None ")
    assert dedupe.defect_key(finding) == dedupe.defect_key(finding)
    assert dedupe.defect_key(finding) == dedupe.defect_key(_finding(evidence="token = none"))


def test_input_is_not_mutated():
    """The caller's winner dict never gains the annotation keys."""
    one = _finding(lens="correctness")
    two = _finding(lens="security")
    kept, _ = dedupe.dedupe([one, two])
    assert kept[0] is not one
    assert "lenses" not in one and "lenses" not in two
    assert "additional" not in one and "additional" not in two


def test_none_confidence_still_participates():
    """A finding with confidence None is treated as 0 and can still be dropped."""
    weak = _finding(confidence=None, lens="security")
    strong = _finding(confidence=0.4, lens="correctness")
    kept, dropped = dedupe.dedupe([weak, strong])
    assert len(kept) == 1 and len(dropped) == 1
    assert kept[0]["lens"] == "correctness"

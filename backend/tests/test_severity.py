from buglens import severity as sev


def test_alias_normalisation():
    assert sev.normalize("critical") == "blocker"
    assert sev.normalize("high") == "major"
    assert sev.normalize("medium") == "minor"
    assert sev.normalize("low") == "nit"
    assert sev.normalize("nonsense") == "minor"


def test_rank_is_ordered():
    assert sev.rank("blocker") < sev.rank("major") < sev.rank("minor") < sev.rank("nit")


def test_legacy_mapping_matches_extension_contract():
    assert sev.to_legacy("blocker") == "critical"
    assert sev.to_legacy("major") == "warning"
    assert sev.to_legacy("nit") == "suggestion"


def test_budget_sorts_by_severity_then_confidence():
    findings = [
        {"severity": "nit", "confidence": 0.9, "path": "a", "line": 1},
        {"severity": "blocker", "confidence": 0.5, "path": "a", "line": 2},
        {"severity": "major", "confidence": 0.6, "path": "a", "line": 3},
    ]
    kept, dropped = sev.budget(findings, max_comments=2)
    assert kept[0]["severity"] == "blocker"
    assert kept[1]["severity"] == "major"
    assert dropped[0]["severity"] == "nit"


def test_budget_respects_min_severity_floor():
    findings = [{"severity": "nit", "confidence": 0.9}, {"severity": "blocker", "confidence": 0.9}]
    kept, dropped = sev.budget(findings, max_comments=5, min_severity="major")
    assert [f["severity"] for f in kept] == ["blocker"]
    assert [f["severity"] for f in dropped] == ["nit"]


def test_score_decreases_and_clamps():
    assert sev.score_from_findings([]) == 100
    assert sev.score_from_findings([{"severity": "blocker"}, {"severity": "blocker"},
                                    {"severity": "blocker"}, {"severity": "blocker"},
                                    {"severity": "blocker"}]) == 0

def test_apply_lens_caps_limits_per_lens():
    findings = [
        {"severity": "major", "lens": "security", "path": "a", "line": 1},
        {"severity": "minor", "lens": "security", "path": "a", "line": 2},
        {"severity": "minor", "lens": "ui", "path": "a", "line": 3},
        {"severity": "nit", "path": "a", "line": 4},
    ]
    kept, dropped = sev.apply_lens_caps(findings, {"security": 1})
    assert [f["line"] for f in kept] == [1, 3, 4]
    assert [f["line"] for f in dropped] == [2]


def test_apply_lens_caps_no_caps_is_a_noop():
    findings = [{"lens": "security"}, {"lens": "ui"}]
    kept, dropped = sev.apply_lens_caps(findings, {})
    assert kept == findings and dropped == []

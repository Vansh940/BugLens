from buglens import verify
from buglens.diff import parse_patch
from buglens.rules import load_rules

PATCH = """@@ -1,3 +1,4 @@
 x = 1
+bad = eval(user_input)
 print(x)
"""


def _fd():
    return parse_patch(PATCH, "a.py")


def _finding(**over):
    raw = {
        "severity": "major", "category": "security", "line": 2,
        "title": "eval on user input", "body": "remote code execution",
        "fix": "use ast.literal_eval", "evidence": "bad = eval(user_input)",
        "confidence": 0.9,
    }
    raw.update(over)
    return verify.normalize_finding(raw, "security", "security", "a.py")


def test_normalize_finding_has_canonical_shape():
    f = _finding()
    assert f["severity"] == "major"
    assert f["fingerprint"]
    assert f["path"] == "a.py"


def test_normalize_sanitises_multiline_suggestion():
    assert _finding(suggestion="safe = 1\nmore = 2")["suggestion"] is None
    assert _finding(suggestion="safe = ast.literal_eval(user_input)")["suggestion"] == "safe = ast.literal_eval(user_input)"


def test_normalize_rejects_missing_title():
    raw = {"body": "no title", "evidence": "bad = eval(user_input)"}
    assert verify.normalize_finding(raw, "security", "security", "a.py") is None


def test_machine_gate_drops_hallucinated_evidence():
    fd = _fd()
    rules = load_rules()
    bad = _finding(evidence="totally not in the diff at all")
    ok, reason = verify.machine_gate(bad, fd, rules)
    assert not ok and "evidence" in reason


def test_machine_gate_keeps_real_finding_and_snaps_line():
    fd = _fd()
    rules = load_rules()
    f = _finding(line=2)
    ok, _ = verify.machine_gate(f, fd, rules)
    assert ok and f["line"] == 2


def test_machine_gate_reanchors_to_evidence_line():
    fd = _fd()
    rules = load_rules()
    # model reported line 3 (context) but the evidence lives on added line 2
    f = _finding(line=3)
    ok, _ = verify.machine_gate(f, fd, rules)
    assert ok and f["line"] == 2 and f.get("line_drift") == -1


def test_machine_gate_reanchors_beyond_snap_tolerance():
    patch = "@@ -1,1 +1,60 @@\n x = 1\n" + "".join(f"+line {i}\n" for i in range(2, 40)) + "+bad = eval(user_input)\n"
    fd = parse_patch(patch, "a.py")
    rules = load_rules()
    f = _finding(line=2)          # 38 lines away, outside snap tolerance, but evidence matches
    ok, _ = verify.machine_gate(f, fd, rules)
    assert ok and f["line"] == 40


async def test_verify_all_without_model_gate():
    fd = _fd()
    rules = load_rules()
    rules.verify = False
    good = _finding()
    bad = _finding(title="made up", evidence="import nonexistentlib")
    result = await verify.verify_all([good, bad], {"a.py": fd}, rules)
    assert [f["title"] for f in result.kept] == ["eval on user input"]
    assert result.dropped and "machine" in result.dropped[0]["drop_reason"]


async def test_verify_all_drops_suppressed_fingerprint():
    fd = _fd()
    rules = load_rules()
    rules.verify = False
    good = _finding()
    rules._suppressed = {good["fingerprint"]}
    result = await verify.verify_all([good], {"a.py": fd}, rules)
    assert not result.kept
    assert "suppressed" in result.dropped[0]["drop_reason"]

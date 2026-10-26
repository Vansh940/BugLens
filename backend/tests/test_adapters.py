import json

from buglens import adapters
from buglens.diff import parse_patch
from buglens.rules import load_rules

PATCH = """@@ -1,3 +1,5 @@
 x = 1
+bad = eval(user_input)
 print(x)
+import os
 y = 2
"""


def _fd():
    return parse_patch(PATCH, "a.py")


def test_ruff_parser_maps_severity_by_code():
    rules = adapters.PARSERS["ruff"](json.dumps([
        {"code": "F821", "message": "undefined name 'x'", "location": {"row": 2, "column": 1}},
        {"code": "E501", "message": "line too long", "location": {"row": 3, "column": 80}},
    ]))
    assert rules[0].severity == "major" and rules[0].code == "F821"
    assert rules[1].severity == "minor" and rules[1].line == 3


def test_bandit_parser_maps_high_to_major():
    rules = adapters.PARSERS["bandit"](json.dumps({"results": [
        {"issue_severity": "HIGH", "test_id": "B307", "issue_text": "eval", "line_number": 2},
    ]}))
    assert rules[0].severity == "major" and rules[0].category == "security"


def test_hadolint_and_shellcheck_parse():
    shell = adapters.PARSERS["shellcheck"](json.dumps([
        {"level": "error", "code": 2086, "message": "quote this", "line": 4},
    ]))
    assert shell[0].severity == "major" and shell[0].code == "SC2086"
    docker = adapters.PARSERS["hadolint"](json.dumps([
        {"level": "warning", "code": "DL3008", "message": "pin versions", "line": 2},
    ]))
    assert docker[0].severity == "minor" and docker[0].code == "DL3008"


def test_parsable_parser_handles_yamllint():
    rules = adapters.PARSERS["parsable"]("a.yml:3:2: [warning] truthy value should be true\n")
    assert rules and rules[0].line == 3 and rules[0].severity == "minor"


def test_enabled_adapters_off_auto_and_explicit(monkeypatch):
    monkeypatch.setattr(adapters, "_installed", lambda a: True)
    assert adapters.enabled_adapters([]) == []
    assert adapters.enabled_adapters(["off"]) == []
    assert len(adapters.enabled_adapters(["auto"])) == len(adapters.ADAPTERS)
    assert [a.name for a in adapters.enabled_adapters(["ruff"])] == ["ruff"]


def test_enabled_adapters_skips_missing_tools(monkeypatch):
    monkeypatch.setattr(adapters, "_installed", lambda a: a.name == "ruff")
    assert [a.name for a in adapters.enabled_adapters(["auto"])] == ["ruff"]


def test_scan_keeps_only_added_lines(monkeypatch):
    monkeypatch.setattr(adapters, "_installed", lambda a: True)
    output = json.dumps([
        {"code": "F821", "message": "on added line", "location": {"row": 2, "column": 1}},
        {"code": "E501", "message": "on context line", "location": {"row": 3, "column": 1}},
    ])
    monkeypatch.setattr(adapters, "_run", lambda *a, **k: output)
    fd = _fd()
    findings, ran = adapters.scan({"a.py": "x = 1\nbad = eval(user_input)\nprint(x)\n"}, {"a.py": fd}, ["ruff"])
    assert ran == ["ruff"]
    assert [f["line"] for f in findings] == [2]
    assert findings[0]["deterministic"] is True and findings[0]["confidence"] == 1.0
    assert findings[0]["fingerprint"]


def test_scan_is_off_by_default(monkeypatch):
    monkeypatch.setattr(adapters, "_installed", lambda a: True)
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return ""

    monkeypatch.setattr(adapters, "_run", boom)
    findings, ran = adapters.scan({"a.py": "x = 1\n"}, {"a.py": _fd()}, [])
    assert findings == [] and ran == [] and called["n"] == 0

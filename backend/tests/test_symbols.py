"""Symbol/contract graph and ripple analysis (Wave D)."""
from __future__ import annotations

from buglens import symbols
from buglens.diff import fingerprint, parse_patch
from buglens.indexer import parse_text


def _fd(path: str, added: list[str]) -> object:
    body = "".join(f"+{t}\n" for t in added)
    return parse_patch(f"@@ -0,0 +1,{len(added)} @@\n{body}", path=path)


def test_changed_defs_reads_python_js_go_and_methods():
    fd = _fd("a.py", [
        "def handler(user, token):",
        "async def fetch(url):",
        "export function render(props) {",
        "func Serve(w http.ResponseWriter) {",
        "handle(req) {",
    ])
    names = [d["name"] for d in symbols.changed_defs([fd])]
    assert names == ["handler", "fetch", "render", "Serve", "handle"]


def test_changed_defs_skips_control_keywords_and_ignores_defaults():
    fd = _fd("a.js", ["if (x) {", "for (i in y) {", "function ok(a, b = 2, ...rest) {"])
    ds = symbols.changed_defs([fd])
    assert [d["name"] for d in ds] == ["ok"]
    assert ds[0]["params"] == ["a", "b = 2", "...rest"]
    assert ds[0]["required"] == ["a"]


def test_ripple_finds_callers_excluding_changed_paths():
    index = {
        "pkg/core.py": parse_text("pkg/core.py", "def run(a):\n    return a\n"),
        "pkg/use.py": parse_text("pkg/use.py", "from pkg.core import run\nrun(1)\n"),
        "pkg/caller2.py": parse_text("pkg/caller2.py", "run(2)\n"),
    }
    changed = [{"name": "run", "path": "pkg/core.py", "line": 1, "params": ["a"],
                "required": ["a"], "raw": "def run(a):"}]
    items = symbols.ripple(index, changed, ["pkg/core.py"])
    assert len(items) == 1
    assert items[0]["caller_count"] == 2
    assert items[0]["callers"] == ["pkg/caller2.py", "pkg/use.py"]


def test_ripple_ignores_symbols_without_callers():
    index = {"a.py": parse_text("a.py", "def lonely():\n    pass\n")}
    changed = [{"name": "lonely", "path": "a.py", "line": 1, "params": [],
                "required": [], "raw": "def lonely():"}]
    assert symbols.ripple(index, changed, ["a.py"]) == []


def test_ripple_findings_only_for_new_required_argument_with_callers():
    with_callers = [{"name": "run", "path": "core.py", "line": 1, "params": ["a", "b"],
                     "required": ["a", "b"], "caller_count": 2,
                     "callers": ["x.py", "y.py"], "raw": "def run(a, b):"}]
    fs = symbols.ripple_findings(with_callers)
    assert len(fs) == 1
    f = fs[0]
    assert f["lens"] == "merge" and f["severity"] == "major" and f["category"] == "bug"
    assert f["deterministic"] is False and 0 < f["confidence"] < 1
    assert f["fingerprint"] == fingerprint("core.py", "bug", f["title"])
    assert "b" in f["title"]

    optional_only = [{**with_callers[0], "required": []}]
    assert symbols.ripple_findings(optional_only) == []

    no_callers = [{**with_callers[0], "caller_count": 0, "callers": []}]
    assert symbols.ripple_findings(no_callers) == []


def test_ripple_block_is_bounded_and_lists_callers():
    items = [{"name": "run", "path": "core.py", "line": 3, "params": ["a"], "required": ["a"],
              "raw": "def run(a):", "callers": ["x.py", "y.py", "z.py", "w.py", "q.py"],
              "caller_count": 5}]
    block = symbols.ripple_block(items)
    assert block.startswith("Changed symbols with in-repo callers")
    assert "run(a)" in block and "core.py:3" in block and "(+1 more)" in block
    assert symbols.ripple_block([]) == ""

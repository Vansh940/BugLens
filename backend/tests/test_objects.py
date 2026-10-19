"""Code-object lensing: added lines grouped by enclosing symbol (Wave G)."""
from __future__ import annotations

from buglens import objects
from buglens.diff import parse_patch


def _fd(path: str, added: list[str]) -> object:
    body = "".join(f"+{t}\n" for t in added)
    return parse_patch(f"@@ -0,0 +1,{len(added)} @@\n{body}", path=path)


class _Line:
    def __init__(self, new_line: int | None, text: str) -> None:
        self.new_line = new_line
        self.text = text


class _FD:
    def __init__(self, path: str, added: list) -> None:
        self.path = path
        self.added = added


def test_two_functions_become_two_units_in_order():
    fd = _fd("a.py", [
        "def alpha(a):",
        "    return a",
        "",
        "def beta(b):",
        "    return b",
    ])
    units = objects.build_units([fd])
    assert [u["symbol"] for u in units] == ["alpha", "beta"]
    first, second = units
    assert first["id"] == "a.py:alpha"
    assert first["path"] == "a.py"
    assert first["start_line"] == 1 and first["end_line"] == 3
    assert first["lines"] == [1, 2, 3]
    assert first["chars"] == 28
    assert second["id"] == "a.py:beta"
    assert second["start_line"] == 4 and second["end_line"] == 5
    assert second["lines"] == [4, 5]
    assert second["chars"] == 26


def test_lines_before_any_def_become_file_unit():
    fd = _fd("b.py", [
        "import os",
        "CONST = 1",
        "",
        "def gamma():",
        "    return 2",
    ])
    units = objects.build_units([fd])
    assert [u["symbol"] for u in units] == ["<file>", "gamma"]
    top = units[0]
    assert top["id"] == "b.py:<file>"
    assert top["start_line"] == 1 and top["end_line"] == 3
    assert top["lines"] == [1, 2, 3]


def test_two_files_and_unit_summary_counts_files():
    a = _fd("a.py", ["def alpha():", "    return 1"])
    b = _fd("b.py", ["def beta():", "    return 2"])
    one = objects.build_units([a])
    assert objects.unit_summary(one) == "1 code object(s) across 1 file(s)"
    both = objects.build_units([a, b])
    assert {u["path"] for u in both} == {"a.py", "b.py"}
    assert len(both) == 2
    assert objects.unit_summary(both) == "2 code object(s) across 2 file(s)"
    assert objects.unit_summary([]) == ""


def test_symbol_for_inside_outside_and_falsy_line():
    fd = _fd("a.py", [
        "def alpha(a):",
        "    return a",
        "",
        "def beta(b):",
        "    return b",
    ])
    units = objects.build_units([fd])
    assert objects.symbol_for("a.py", 2, units) == "alpha"
    assert objects.symbol_for("a.py", 5, units) == "beta"
    assert objects.symbol_for("a.py", 99, units) == "<file>"
    assert objects.symbol_for("other.py", 2, units) == "<file>"
    assert objects.symbol_for("a.py", None, units) == "<file>"
    assert objects.symbol_for("a.py", 0, units) == "<file>"


def test_max_units_cap_is_respected():
    picked = [_FD(f"f{i}.py", [_Line(1, "x = 1")]) for i in range(objects.MAX_UNITS + 5)]
    units = objects.build_units(picked)
    assert len(units) == objects.MAX_UNITS


def test_build_units_is_deterministic():
    fd = _fd("a.py", ["def alpha():", "    return 1", "", "def beta():", "    return 2"])
    assert objects.build_units([fd]) == objects.build_units([fd])

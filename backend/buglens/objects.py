"""Code-object lensing: group a PR's added lines by the symbol they belong to.

``symbols.py`` returns one entry per added definition line. This module answers the
next question: for every added line of a picked file, which code object (the enclosing
definition, or the file itself) owns it. The output is a bounded, ordered list of units
the pipeline uses to tag findings with an enclosing symbol and to name review depth.

Pure and deterministic over the diff it is handed: no model, no network, no I/O, so the
same inputs always produce the same units.
"""
from __future__ import annotations

from . import symbols

MAX_UNITS = 200

FILE_SYMBOL = "<file>"


def _sorted_defs(fd) -> list[dict]:
    defs = symbols.changed_defs([fd])
    defs.sort(key=lambda d: (d.get("line") is None, d.get("line") or 0))
    return defs


def _symbol_at(defs: list[dict], new_line: int) -> str:
    name = FILE_SYMBOL
    for d in defs:
        line = d.get("line")
        if line is not None and line <= new_line:
            name = d["name"]
        else:
            break
    return name


def build_units(picked: list) -> list[dict]:
    """Group each file's added lines by enclosing symbol, ordered by (start_line, id)."""
    groups: dict[tuple[str, str], dict] = {}
    for fd in picked or []:
        path = getattr(fd, "path", "")
        defs = _sorted_defs(fd)
        for line in getattr(fd, "added", None) or []:
            new_line = getattr(line, "new_line", None)
            if not new_line:
                continue
            symbol = _symbol_at(defs, new_line)
            key = (path, symbol)
            group = groups.get(key)
            if group is None:
                group = {"id": f"{path}:{symbol}", "path": path, "symbol": symbol,
                         "lines": [], "chars": 0}
                groups[key] = group
            group["lines"].append(new_line)
            group["chars"] += len(getattr(line, "text", "") or "") + 1
    units: list[dict] = []
    for group in groups.values():
        lines = sorted(group["lines"])
        units.append({
            "id": group["id"],
            "path": group["path"],
            "symbol": group["symbol"],
            "start_line": lines[0],
            "end_line": lines[-1],
            "lines": lines,
            "chars": group["chars"],
        })
    units.sort(key=lambda u: (u["start_line"], u["id"]))
    return units[:MAX_UNITS]


def symbol_for(path: str, line: int | None, units: list[dict]) -> str:
    """The symbol of the first unit containing `path`/`line`, else "<file>"."""
    if not line:
        return FILE_SYMBOL
    for unit in units or []:
        if unit["path"] == path and unit["start_line"] <= line <= unit["end_line"]:
            return unit["symbol"]
    return FILE_SYMBOL


def unit_summary(units: list[dict]) -> str:
    """One-line count of units and distinct files; "" when there are no units."""
    if not units:
        return ""
    files = sorted({unit["path"] for unit in units})
    return f"{len(units)} code object(s) across {len(files)} file(s)"

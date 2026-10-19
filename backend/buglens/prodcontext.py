"""Link ticket text to the code symbols a PR changed, plus a bounded external-signal block.

``objects.build_units`` already tells us which code objects a PR touched. This module answers
the next question: which of those objects does the linked ticket actually mention, so the
review can point a spec lens at the right functions and files. It also renders optional
production signals ("this error fired N times, see the dashboard") as a small, hard-budgeted
prompt block.

Pure and deterministic: plain strings and dicts in, strings and lists out. No I/O, no model,
no network, so the same inputs always give the same block.
"""
from __future__ import annotations

import re

MAX_SYMBOLS = 8
MAX_BLOCK_CHARS = 700

FILE_SYMBOL = "<file>"

_TICKET_HEADING = "## Ticket text maps to"
_SIGNALS_HEADING = "## External signals"


def _word_re(name: str) -> re.Pattern[str]:
    """Whole-word, ASCII-identifier boundary matcher for a literal symbol name."""
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", re.IGNORECASE)


def _cap(limit: int) -> int:
    try:
        return max(0, int(limit))
    except (TypeError, ValueError):
        return MAX_SYMBOLS


def map_symbols(text: str, units: list, limit: int = MAX_SYMBOLS) -> list:
    """Distinct unit symbols that occur in `text` as whole words, capped and ordered.

    Only units whose `symbol` is a non-empty string other than "<file>" are considered. A
    match is a case-insensitive whole-word occurrence (so `run` does not match `running`).
    Ordered by the symbol's first occurrence index in `text`, ties broken by name ascending.
    Returns [] for empty text or no match. Never raises.
    """
    if not text:
        return []
    first: dict[str, int] = {}
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        symbol = unit.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol == FILE_SYMBOL:
            continue
        if symbol in first:
            continue
        found = _word_re(symbol).search(text)
        if found:
            first[symbol] = found.start()
    ordered = sorted(first, key=lambda name: (first[name], name))
    return ordered[:_cap(limit)]


def ticket_block(text: str, units: list, limit: int = MAX_SYMBOLS) -> str:
    """Ticket text mapped to the paths of the symbols it names, or "" when nothing maps.

    Heading line first, then one ``- `symbol` (path)`` bullet per mapped symbol, using the
    first unit that carries that symbol for its path. Ends with a single trailing newline.
    """
    symbols = map_symbols(text, units, limit)
    if not symbols:
        return ""
    paths: dict[str, str] = {}
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        symbol = unit.get("symbol")
        if isinstance(symbol, str) and symbol not in paths:
            paths[symbol] = str(unit.get("path") or "")
    lines = [_TICKET_HEADING]
    for symbol in symbols:
        lines.append(f"- `{symbol}` ({paths.get(symbol, '')})")
    return "\n".join(lines) + "\n"


def signals_block(items: list, budget_chars: int = MAX_BLOCK_CHARS) -> str:
    """Render external signals as a bounded markdown block, or "" when there is nothing.

    One ``- [source] title`` line per non-blank item, in input order, with `` (count)`` only
    when `count` is not None and `` url`` only when `url` is a non-empty string. The whole
    block is truncated to `budget_chars` on a line boundary; if not even the first bullet
    fits, the heading line alone is returned. Always a str, never None.
    """
    lines: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        title = item.get("title")
        source_txt = "" if source is None else str(source)
        title_txt = "" if title is None else str(title)
        if not source_txt.strip() and not title_txt.strip():
            continue
        line = f"- [{source_txt}] {title_txt}"
        count = item.get("count")
        if count is not None:
            line += f" ({count})"
        url = item.get("url")
        if isinstance(url, str) and url:
            line += f" {url}"
        lines.append(line)
    if not lines:
        return ""
    try:
        budget = max(0, int(budget_chars))
    except (TypeError, ValueError):
        budget = MAX_BLOCK_CHARS
    out = [_SIGNALS_HEADING]
    used = len(_SIGNALS_HEADING)
    for line in lines:
        if used + 1 + len(line) > budget:
            break
        out.append(line)
        used += 1 + len(line)
    return "\n".join(out)

"""Ticket-to-symbol mapping and bounded external-signal block (Wave H)."""
from __future__ import annotations

from buglens import objects, prodcontext
from buglens.diff import parse_patch


def test_map_symbols_matches_whole_word_only():
    units = [{"symbol": "run", "path": "a.py"}]
    assert prodcontext.map_symbols("please run the tests", units) == ["run"]
    assert prodcontext.map_symbols("the running process", units) == []
    assert prodcontext.map_symbols("RUN it again", units) == ["run"]


def test_map_symbols_orders_by_first_occurrence():
    units = [
        {"symbol": "beta", "path": "b.py"},
        {"symbol": "alpha", "path": "a.py"},
        {"symbol": "gamma", "path": "c.py"},
    ]
    text = "beta then alpha then gamma"
    assert prodcontext.map_symbols(text, units) == ["beta", "alpha", "gamma"]
    assert prodcontext.map_symbols("gamma first, then beta", units) == ["gamma", "beta"]


def test_map_symbols_ties_break_by_name():
    units = [
        {"symbol": "run", "path": "a.py"},
        {"symbol": "Run", "path": "b.py"},
    ]
    assert prodcontext.map_symbols("Run", units) == ["Run", "run"]


def test_map_symbols_limit_and_file_placeholder():
    units = [
        {"symbol": "<file>", "path": "a.py"},
        {"symbol": "", "path": "b.py"},
        {"symbol": "alpha", "path": "a.py"},
        {"symbol": "beta", "path": "b.py"},
        {"symbol": "gamma", "path": "c.py"},
    ]
    assert prodcontext.map_symbols("alpha beta gamma", units, limit=2) == ["alpha", "beta"]
    assert prodcontext.map_symbols("alpha beta gamma", units) == ["alpha", "beta", "gamma"]
    assert prodcontext.map_symbols("", units) == []
    assert prodcontext.map_symbols("nothing here", units) == []


def test_map_symbols_distinct_and_deterministic():
    units = [
        {"symbol": "alpha", "path": "a.py"},
        {"symbol": "alpha", "path": "a.py"},
    ]
    assert prodcontext.map_symbols("alpha alpha", units) == ["alpha"]
    assert prodcontext.map_symbols("alpha", units) == prodcontext.map_symbols("alpha", units)


def test_ticket_block_empty_and_renders_paths():
    units = [
        {"symbol": "alpha", "path": "a.py"},
        {"symbol": "beta", "path": "b.py"},
    ]
    assert prodcontext.ticket_block("nothing mentioned", units) == ""
    block = prodcontext.ticket_block("please fix alpha and beta", units)
    assert block == (
        "## Ticket text maps to\n"
        "- `alpha` (a.py)\n"
        "- `beta` (b.py)\n"
    )
    assert block.endswith("\n") and not block.endswith("\n\n")


def test_ticket_block_uses_first_unit_for_path():
    units = [
        {"symbol": "alpha", "path": "first.py"},
        {"symbol": "alpha", "path": "second.py"},
    ]
    assert prodcontext.ticket_block("alpha", units) == (
        "## Ticket text maps to\n- `alpha` (first.py)\n"
    )


def test_signals_block_empty_and_all_blank():
    assert prodcontext.signals_block([]) == ""
    assert prodcontext.signals_block([{"source": "", "title": "  "}]) == ""
    assert prodcontext.signals_block([{"source": "", "title": ""}]) == ""
    assert prodcontext.signals_block([{"source": "  ", "title": ""}]) == ""


def test_signals_block_renders_variants():
    items = [
        {"source": "sentry", "title": "TypeError in x"},
        {"source": "datadog", "title": "slow query", "count": 3},
        {"source": "posthog", "title": "funnel drop", "count": 0, "url": "https://x"},
    ]
    lines = prodcontext.signals_block(items).splitlines()
    assert lines[0] == "## External signals"
    assert lines[1] == "- [sentry] TypeError in x"
    assert lines[2] == "- [datadog] slow query (3)"
    assert lines[3] == "- [posthog] funnel drop (0) https://x"


def test_signals_block_skips_blanks_and_keeps_order():
    items = [
        {"source": "", "title": ""},
        {"source": "sentry", "title": "boom"},
        {"source": "  ", "title": "  "},
        {"source": "datadog", "title": "lag"},
    ]
    assert prodcontext.signals_block(items).splitlines() == [
        "## External signals",
        "- [sentry] boom",
        "- [datadog] lag",
    ]


def test_signals_block_url_needs_nonempty_string():
    items = [{"source": "s", "title": "t", "url": ""}, {"source": "s", "title": "u", "url": "http://z"}]
    assert prodcontext.signals_block(items).splitlines() == [
        "## External signals",
        "- [s] t",
        "- [s] u http://z",
    ]


def test_signals_block_truncates_on_line_boundary():
    items = [
        {"source": "sentry", "title": "a" * 40},
        {"source": "sentry", "title": "b" * 40},
        {"source": "sentry", "title": "c" * 40},
    ]
    block = prodcontext.signals_block(items, budget_chars=75)
    assert block == "## External signals\n- [sentry] " + "a" * 40
    assert len(block) <= 75


def test_signals_block_heading_only_when_even_first_line_misses():
    items = [{"source": "sentry", "title": "a" * 40}]
    assert prodcontext.signals_block(items, budget_chars=5) == "## External signals"


def test_blocks_are_deterministic():
    units = [{"symbol": "alpha", "path": "a.py"}, {"symbol": "beta", "path": "b.py"}]
    items = [{"source": "sentry", "title": "boom", "count": 2}]
    assert prodcontext.ticket_block("alpha beta", units) == prodcontext.ticket_block("alpha beta", units)
    assert prodcontext.signals_block(items) == prodcontext.signals_block(items)


def test_realistic_units_from_patch():
    patch = (
        "@@ -0,0 +1,5 @@\n"
        "+def compute_total(items):\n"
        "+    return sum(items)\n"
        "+\n"
        "+def render_row(item):\n"
        "+    return str(item)\n"
    )
    fd = parse_patch(patch, path="cart.py")
    units = objects.build_units([fd])
    assert [u["symbol"] for u in units] == ["compute_total", "render_row"]
    assert prodcontext.map_symbols("the ticket says compute_total is wrong", units) == ["compute_total"]
    assert prodcontext.ticket_block("fix render_row", units) == (
        "## Ticket text maps to\n- `render_row` (cart.py)\n"
    )

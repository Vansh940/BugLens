from types import SimpleNamespace

from buglens import learn


def test_lens_ranking_demotes_and_trusts():
    stats = {
        "security": {"raised": 10, "up": 2, "down": 6, "fixed": 1, "unfixed": 1},
        "tests": {"raised": 10, "up": 6, "down": 2, "fixed": 1, "unfixed": 1},
        "ui": {"raised": 3, "up": 0, "down": 3, "fixed": 0, "unfixed": 0},
    }
    demoted, trusted = learn.lens_ranking(stats)
    assert demoted == ["security"]
    assert trusted == ["tests"]
    assert "ui" not in demoted
    assert "ui" not in trusted


def test_lens_ranking_orders_by_rate_then_name():
    stats = {
        "z": {"raised": 10, "down": 8, "up": 1},
        "a": {"raised": 10, "down": 8, "up": 1},
        "b": {"raised": 10, "down": 4, "up": 3},
        "c": {"raised": 10, "down": 1, "up": 5},
    }
    demoted, trusted = learn.lens_ranking(stats)
    assert demoted == ["a", "z"]
    assert trusted == ["c", "b"]


def test_order_lenses_demotes_middle_to_end():
    lenses = [SimpleNamespace(name=n) for n in ("correctness", "ui", "security")]
    out = learn.order_lenses(lenses, {"ui"})
    assert [l.name for l in out] == ["correctness", "security", "ui"]
    assert len(out) == len(lenses)
    assert out is not lenses
    assert [l.name for l in lenses] == ["correctness", "ui", "security"]


def test_learn_block_empty_is_blank():
    assert learn.learn_block([], {}) == ""


def test_learn_block_skips_nonpositive_scope():
    scopes = [{"scope": "a/*", "lens": "ui", "downs": 2, "ups": 2, "sample": ""}]
    assert learn.learn_block(scopes, {}) == ""


def test_learn_block_heading_and_bullet():
    scopes = [{"scope": "app/api/*", "lens": "performance", "downs": 5, "ups": 3, "sample": "x"}]
    block = learn.learn_block(scopes, {})
    assert block.splitlines()[0] == (
        "## Feedback on this repo (learned from reactions; do not repeat dismissed noise)"
    )
    assert (
        "- the team repeatedly dismissed performance findings in `app/api/*` "
        "(2 times) - raise fewer of these"
    ) in block


def test_learn_block_lists_most_downvoted_lenses():
    stats = {"security": {"raised": 10, "down": 8, "up": 1}}
    block = learn.learn_block([], stats)
    assert block.splitlines()[-1] == "Lenses this repo down-votes most: security"


def test_learn_block_truncates_to_budget():
    scopes = [
        {"scope": f"app/mod{i}/*", "lens": "ui", "downs": 9, "ups": 0, "sample": ""}
        for i in range(10)
    ]
    block = learn.learn_block(scopes, {}, budget_chars=120)
    assert block
    assert len(block) <= 120


def test_candidate_rules_ignore_glob_and_lens():
    scopes = [{"scope": "app/api/*", "lens": "performance", "downs": 6, "ups": 1, "sample": "x"}]
    stats = {"ui": {"raised": 10, "down": 7, "up": 1}}
    rules = learn.candidate_rules(scopes, stats)
    kinds = {r["kind"] for r in rules}
    assert "ignore-glob" in kinds
    assert "focus-drop-lens" in kinds

    ig = next(r for r in rules if r["kind"] == "ignore-glob")
    assert ig["yaml"].startswith("ignore_paths:")
    assert ig["scope"] == "app/api/*"
    assert ig["lens"] is None
    assert ig["id"] == "learn:ignore:app/api/*"

    foc = next(r for r in rules if r["kind"] == "focus-drop-lens")
    assert foc["lens"] == "ui"
    assert foc["scope"] is None
    assert foc["id"] == "learn:lens:ui"
    assert foc["yaml"].startswith("# focus: []")

    assert len({r["id"] for r in rules}) == len(rules)
    assert len(rules) <= learn.MAX_SUGGESTIONS


def test_candidate_rules_respects_min_downs():
    scopes = [{"scope": "app/api/*", "lens": "ui", "downs": 2, "ups": 0, "sample": ""}]
    assert learn.candidate_rules(scopes, {}) == []
    assert learn.candidate_rules(scopes, {}, min_downs=2)[0]["kind"] == "ignore-glob"


def test_candidate_rules_capped_and_sorted():
    scopes = [
        {"scope": f"app/mod{i}/*", "lens": "ui", "downs": 5, "ups": 0, "sample": ""}
        for i in range(10)
    ]
    rules = learn.candidate_rules(scopes, {})
    assert len(rules) == learn.MAX_SUGGESTIONS
    assert [r["scope"] for r in rules] == sorted(r["scope"] for r in rules)


def test_candidate_rules_deterministic():
    scopes = [
        {"scope": "app/api/*", "lens": "performance", "downs": 6, "ups": 1, "sample": "x"},
        {"scope": "app/web/*", "lens": "ui", "downs": 4, "ups": 0, "sample": "y"},
    ]
    stats = {
        "ui": {"raised": 10, "down": 7, "up": 1},
        "security": {"raised": 8, "down": 5, "up": 0},
    }
    assert learn.candidate_rules(scopes, stats) == learn.candidate_rules(scopes, stats)

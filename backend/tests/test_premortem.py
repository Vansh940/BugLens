from buglens import premortem as pm
from buglens.diff import ADD, DiffLine, FileDiff


def fd(path, added=1, status="modified"):
    lines = [DiffLine(kind=ADD, text=f"line {i}", new_line=i + 1, old_line=None)
             for i in range(added)]
    return FileDiff(path=path, status=status, lines=lines)


def test_trigger_matches_a_whole_path_token_not_a_substring():
    assert pm.triggers([fd("api/routes/user.py", status="added")]) == ["new route"]
    assert pm.triggers([fd("author.py")]) == []
    assert pm.triggers([fd("src/authorship.py")]) == []


def test_trigger_migration_and_auth_paths():
    assert "migration" in pm.triggers([fd("db/migrations/001_init.sql")])
    assert pm.triggers([fd("alembic/versions/abc.py")]) == ["migration"]
    assert "auth/security path" in pm.triggers([fd("src/auth/login.py")])
    assert "auth/security path" in pm.triggers([fd("app/token_store.py")])


def test_new_route_requires_added_or_renamed_status():
    assert pm.triggers([fd("api/routes/user.py", status="modified")]) == []
    assert pm.triggers([fd("api/routes/user.py", status="added")]) == ["new route"]


def test_large_change_fires_on_total_added_lines():
    below = [fd(f"a{i}.py", added=50) for i in range(3)]         # 150 lines
    assert pm.triggers(below) == []
    above = [fd(f"a{i}.py", added=70) for i in range(3)]         # 210 lines
    assert "large change" in pm.triggers(above)


def test_triggers_are_ordered_and_deduped():
    fds = [fd("api/auth/routes/login.py", status="added"), fd("api/auth/routes/session.py", status="added")]
    assert pm.triggers(fds) == ["new route", "auth/security path"]


def test_tier_needs_the_mechanism_on_and_a_trigger():
    assert pm.tier(True, ["migration"]) == "deep"
    assert pm.tier(True, []) == "quick"
    assert pm.tier(False, ["migration"]) == "quick"


def test_select_keeps_only_anchored_items_and_dedupes():
    fds = [fd("api/routes/user.py", added=5, status="added")]
    items = [
        {"title": "keep", "why": "w", "path": "api/routes/user.py", "line": 3},
        {"title": "off-diff-line", "why": "w", "path": "api/routes/user.py", "line": 99},
        {"title": "unknown-file", "why": "w", "path": "nope.py", "line": 1},
        {"title": "no-line", "why": "w", "path": "api/routes/user.py", "line": None},
        {"title": "dupe", "why": "w", "path": "api/routes/user.py", "line": 3},
        "not a dict",
    ]
    kept = pm.select(items, fds)
    assert [k["title"] for k in kept] == ["keep"]


def test_select_caps_at_max_items():
    fds = [fd("api/routes/user.py", added=10, status="added")]
    items = [{"title": f"t{i}", "why": "w", "path": "api/routes/user.py", "line": i + 1}
             for i in range(10)]
    assert len(pm.select(items, fds)) == pm.MAX_ITEMS


def test_render_md_is_collapsible_and_empty_when_nothing():
    assert pm.render_md([]) == []
    md = "\n".join(pm.render_md([{"title": "Cache stampede", "why": "cold start", "path": "a.py", "line": 4}]))
    assert "<details>" in md and "</details>" in md
    assert "Cache stampede" in md
    assert "`a.py:4`" in md

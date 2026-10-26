from buglens import lenses as L


def test_builtin_experts_present():
    names = {l.name for l in L.LENSES}
    assert {"correctness", "security", "performance", "ui", "tests", "maintainability"} <= names
    assert L.MERGE.name == "merge"


def test_select_lenses_for_python_defaults():
    assert [l.name for l in L.select_lenses("a.py", "python")] == ["correctness", "security"]


def test_ui_lens_activates_on_frontend_files():
    names = [l.name for l in L.select_lenses("App.tsx", "typescript")]
    assert "ui" in names


def test_focus_is_an_allow_list():
    assert [l.name for l in L.select_lenses("a.py", "python", focus=["security"])] == ["security"]
    assert L.select_lenses("a.py", "python", focus=["ui"]) == []


def test_custom_lens_from_markdown():
    custom = L.load_custom_lenses({
        ".buglens/lenses/sql-safety.md":
            "# SQL Safety\nexts: sql\ncategory: security\nNo string-built queries; watch SELECT *.",
    })
    assert len(custom) == 1
    lens = custom[0]
    assert lens.name == "sql-safety"
    assert lens.category == "security"
    pool = L.LENSES + custom
    assert "sql-safety" in [l.name for l in L.select_lenses("q.sql", "sql", pool=pool)]
    assert "sql-safety" not in [l.name for l in L.select_lenses("a.py", "python", pool=pool)]

from buglens import walkthrough as w


class FD:
    def __init__(self, path, status="modified", added_chars=10):
        self.path = path
        self.status = status
        self.added_chars = added_chars


def test_classify_first_match_wins():
    assert w.classify("tests/test_models.py") == "tests"
    assert w.classify("src/models/user.py") == "model"
    assert w.classify("docs/api.md") == "docs"
    assert w.classify("src/api/routes.py") == "api"
    assert w.classify("web/components/Button.tsx") == "ui"
    assert w.classify("migrations/0001_init.sql") == "migration"
    assert w.classify(".github/workflows/ci.yml") == "config"
    assert w.classify("src/service/order.go") == "logic"


def test_build_change_stack_groups_and_orders():
    files = [
        FD("src/api/routes.py"),
        FD("src/models/user.py"),
        FD("tests/test_api.py"),
        FD("src/service/order.py", added_chars=100),
    ]
    findings = [{"path": "src/service/order.py", "severity": "major", "title": "NPE on empty cart"}]
    stack = w.build_change_stack(files, findings)

    assert [c["key"] for c in stack] == ["model", "logic", "api", "tests"]
    logic = next(c for c in stack if c["key"] == "logic")
    assert logic["files"] == ["src/service/order.py"]
    assert logic["added"] == 100
    assert "1 finding(s)" in logic["note"] and "NPE on empty cart" in logic["note"]


def test_note_reports_all_new_files():
    stack = w.build_change_stack([FD("docs/a.md", status="added")], [])
    assert stack[0]["key"] == "docs"
    assert "1 file, all new" in stack[0]["note"]


def test_empty_input_is_empty_stack():
    assert w.build_change_stack([], []) == []

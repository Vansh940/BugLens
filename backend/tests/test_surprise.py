from buglens import surprise as sp
from buglens.diff import ADD, DiffLine, FileDiff


def fd(path, status="modified"):
    return FileDiff(path=path, status=status,
                    lines=[DiffLine(kind=ADD, text="x", new_line=1, old_line=None)])


def test_pr_kind_reads_a_conventional_prefix_or_the_first_word():
    assert sp.pr_kind("fix(api): handle empty") == "fix"
    assert sp.pr_kind("feat: add pagination") == "feature"
    assert sp.pr_kind("Fix the crash") == "fix"
    assert sp.pr_kind("docs: update readme") == "docs"
    assert sp.pr_kind("chore(deps): bump axios") == "chore"
    assert sp.pr_kind("Improve performance") == "unknown"
    assert sp.pr_kind("") == "unknown"


def test_docs_pr_touching_code_is_a_surprise():
    assert sp.detect("docs", [fd("docs/guide.md"), fd("app/service.py")]) == [
        "docs PR changes code: app/service.py"]


def test_tests_only_pr_touching_product_code_is_a_surprise():
    assert sp.detect("test", [fd("tests/test_a.py"), fd("app/service.py")]) == [
        "tests-only PR changes product code: app/service.py"]


def test_migration_touched_by_a_refactor_or_a_fix():
    assert sp.detect("refactor", [fd("db/migrations/002.sql")]) == [
        "refactor touches a schema migration: db/migrations/002.sql"]
    assert sp.detect("fix", [fd("db/migrations/002.sql")]) == [
        "bug fix touches a schema migration: db/migrations/002.sql"]


def test_ui_change_touching_auth_is_a_surprise():
    assert sp.detect("feature", [fd("web/components/Login.tsx"), fd("src/auth/token_store.py")]) == [
        "UI change touches an auth/security path: src/auth/token_store.py"]


def test_dependency_added_by_a_non_feature_pr():
    assert sp.detect("fix", [fd("package.json", status="added")]) == [
        "fix PR adds a dependency: package.json"]


def test_a_feature_adding_a_dependency_is_not_a_surprise():
    assert sp.detect("feature", [fd("package.json", status="added")]) == []


def test_no_surprise_returns_empty():
    assert sp.detect("feature", [fd("app/service.py"), fd("tests/test_service.py")]) == []

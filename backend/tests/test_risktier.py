from buglens import risktier as rt
from buglens.diff import ADD, DiffLine, FileDiff


def fd(path, added=1, status="modified"):
    lines = [DiffLine(kind=ADD, text=f"line {i}", new_line=i + 1, old_line=None)
             for i in range(added)]
    return FileDiff(path=path, status=status, lines=lines)


def test_no_files_is_l0_and_renders_nothing():
    t = rt.tier([], [])
    assert t["level"] == "L0"
    assert rt.render_header_md(t["level"], t["reasons"]) == ""


def test_plain_code_change_with_a_test_stays_l1():
    fds = [fd("app/logic.py"), fd("tests/test_logic.py")]
    t = rt.tier([], fds)
    assert t["level"] == "L1"
    assert t["reasons"] == ["small, low-risk change"]
    assert "L1" in rt.render_header_md(t["level"], t["reasons"])


def test_medium_finding_is_l2():
    t = rt.tier([{"severity": "major"}], [fd("app/logic.py"), fd("tests/test_logic.py")])
    assert t["level"] == "L2"
    assert "medium-severity finding" in t["reasons"]


def test_blocker_finding_is_l3():
    t = rt.tier([{"severity": "blocker"}], [fd("app/logic.py"), fd("tests/test_logic.py")])
    assert t["level"] == "L3"
    assert rt.advisory(t["level"]) == "read closely"


def test_auth_path_and_migration_and_secrets_are_l3():
    assert rt.tier([], [fd("src/auth/login.py"), fd("tests/test_a.py")])["level"] == "L3"
    assert rt.tier([], [fd("db/migrations/001.sql"), fd("tests/test_a.py")])["level"] == "L3"
    assert rt.tier([], [fd("app/logic.py"), fd("tests/test_a.py")], secrets_flagged=True)["level"] == "L3"


def test_weak_evidence_bumps_a_plain_change_to_l2():
    t = rt.tier([], [fd("app/logic.py")])
    assert t["level"] == "L2"
    assert any(r.startswith("weak evidence") for r in t["reasons"])


def test_large_diff_is_l2_and_reasons_are_named():
    t = rt.tier([], [fd("app/big.py", added=250), fd("tests/test_big.py")])
    assert t["level"] == "L2"
    assert any("large diff" in r for r in t["reasons"])
    md = rt.render_header_md(t["level"], t["reasons"])
    assert "**Review depth recommended: L2**" in md

import pytest

from buglens import store


@pytest.fixture(autouse=True)
async def _schema(db):
    await store.init(db)
    return db


def _item(fingerprint, line=1, comment_id=None, node_id=None, path="a.py"):
    return {"fingerprint": fingerprint, "path": path, "line": line,
            "comment_id": comment_id, "node_id": node_id}


async def test_init_creates_posted_comments_table(db):
    async with store._open(db) as conn:
        row = await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='posted_comments'"
        )).fetchone()
    assert row is not None


async def test_save_then_read_roundtrip(db):
    await store.save_posted_comments("o/r", 1, "sha1", [
        _item("fp_a", line=3, comment_id=111, node_id="n_a", path="a.py"),
        _item("fp_b", line=7, comment_id="222", node_id="n_b", path="b.py"),
    ], db)
    rows = await store.posted_comments("o/r", 1, db)
    assert len(rows) == 2
    by_fp = {r["fingerprint"]: r for r in rows}
    assert by_fp["fp_a"]["path"] == "a.py" and by_fp["fp_a"]["line"] == 3
    assert by_fp["fp_a"]["comment_id"] == "111" and by_fp["fp_a"]["node_id"] == "n_a"
    assert by_fp["fp_a"]["head_sha"] == "sha1"
    assert by_fp["fp_b"]["comment_id"] == "222" and by_fp["fp_b"]["node_id"] == "n_b"


async def test_read_ordered_by_created_at(db):
    await store.save_posted_comments("o/r", 1, "sha1", [
        _item("fp_a"), _item("fp_b"),
    ], db)
    async with store._open(db) as conn:
        await conn.execute("UPDATE posted_comments SET created_at='2020-01-01T00:00:00Z'"
                           " WHERE fingerprint='fp_b'")
        await conn.execute("UPDATE posted_comments SET created_at='2021-01-01T00:00:00Z'"
                           " WHERE fingerprint='fp_a'")
        await conn.commit()
    rows = await store.posted_comments("o/r", 1, db)
    assert [r["fingerprint"] for r in rows] == ["fp_b", "fp_a"]


async def test_resave_updates_single_row(db):
    await store.save_posted_comments("o/r", 2, "sha1", [
        _item("fp_x", line=1, comment_id=1, node_id="old"),
    ], db)
    await store.save_posted_comments("o/r", 2, "sha2", [
        _item("fp_x", line=9, comment_id=999, node_id="new"),
    ], db)
    rows = await store.posted_comments("o/r", 2, db)
    assert len(rows) == 1
    assert rows[0]["line"] == 9
    assert rows[0]["comment_id"] == "999"
    assert rows[0]["node_id"] == "new"
    assert rows[0]["head_sha"] == "sha2"


async def test_items_without_fingerprint_are_skipped(db):
    await store.save_posted_comments("o/r", 3, "sha", [
        {"fingerprint": "", "path": "a.py", "line": 1},
        {"path": "b.py", "line": 2},
        _item("fp_ok", path="c.py", line=3),
    ], db)
    rows = await store.posted_comments("o/r", 3, db)
    assert [r["fingerprint"] for r in rows] == ["fp_ok"]


async def test_other_repo_or_pr_is_empty(db):
    await store.save_posted_comments("o/r", 1, "sha", [_item("fp_a")], db)
    assert await store.posted_comments("o/other", 1, db) == []
    assert await store.posted_comments("o/r", 2, db) == []


async def test_forget_removes_only_targeted_row(db):
    await store.save_posted_comments("o/r", 4, "sha", [
        _item("fp_a"), _item("fp_b"),
    ], db)
    await store.forget_posted_comment("o/r", 4, "fp_a", db)
    assert [r["fingerprint"] for r in await store.posted_comments("o/r", 4, db)] == ["fp_b"]
    await store.forget_posted_comment("o/r", 4, "absent", db)
    assert len(await store.posted_comments("o/r", 4, db)) == 1

import pytest

from buglens import store


@pytest.fixture(autouse=True)
async def _schema(db):
    await store.init(db)
    return db


async def test_claim_run_is_idempotent_per_revision(db):
    first = await store.claim_run("o/r", 1, "sha1", "base", db)
    assert first
    assert await store.claim_run("o/r", 1, "sha1", "base", db) is None   # same revision
    assert await store.claim_run("o/r", 1, "sha2", "base", db)           # new revision


async def test_record_delivery_dedupes(db):
    assert await store.record_delivery("d1", "o/r", 1, "sha", db) is True
    assert await store.record_delivery("d1", "o/r", 1, "sha", db) is False


async def test_outcomes_suppress_only_after_threshold(db):
    await store.record_outcome("o/r", "fp1", "down", "noisy", db)
    assert "fp1" not in await store.suppressed("o/r", path=db)
    await store.record_outcome("o/r", "fp1", "down", "noisy", db)
    assert "fp1" in await store.suppressed("o/r", path=db)


async def test_upvotes_become_confirmed_good(db):
    await store.record_outcome("o/r", "fp2", "up", "good catch", db)
    assert "good catch" in await store.confirmed_good("o/r", path=db)


async def test_posted_fingerprints_roundtrip(db):
    run_id = await store.claim_run("o/r", 1, "sha", "base", db)
    findings = [{"fingerprint": "fp9", "path": "a.py", "line": 1, "severity": "major",
                 "category": "bug", "lens": "correctness", "title": "t", "body": "b",
                 "fix": "", "confidence": 0.7}]
    await store.save_findings(run_id, findings, db)
    assert await store.posted_fingerprints("o/r", db) == set()   # not posted yet
    await store.mark_posted(run_id, db)
    assert await store.posted_fingerprints("o/r", db) == {"fp9"}


async def test_stats_counts_rows(db):
    await store.claim_run("o/r", 1, "sha", "base", db)
    stats = await store.stats(db)
    assert stats["runs"] == 1


async def test_file_cache_roundtrip_and_expiry(db):
    assert await store.get_file_cache("o/r", "k1", path=db) is None
    await store.put_file_cache("o/r", "k1", [{"title": "t"}], db)
    assert await store.get_file_cache("o/r", "k1", path=db) == [{"title": "t"}]
    assert await store.get_file_cache("o/r", "k1", max_age_s=0, path=db) is None


async def test_store_uses_wal_and_busy_timeout(db):
    import aiosqlite

    await store.init(db)
    async with aiosqlite.connect(db) as raw:
        mode = (await (await raw.execute("PRAGMA journal_mode")).fetchone())[0]
    assert mode.lower() == "wal"


async def test_connection_pool_is_bounded(db, monkeypatch):
    """Concurrency must reuse connections, not spawn a thread per caller."""
    import asyncio
    import aiosqlite

    created = 0
    real_connect = aiosqlite.connect

    async def counting_connect(*a, **k):
        nonlocal created
        created += 1
        return await real_connect(*a, **k)

    monkeypatch.setattr(store.aiosqlite, "connect", counting_connect)
    await store.init(db)
    created = 0                       # ignore init's connection

    async def touch(i):
        async with store._open(db) as conn:
            await conn.execute("SELECT 1")
            await asyncio.sleep(0.01)   # hold a slot so callers overlap

    await asyncio.gather(*[touch(i) for i in range(24)])
    assert created <= store._POOL_SIZE


async def test_concurrent_writers_do_not_raise_locked(db):
    """WAL + busy_timeout: a burst of writers must queue, not fail."""
    import asyncio

    await store.init(db)
    await asyncio.gather(*[store.record_outcome("o/r", f"fp{i}", "down", "r", db) for i in range(40)])
    await asyncio.gather(*[store.record_outcome("o/r", f"fp{i}", "down", "r", db) for i in range(40)])
    assert len(await store.suppressed("o/r", path=db)) == 40


# ── iteration 14: reliability + scale ─────────────────────────────────────────
async def test_stale_unfinished_run_is_reclaimed(db):
    """A crashed review must not block the revision forever."""
    from buglens import store

    await store.init(db)
    first = await store.claim_run("o/r", 1, "sha1", "base", db)
    assert first
    assert await store.claim_run("o/r", 1, "sha1", "base", db) is None   # in flight, not stale

    async with store._open(db) as conn:
        await conn.execute("UPDATE runs SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (first,))
        await conn.commit()
    assert await store.claim_run("o/r", 1, "sha1", "base", db) == first  # reclaimed


async def test_finished_run_is_never_reclaimed(db):
    from buglens import store

    await store.init(db)
    run = await store.claim_run("o/r", 1, "sha1", "base", db)
    await store.finish_run(run, "looks-good", 100, 0, False, db)
    async with store._open(db) as conn:
        await conn.execute("UPDATE runs SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (run,))
        await conn.commit()
    assert await store.claim_run("o/r", 1, "sha1", "base", db) is None


async def test_index_version_changes_when_index_written(db):
    from buglens import store

    await store.init(db)
    before = await store.index_version("o/r", db)
    await store.put_index_many("o/r", [{"path": "a.py", "sha": "s1", "lang": "python"}], db)
    after = await store.index_version("o/r", db)
    assert before != after


async def test_sweep_expires_file_cache_and_dormant_repos(db):
    import time

    from buglens import store

    await store.init(db)
    await store.put_file_cache("o/r", "key1", [{"path": "a.py"}], db)
    await store.put_index_many("o/r", [{"path": "a.py", "sha": "s1", "lang": "python"}], db)
    assert await store.get_file_cache("o/r", "key1", path=db) is not None

    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 400 * 86400))
    await store.claim_run("o/r", 1, "sha1", "base", db)
    async with store._open(db) as conn:
        await conn.execute("UPDATE file_cache SET created_at=?", (old,))
        await conn.execute("UPDATE runs SET created_at=?", (old,))
        await conn.commit()

    removed = await store.sweep(days_cache=7, days_repo=30, path=db)
    assert removed["file_cache"] == 1 and removed["repo_index"] == 1
    assert await store.get_file_cache("o/r", "key1", path=db) is None


# ── iteration 16: re-review round ledger ──────────────────────────────────────
async def test_count_rounds_increments_per_claimed_run(db):
    await store.init(db)
    r1 = await store.claim_run("o/r", 7, "shaA", None, db)
    assert r1 is not None
    assert await store.count_rounds("o/r", 7, db) == 1
    await store.finish_run(r1, "ok", 100, 0, False, db)
    assert await store.claim_run("o/r", 7, "shaB", None, db) is not None
    assert await store.count_rounds("o/r", 7, db) == 2
    assert await store.count_rounds("o/other", 7, db) == 0


async def test_pr_round_files_excludes_current_round(db):
    await store.init(db)
    await store.record_round_files("o/r", 3, 1, [("a.py", "sha1"), ("b.py", "sha2")], db)

    assert await store.reviewed_contents("o/r", 3, 2, db) == {"a.py": {"sha1"}, "b.py": {"sha2"}}
    assert await store.reviewed_contents("o/r", 3, 1, db) == {}      # first_round is not "earlier"

    await store.record_round_files("o/r", 3, 4, [("a.py", "sha1")], db)   # re-seen keeps round 1
    await store.record_round_files("o/r", 3, 4, [("a.py", "sha9")], db)   # genuinely new content
    seen = await store.reviewed_contents("o/r", 3, 5, db)
    assert seen["a.py"] == {"sha1", "sha9"}


async def test_record_round_files_skips_empty_hashes(db):
    await store.init(db)
    await store.record_round_files("o/r", 1, 1, [("a.py", ""), ("", "x")], db)
    assert await store.reviewed_contents("o/r", 1, 2, db) == {}

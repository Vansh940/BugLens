"""Micro-benchmarks for the hot paths. Run: python bench.py  (cwd = backend)

Measures the things that scale with repo size and review volume:

* code-graph construction and retrieval (runs on every review)
* store round-trips and concurrent writes (SQLite, many calls per review)

Each measurement is the median of several repeats, because single-shot numbers on a
laptop vary by an order of magnitude. Run before and after a performance change; the
numbers quoted in docs/operations.md come from here.
"""
import asyncio
import os
import random
import statistics
import tempfile
import time

os.environ.setdefault("BUGLENS_DB", os.path.join(tempfile.mkdtemp(), "bench.sqlite"))

from buglens import indexer, pipeline, store  # noqa: E402

REPEATS = 5


def synth(n_files: int) -> dict[str, str]:
    """A synthetic repo with a shallow package tree and a sprinkle of tests."""
    random.seed(7)
    files: dict[str, str] = {}
    for i in range(n_files):
        depth = i % 4
        pkg = "/".join(f"p{d}" for d in range(depth)) or "."
        imports = [f"from p0.mod{j} import helper{j}" for j in random.sample(range(i), min(3, i))]
        files[f"{pkg}/mod{i}.py"] = (
            '"""Module docstring."""\n'
            + "\n".join(imports)
            + f"\n\n\ndef helper{i}(x):\n    return x + {i}\n"
            + f"\n\nclass Service{i}:\n    def run(self, v):\n        return helper{i}(v)\n"
        )
    for i in range(max(1, n_files // 20)):
        files[f"tests/test_mod{i}.py"] = (
            f"from p0.mod{i} import helper{i}\n\n\ndef test_helper{i}():\n    assert helper{i}(1)\n"
        )
    return files


async def async_median_ms(coro_fn, repeats: int = REPEATS) -> tuple[float, object]:
    times: list[float] = []
    out = None
    for _ in range(repeats):
        start = time.perf_counter()
        out = await coro_fn()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), out


def sync_median_ms(fn, repeats: int = REPEATS) -> tuple[float, object]:
    times: list[float] = []
    out = None
    for _ in range(repeats):
        start = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), out


def report(label: str, ms: float, extra: str) -> None:
    print(f"[{label:<9}] {ms:9.1f} ms   {extra}")


async def engine_benchmarks() -> None:
    for n in (500, 2000):
        files = synth(n)
        paths = list(files)
        index = {p: indexer.parse_text(p, text) for p, text in files.items()}
        changed = random.sample(paths, 5)

        ms, _ = sync_median_ms(lambda: [indexer.parse_text(p, t) for p, t in files.items()])
        report("parse", ms, f"{n} files")

        ms, graph = sync_median_ms(lambda: indexer.build_graph(index))
        report("graph", ms, f"{n} files")

        ms, related = sync_median_ms(lambda: indexer.related_files(index, graph, changed))
        report("related", ms, f"{n} files -> {len(related)} picked")

        ms, _ = sync_median_ms(lambda: indexer.tests_for(index, changed[0]))
        report("tests", ms, f"{n} files")

        ms, _ = sync_median_ms(lambda: indexer.context_pack(index, related))
        report("pack", ms, f"{n} files")
        print()


async def store_benchmarks() -> None:
    db = os.environ["BUGLENS_DB"]
    await store.init(db)
    records = [{"path": f"f{i}.py", "sha": f"s{i}", "lang": "python", "defs": []}
               for i in range(300)]

    ms, _ = await async_median_ms(lambda: store.put_index_many("o/r", records, db))
    report("sqlite", ms, "put_index_many(300)")

    async def _reads():
        for _ in range(100):
            await store.get_index_state("o/r", db)

    ms, _ = await async_median_ms(_reads)
    report("sqlite", ms, "get_index_state x100")

    async def _misses():
        for i in range(100):
            await store.get_file_cache("o/r", f"missing{i}", path=db)

    ms, _ = await async_median_ms(_misses)
    report("sqlite", ms, "cache miss x100")

    async def _burst():
        await asyncio.gather(*[store.record_outcome("o/r", f"fp{i}", "down", "t", db)
                               for i in range(50)])

    ms, _ = await async_median_ms(_burst, 3)
    report("sqlite", ms, "50 concurrent writes")

    async def _writes():
        for i in range(100):
            await store.record_outcome("o/r", f"x{i}", "down", "t", db)

    ms, _ = await async_median_ms(_writes, 3)
    report("sqlite", ms / 100, "write p50 (median of 100)")

    # Code-graph load: the pipeline caches (index, graph) by index version, so a warm read
    # should not touch the 300-row table at all.
    big = [{"path": f"p{i % 20}/mod{i}.py", "sha": f"s{i}", "lang": "python",
            "module": f"p{i % 20}.mod{i}", "defs": [], "imports": [], "refs": [], "tests": False}
           for i in range(2000)]
    await store.put_index_many("bench/repo", big, db)

    async def _cold():
        pipeline._GRAPH_CACHE.clear()
        return await pipeline._load_graph("bench/repo", db)

    cold_ms, _ = await async_median_ms(_cold, 3)

    async def _warm():
        return await pipeline._load_graph("bench/repo", db)

    warm_ms, _ = await async_median_ms(_warm, 5)
    report("graph", cold_ms, "load 2000-file index (cold)")
    report("graph", warm_ms, "load 2000-file index (cached)")


async def main() -> None:
    await engine_benchmarks()
    await store_benchmarks()
    await store.close_pool()


if __name__ == "__main__":
    asyncio.run(main())

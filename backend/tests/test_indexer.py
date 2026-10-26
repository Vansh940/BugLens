from buglens import indexer


PY_UTIL = '''"""Small helpers."""


def parse(value):
    return int(value)


class Widget:
    """A widget."""

    def render(self, x):
        return parse(x)
'''

PY_MAIN = '''from app.util import parse, Widget


def run(data):
    return Widget().render(parse(data))
'''

PY_TEST = '''from app.util import parse


def test_parse():
    assert parse("1") == 1
'''

JS = '''import { helper } from "./util";
export function run(x) { return helper(x); }
'''


def test_parse_python_defs_imports_refs():
    rec = indexer.parse_text("app/util.py", PY_UTIL)
    names = {d["name"] for d in rec["defs"]}
    assert {"parse", "Widget", "Widget.render"} <= names
    assert rec["lang"] == "python"
    assert rec["doc"] == "Small helpers."
    assert any(r["name"] == "parse" for r in rec["refs"])
    assert rec["imports"] == []


def test_parse_python_imports():
    rec = indexer.parse_text("app/main.py", PY_MAIN)
    mods = {i["module"] for i in rec["imports"]}
    assert "app.util" in mods


def test_parse_javascript_fallback():
    rec = indexer.parse_text("src/main.js", JS)
    assert rec["lang"] == "javascript"
    assert {d["name"] for d in rec["defs"]} >= {"run"}
    assert any(i["module"] == "./util" for i in rec["imports"])


def test_test_file_detection():
    assert indexer.is_test_file("tests/test_a.py")
    assert indexer.is_test_file("src/a.spec.ts")
    assert indexer.is_test_file("pkg/foo_test.go")
    assert not indexer.is_test_file("app/util.py")


def test_build_graph_and_reverse_deps():
    index = {p: indexer.parse_text(p, t) for p, t in
             {"app/util.py": PY_UTIL, "app/main.py": PY_MAIN, "tests/test_util.py": PY_TEST}.items()}
    graph = indexer.build_graph(index)
    assert "app/util.py" in graph["app/main.py"]
    assert "app/main.py" in indexer.reverse_deps(graph, "app/util.py")


def test_tests_for_finds_importing_test():
    index = {p: indexer.parse_text(p, t) for p, t in
             {"app/util.py": PY_UTIL, "tests/test_util.py": PY_TEST}.items()}
    assert indexer.tests_for(index, "app/util.py") == {"tests/test_util.py"}


def test_related_files_ranks_neighbours_and_tests():
    index = {p: indexer.parse_text(p, t) for p, t in
             {"app/util.py": PY_UTIL, "app/main.py": PY_MAIN, "tests/test_util.py": PY_TEST}.items()}
    graph = indexer.build_graph(index)
    related = indexer.related_files(index, graph, ["app/main.py"])
    assert "app/util.py" in related
    assert "app/main.py" not in related


def test_context_pack_is_bounded_and_lists_defs():
    index = {"app/util.py": indexer.parse_text("app/util.py", PY_UTIL)}
    block = indexer.context_pack(index, ["app/util.py"], budget_chars=500)
    assert "app/util.py" in block and "parse" in block
    assert len(indexer.context_pack(index, ["app/util.py"], budget_chars=10)) <= 10


async def test_index_files_is_incremental_and_prunes(db):
    from buglens import store

    await store.init(db)
    report = await indexer.index_files("o/r", {"a.py": PY_UTIL, "b.py": PY_MAIN}, db)
    assert report["added"] == 2 and report["skipped"] == 0

    report2 = await indexer.index_files("o/r", {"a.py": PY_UTIL, "b.py": PY_MAIN}, db)
    assert report2["skipped"] == 2 and report2["updated"] == 0

    report3 = await indexer.index_files("o/r", {"a.py": PY_UTIL + "\ndef extra():\n    return 2\n"}, db,
                                        prune=True)
    assert report3["updated"] == 1 and report3["pruned"] == 1

    index = await store.get_index("o/r", db)
    assert set(index) == {"a.py"}
    assert index["a.py"]["sha"]


# ── equivalence: the fast graph must resolve imports exactly like the old brute force ──
def _bruteforce_graph(index: dict[str, dict]) -> dict[str, set[str]]:
    """The original O(paths x imports x modules x paths) resolution, kept as a spec."""
    modules = {indexer.module_name(p) for p in index}
    graph: dict[str, set[str]] = {p: set() for p in index}
    for path, rec in index.items():
        for mod in {i.get("module", "") for i in rec.get("imports", []) if i.get("module")}:
            base = mod.split(".")[0]
            for cand in modules:
                if mod == cand or cand.endswith("." + mod) or (
                    cand.split(".")[0] == base and "." not in mod
                ):
                    for other in index:
                        if indexer.module_name(other) == cand:
                            graph[path].add(other)
    return graph


def test_build_graph_matches_bruteforce():
    files = {
        "app/__init__.py": "",
        "app/util.py": "def parse(v):\n    return v\n",
        "app/deep/helper.py": "def assist():\n    return 1\n",
        "app/main.py": "import app.util\nfrom app.deep import helper\nfrom util import parse\nimport json\n",
        "app/other.py": "from app.deep.helper import assist\nfrom app.util import parse\n",
        "libs/thing.py": "from app import util\n",
        "loose.py": "import helper\nimport app.deep.helper\n",
    }
    index = {p: indexer.parse_text(p, t) for p, t in files.items()}
    assert indexer.build_graph(index) == _bruteforce_graph(index)


def test_build_graph_has_no_self_edges():
    index = {p: indexer.parse_text(p, t) for p, t in {
        "a.py": "import a\n", "b.py": "from a import thing\n"}.items()}
    graph = indexer.build_graph(index)
    assert "a.py" not in graph["a.py"]     # a module importing itself is not an edge
    assert graph["b.py"] == {"a.py"}       # a real import still resolves

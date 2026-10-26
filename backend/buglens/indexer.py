"""Persistent, incremental code graph (phase B of CONTEXT_PLAN.md).

Greptile/CodeRabbit build a repo-wide graph of files, functions/classes, imports and
references, then pull callers, tests and neighbours into a review. We do the same with
the standard library only, no tree-sitter, no vector DB, no external indexer:

* ``parse_text(path, text)``, one file → ``{defs, imports, refs, doc, tests}``.
  Python goes through ``ast`` (accurate); everything else through a conservative regex
  fallback that still finds defs/classes/imports in JS/TS/Go/Rust/Java-likes.
* ``index_files(repo, files, db)``, incremental: a file is re-parsed only when its
  content hash changed; stale rows are pruned.
* ``related_files(repo, changed, db)``, the retrieval step: import-graph neighbours
  (both directions), files that reference the changed symbols, and their tests, ranked by
  a cheap personalized-PageRank over the import graph (Aider's trick, without embeddings).

The index is a *cache*, never a source of truth: any failure just means we fall back to
the tree/snippet context the pipeline already uses.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re

from . import store

MAX_DEFS = 200
MAX_REFS = 400
MAX_DOC = 400

TEST_PATTERNS = (
    re.compile(r"(^|/)(tests?|__tests__|spec)/", re.I),
    re.compile(r"(^|/)test_[^/]+\.py$", re.I),
    re.compile(r"\.(test|spec)\.[a-z0-9]+$", re.I),
    re.compile(r"_test\.go$", re.I),
)

_JS_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|enum|const|let|var|struct|trait|impl|fn|func)\s+"
    r"([A-Za-z_$][\w$]*)",
    re.M,
)
_JS_IMPORT = re.compile(
    r"^\s*(?:import\s+(?:[^'\"]*?\s+from\s+)?['\"]([^'\"]+)['\"]"
    r"|from\s+([\w.]+)\s+import\s+([^#\n]+)"
    r"|(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|using\s+([\w.]+))",
    re.M,
)
_GO_IMPORT = re.compile(r'^\s*(?:import\s+)?[_a-z]*\s*"([^"]+)"', re.M)

_LANGS_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".rb": "ruby",
    ".php": "php", ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".swift": "swift", ".scala": "scala", ".sh": "shell",
}


def language_for(path: str) -> str:
    return _LANGS_BY_EXT.get(os.path.splitext(path)[1].lower(), "")


def is_test_file(path: str) -> bool:
    return any(rx.search(path) for rx in TEST_PATTERNS)


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def module_name(path: str) -> str:
    """Best-effort dotted module for a path (used to match imports to files)."""
    p = re.sub(r"\.(py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|kt|rb|php|cs|swift)$", "", path)
    p = re.sub(r"/index$", "", p)
    return p.replace("/", ".").lstrip(".")


# ── parsing ───────────────────────────────────────────────────────────────────
def _parse_python(path: str, text: str) -> dict:
    defs: list[dict] = []
    imports: list[dict] = []
    refs: list[dict] = []
    doc = ""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _parse_regex(path, text)
    doc = (ast.get_docstring(tree) or "")[:MAX_DOC]

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.append({
                    "name": (prefix + child.name), "kind": "def", "line": child.lineno,
                    "sig": f"{child.name}({', '.join(a.arg for a in child.args.args)})",
                    "doc": (ast.get_docstring(child) or "")[:200],
                })
            elif isinstance(child, ast.ClassDef):
                defs.append({
                    "name": (prefix + child.name), "kind": "class", "line": child.lineno,
                    "sig": f"class {child.name}", "doc": (ast.get_docstring(child) or "")[:200],
                })
                walk(child, prefix + child.name + ".")
                continue
            elif isinstance(child, ast.Import):
                for a in child.names:
                    imports.append({"module": a.name, "names": [a.asname or a.name.split(".")[-1]],
                                    "line": child.lineno})
            elif isinstance(child, ast.ImportFrom):
                imports.append({"module": child.module or "", "names": [a.name for a in child.names],
                                "line": child.lineno, "level": child.level})
            if isinstance(child, (ast.Call,)):
                fn = child.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name:
                    refs.append({"name": name, "line": getattr(fn, "lineno", child.lineno)})
            walk(child)
    walk(tree)
    if len(defs) > MAX_DEFS:
        defs = defs[:MAX_DEFS]
    return {"path": path, "lang": "python", "doc": doc, "defs": defs,
            "imports": imports[:120], "refs": refs[:MAX_REFS],
            "tests": is_test_file(path)}


def _parse_regex(path: str, text: str) -> dict:
    lang = language_for(path)
    defs, imports, refs = [], [], []
    for m in _JS_DEF.finditer(text):
        defs.append({"name": m.group(1), "kind": "def", "line": text.count("\n", 0, m.start()) + 1,
                     "sig": m.group(1), "doc": ""})
    for m in _JS_IMPORT.finditer(text):
        mod = next((g for g in m.groups() if g), "")
        imports.append({"module": mod.strip(), "names": [], "line": text.count("\n", 0, m.start()) + 1})
    if lang == "go":
        for m in _GO_IMPORT.finditer(text):
            imports.append({"module": m.group(1), "names": [], "line": 1})
    for m in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*\(", text):
        refs.append({"name": m.group(1), "line": text.count("\n", 0, m.start()) + 1})
    doc = ""
    first = re.search(r'^\s*(?:"""|\'\'\'|/\*\*|//)\s*(.+)$', text, re.M)
    if first:
        doc = first.group(1).strip()[:MAX_DOC]
    return {"path": path, "lang": lang, "doc": doc, "defs": defs[:MAX_DEFS],
            "imports": imports[:120], "refs": refs[:MAX_REFS], "tests": is_test_file(path)}


def parse_text(path: str, text: str) -> dict:
    if language_for(path) == "python":
        return _parse_python(path, text)
    return _parse_regex(path, text)


# ── indexing ──────────────────────────────────────────────────────────────────
async def index_files(repo: str, files: dict[str, str], db: str | None = None,
                      prune: bool = False) -> dict:
    """Parse and store only the files whose content changed. Returns a small report."""
    known = await store.get_index_state(repo, db)
    added = updated = skipped = 0
    pending: list[dict] = []
    for path, text in files.items():
        sha = content_sha(text)
        if known.get(path) == sha:
            skipped += 1
            continue
        record = parse_text(path, text)
        record["sha"] = sha
        pending.append(record)
        if path in known:
            updated += 1
        else:
            added += 1
    if pending:                                   # single transaction, not one per file
        await store.put_index_many(repo, pending, db)
    pruned = await store.prune_index(repo, set(files), db) if prune else 0
    return {"added": added, "updated": updated, "skipped": skipped, "pruned": pruned,
            "size": len(files)}


# ── retrieval ─────────────────────────────────────────────────────────────────
def _maybe_module(path: str) -> str:
    return module_name(path)


def _imports_from(record: dict) -> set[str]:
    return {i.get("module", "") for i in record.get("imports", []) if i.get("module")}


def _module_maps(index: dict[str, dict]) -> tuple[dict, dict, dict]:
    """Build module lookups once instead of rescanning every module for every import.

    The old ``build_graph`` was O(paths x imports x modules x paths), 9.4s on a 2000-file
    repo, on every review. These three maps make resolution a dict lookup:
      exact  : "a.b.c"     -> paths
      suffix : "b.c"/"c"   -> paths   (covers ``cand.endswith("." + mod)``)
      top    : "a"         -> paths   (covers a bare top-level import)
    """
    exact: dict[str, set[str]] = {}
    suffix: dict[str, set[str]] = {}
    top: dict[str, set[str]] = {}
    for path in index:
        mod = _maybe_module(path)
        if not mod:
            continue
        exact.setdefault(mod, set()).add(path)
        parts = mod.split(".")
        for k in range(len(parts)):
            suffix.setdefault(".".join(parts[k:]), set()).add(path)
        top.setdefault(parts[0], set()).add(path)
    return exact, suffix, top


def build_graph(index: dict[str, dict]) -> dict[str, set[str]]:
    """path -> set of in-repo paths it imports (resolved by exact / suffix / top-level match)."""
    exact, suffix, top = _module_maps(index)
    graph: dict[str, set[str]] = {p: set() for p in index}
    for path, rec in index.items():
        edges = graph[path]
        for mod in _imports_from(rec):
            edges.update(exact.get(mod, set()))
            edges.update(suffix.get(mod, set()))
            if "." not in mod:
                edges.update(top.get(mod, set()))
        edges.discard(path)                       # a module importing itself is not an edge
    return graph


def reverse_graph(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    """Invert once (``reverse_deps`` per file was O(paths) each time)."""
    rev: dict[str, set[str]] = {p: set() for p in graph}
    for src, deps in graph.items():
        for dst in deps:
            rev.setdefault(dst, set()).add(src)
    return rev


def reverse_deps(graph: dict[str, set[str]], target: str) -> set[str]:
    return {src for src, deps in graph.items() if target in deps}


def _tests_index(index: dict[str, dict]) -> tuple[list[str], dict[str, set[str]], dict[str, set[str]]]:
    """One pass over the test files: what they import and which symbols they reference."""
    tests = [p for p, r in index.items() if r.get("tests")]
    by_import: dict[str, set[str]] = {}
    by_ref: dict[str, set[str]] = {}
    for p in tests:
        for imp in _imports_from(index[p]):
            by_import.setdefault(imp, set()).add(p)
        for r in index[p].get("refs", []):
            if r.get("name"):
                by_ref.setdefault(r["name"], set()).add(p)
    return tests, by_import, by_ref


def tests_for(index: dict[str, dict], target: str,
              pre: tuple[list[str], dict[str, set[str]], dict[str, set[str]]] | None = None) -> set[str]:
    """Test files that import the target or reference one of its symbols by name."""
    rec = index.get(target) or {}
    names = {d["name"].split(".")[-1] for d in rec.get("defs", []) if d.get("name")}
    tests, by_import, by_ref = pre or _tests_index(index)
    mod = _maybe_module(target)
    hits: set[str] = set(by_import.get(mod, set()))
    for imp, paths in by_import.items():
        if imp and mod.endswith(imp):
            hits.update(paths)
    for name in names:
        hits.update(by_ref.get(name, set()))
    return hits


def pagerank(graph: dict[str, set[str]], seeds: set[str], iterations: int = 12,
             damping: float = 0.85, limit: int = 10) -> list[tuple[str, float]]:
    """Personalized PageRank over the import graph, seeded on the changed files."""
    nodes = list(graph)
    if not nodes:
        return []
    rank = {n: 0.0 for n in nodes}
    for s in seeds:
        if s in rank:
            rank[s] = 1.0 / max(len(seeds), 1)
    # inbound edges (who imports whom) give "importance": a widely-imported file matters.
    inbound: dict[str, list[str]] = {n: [] for n in nodes}
    for src, deps in graph.items():
        for dst in deps:
            if dst in inbound:
                inbound[dst].append(src)
    for _ in range(iterations):
        nxt = {n: (1.0 - damping) / len(nodes) for n in nodes}
        for n in nodes:
            if not inbound[n]:
                nxt[n] += damping * rank[n] / len(nodes)
                continue
            share = damping * rank[n] / len(inbound[n])
            for src in inbound[n]:
                nxt[src] += share
        for s in seeds:                       # keep mass on the seeds (personalization)
            if s in nxt:
                nxt[s] += damping * (1.0 / max(len(seeds), 1)) * 0.3
        rank = nxt
    ranked = sorted(((n, v) for n, v in rank.items() if n not in seeds),
                    key=lambda kv: kv[1], reverse=True)
    return ranked[:limit]


def _ref_index(index: dict[str, dict]) -> dict[str, set[str]]:
    """symbol name -> files that reference it (built once, not per file)."""
    out: dict[str, set[str]] = {}
    for p, rec in index.items():
        for r in rec.get("refs", []):
            if r.get("name"):
                out.setdefault(r["name"], set()).add(p)
    return out


def related_files(index: dict[str, dict], graph: dict[str, set[str]], changed: list[str],
                  limit: int = 8) -> list[str]:
    """Ordered related paths: direct deps, reverse deps, referencing files, tests, PageRank."""
    changed_set = set(changed)
    ordered: list[str] = []
    seen: set[str] = set()

    def push(p: str) -> None:
        if p and p not in changed_set and p not in seen and p in index:
            seen.add(p)
            ordered.append(p)

    rev = reverse_graph(graph)
    tests_pre = _tests_index(index)
    refs = None

    for c in changed:
        for dep in sorted(graph.get(c, ())):
            push(dep)
    for c in changed:
        for src in sorted(rev.get(c, ())):
            push(src)
    for c in changed:
        for t in sorted(tests_for(index, c, tests_pre)):
            push(t)

    changed_names = set()
    for c in changed:
        for d in (index.get(c) or {}).get("defs", []):
            if d.get("name"):
                changed_names.add(d["name"].split(".")[-1])
    if changed_names:
        refs = _ref_index(index)
        for name in sorted(changed_names):
            for p in sorted(refs.get(name, ())):
                push(p)

    for p, _score in pagerank(graph, changed_set, limit=limit * 2):
        push(p)
    return ordered[:limit]


def context_pack(index: dict[str, dict], related: list[str], budget_chars: int = 6000) -> str:
    """Compact text block: for each related file, its doc + top defs and why it matters."""
    parts: list[str] = []
    used = 0
    for p in related:
        rec = index.get(p) or {}
        lines = [f"### {p}" + (" (test)" if rec.get("tests") else "")]
        if rec.get("doc"):
            lines.append(rec["doc"])
        for d in rec.get("defs", [])[:12]:
            sig = d.get("sig") or d.get("name", "")
            lines.append(f"- L{d.get('line')}: {sig}")
        block = "\n".join(lines)
        if used + len(block) > budget_chars:
            break
        used += len(block)
        parts.append(block)
    return "\n\n".join(parts)

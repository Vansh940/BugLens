"""Deterministic Change-Stack grouping for the review walkthrough.

A reviewer wants a change told as a stack of layers, bottom-up, the way Graphite's
"stack" reads, not a flat file list. CodeRabbit spends a model call narrating exactly
that; Greptile leaves a flat list. We do it with pure path heuristics: cheap,
deterministic, offline, and easy to test. No model, no network.

Order is dependency-ish (schema -> models -> logic -> api -> ui -> config -> tests ->
docs) so the story reads in the order a reviewer would want to consume it. Chapter
notes are assembled only from facts we have (file counts, new-file counts, findings).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# key -> (title, display order, [regexes]). `logic` is the fallback and matches nothing.
_CHAPTERS: list[tuple[str, str, int, list[str]]] = [
    ("migration", "Schema & migrations", 10,
     [r"migrations?/", r"\bschema\b", r"\.sql$", r"alembic", r"/flyway/"]),
    ("model", "Data models & types", 20,
     [r"models?/", r"entities/", r"\btypes?/", r"schemas?/", r"\bdto\b", r"\.proto$"]),
    ("logic", "Core logic", 30, []),
    ("api", "API & routes", 40,
     [r"routers?/", r"(^|/)api/", r"controllers?/", r"handlers?/", r"endpoints?/",
      r"views?/", r"serializers?/", r"resolvers?/", r"\.controller\.", r"\.service\."]),
    ("ui", "UI", 50,
     [r"components?/", r"pages?/", r"frontend/", r"(^|/)web/", r"\.tsx$", r"\.jsx$",
      r"\.vue$", r"\.svelte$", r"\.css$", r"\.scss$", r"\.html$"]),
    ("config", "Config & infra", 60,
     [r"\.ya?ml$", r"\.toml$", r"\.ini$", r"\.cfg$", r"\.env", r"dockerfile", r"makefile",
      r"\.github/", r"\.tf$", r"\.lock$", r"\.json$"]),
    ("tests", "Tests", 70,
     [r"(^|/)tests?/", r"__tests__/", r"(^|/)spec/", r"(^|/)test_[^/]+$",
      r"[^/]+_test\.[a-z]+$", r"\.test\.[a-z]+$", r"\.spec\.[a-z]+$"]),
    ("docs", "Docs", 80,
     [r"\.mdx?$", r"\.rst$", r"\.txt$", r"(^|/)docs?/", r"readme", r"changelog", r"license"]),
]

_TITLE = {k: t for k, t, _, _ in _CHAPTERS}
_ORDER = {k: o for k, _, o, _ in _CHAPTERS}
_RX = {k: [re.compile(p) for p in pats] for k, _, _, pats in _CHAPTERS}

# Tests and docs first so "tests/test_models.py" is a test, not a model; then the
# dependency order; `logic` is the default.
_PRECEDENCE = ["tests", "docs", "migration", "model", "api", "ui", "config"]


def classify(path: str) -> str:
    """Which chapter a path belongs to. Deterministic, first-match-wins."""
    p = (path or "").replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    for key in _PRECEDENCE:
        for rx in _RX[key]:
            if rx.search(p) or rx.search(base):
                return key
    return "logic"


def is_test_path(path: str) -> bool:
    return classify(path) == "tests"


def is_doc_path(path: str) -> bool:
    return classify(path) == "docs"


@dataclass
class Chapter:
    key: str
    title: str
    order: int
    files: list[str] = field(default_factory=list)
    added: int = 0
    new: int = 0
    findings: list[dict] = field(default_factory=list)


def _note(ch: Chapter) -> str:
    n = len(ch.files)
    bit = f"{n} file" + ("" if n == 1 else "s")
    if ch.new and ch.new == n:
        bit += ", all new"
    elif ch.new:
        bit += f", {ch.new} new"
    if ch.findings:
        bit += f" · {len(ch.findings)} finding(s)"
        top = "; ".join(
            str(f.get("title") or "").strip()[:60]
            for f in ch.findings[:2]
            if str(f.get("title") or "").strip()
        )
        if top:
            bit += f": {top}"
    return bit


def build_change_stack(files, findings) -> list[dict]:
    """Group changed files into ordered chapters, each carrying its findings.

    ``files`` items may be FileDiff objects (``.path`` / ``.added_chars`` / ``.status``)
    or plain dicts. Returns a list of ``{key,title,files,added,new,findings,note}``.
    """
    chapters: dict[str, Chapter] = {}
    for fd in files or []:
        if isinstance(fd, dict):
            path = fd.get("path") or fd.get("filename") or ""
            added_chars = fd.get("added_chars", 0)
            status = fd.get("status", "")
        else:
            path = getattr(fd, "path", "") or ""
            added_chars = getattr(fd, "added_chars", 0) or 0
            status = getattr(fd, "status", "") or ""
        if not path:
            continue
        key = classify(path)
        ch = chapters.setdefault(key, Chapter(key=key, title=_TITLE[key], order=_ORDER[key]))
        ch.files.append(path)
        ch.added += int(added_chars or 0)
        if str(status).lower() in ("added", "new"):
            ch.new += 1

    by_path: dict[str, list[dict]] = {}
    for f in findings or []:
        p = f.get("path") if isinstance(f, dict) else None
        if p:
            by_path.setdefault(p, []).append(f)

    out: list[dict] = []
    for ch in sorted(chapters.values(), key=lambda c: (c.order, c.title)):
        for p in ch.files:
            ch.findings.extend(by_path.get(p, []))
        out.append({
            "key": ch.key,
            "title": ch.title,
            "files": ch.files,
            "added": ch.added,
            "new": ch.new,
            "findings": ch.findings,
            "note": _note(ch),
        })
    return out

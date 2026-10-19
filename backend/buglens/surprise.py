"""Deterministic surprise detector: review by surprise, not by line count.

Pure module: a PR kind and its changed files in, a list of plain-language surprises out. No model,
no network, no I/O. The signal it looks for is the highest-value human review move: a change that
does not belong to the kind of work the PR claims to be ("why did a UI change touch auth?"). It
only needs the title and the file list, so a repo needs no labels or extra service to use it.
"""
from __future__ import annotations

import re

from .diff import FileDiff
from .premortem import RISKY_TOKENS, tokens
from .walkthrough import classify

KINDS = ("fix", "feature", "refactor", "docs", "test", "chore", "unknown")

_PREFIX = {
    "fix": "fix", "fixes": "fix", "fixed": "fix", "bugfix": "fix", "hotfix": "fix", "repair": "fix",
    "feat": "feature", "feature": "feature", "add": "feature", "adds": "feature", "implement": "feature",
    "introduce": "feature", "support": "feature",
    "refactor": "refactor", "cleanup": "refactor", "clean": "refactor", "rename": "refactor",
    "move": "refactor", "simplify": "refactor", "restructure": "refactor",
    "docs": "docs", "doc": "docs", "documentation": "docs", "readme": "docs",
    "test": "test", "tests": "test", "spec": "test",
    "chore": "chore", "deps": "chore", "bump": "chore", "build": "chore", "ci": "chore",
    "release": "chore", "version": "chore", "lint": "chore",
}

DEPENDENCY_FILES = frozenset({
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "requirements.txt",
    "requirements-dev.txt", "pyproject.toml", "poetry.lock", "pipfile", "pipfile.lock",
    "go.mod", "go.sum", "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock", "composer.json",
    "pom.xml", "build.gradle", "build.gradle.kts", "mix.exs", "pubspec.yaml",
})

_DEP_KINDS = frozenset({"fix", "refactor", "docs", "test"})


def pr_kind(title: str) -> str:
    """Classify the PR from its title. A conventional-commit prefix wins, else the first word."""
    text = (title or "").strip().lower()
    if not text:
        return "unknown"
    head = re.split(r"[:(]", text, 1)[0].strip()
    for word in re.split(r"[^a-z]+", head):
        if word in _PREFIX:
            return _PREFIX[word]
    first = re.split(r"[^a-z]+", text, 1)[0]
    return _PREFIX.get(first, "unknown")


def detect(kind: str, fds: list[FileDiff]) -> list[str]:
    """Plain-language surprises, in a stable order, deduped. Empty when nothing is surprising."""
    out: list[str] = []
    chapters = {classify(fd.path) for fd in fds}
    code = [fd for fd in fds if classify(fd.path) not in ("docs", "tests")]
    migration = next((fd.path for fd in fds if classify(fd.path) == "migration"), None)
    risky_candidates = [fd.path for fd in fds if tokens(fd.path) & RISKY_TOKENS]
    risky = next((p for p in risky_candidates if classify(p) != "ui"), None) or (
        risky_candidates[0] if risky_candidates else None)
    dep = next((fd for fd in fds
                if fd.path.rsplit("/", 1)[-1].lower() in DEPENDENCY_FILES and fd.status == "added"),
               None)

    if kind == "docs" and code:
        out.append(f"docs PR changes code: {code[0].path}")
    if kind == "test" and code:
        out.append(f"tests-only PR changes product code: {code[0].path}")
    if kind == "refactor" and migration:
        out.append(f"refactor touches a schema migration: {migration}")
    if kind == "fix" and migration:
        out.append(f"bug fix touches a schema migration: {migration}")
    if "ui" in chapters and risky:
        out.append(f"UI change touches an auth/security path: {risky}")
    if dep is not None and kind in _DEP_KINDS:
        out.append(f"{kind} PR adds a dependency: {dep.path}")
    return list(dict.fromkeys(out))

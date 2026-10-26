"""Cheap codebase index.

Greptile indexes the repo into a graph of files/functions/dependencies before it
reviews a diff; Bugbot reasons about how a change interacts with "assumptions
made elsewhere". Full graph indexing is out of scope for a single-process bot,
so this module builds the useful 20%: the repo tree, the repo's own convention
files, an import graph over the changed files, and a symbol table of the files
those imports reach. It is regex-based and language-tolerant, never executes
repo code, and is bounded by a character budget.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

IMPORT_PATTERNS = {
    "python": [re.compile(r"^\s*from\s+([A-Za-z0-9_\.]+)\s+import\b", re.M),
               re.compile(r"^\s*import\s+([A-Za-z0-9_\.]+)", re.M)],
    "javascript": [re.compile(r"""^\s*import\s+[^'"]*from\s+['"]([^'"]+)['"]""", re.M),
                   re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", re.M)],
    "typescript": [re.compile(r"""^\s*import\s+[^'"]*from\s+['"]([^'"]+)['"]""", re.M),
                   re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", re.M)],
    "go": [re.compile(r"""^\s*(?:[\w\.]+\s+)?"([^"]+)"\s*$""", re.M)],
    "java": [re.compile(r"^\s*import\s+([\w\.]+);", re.M)],
    "rust": [re.compile(r"^\s*use\s+([\w:]+)", re.M)],
    "ruby": [re.compile(r"^\s*require(?:_relative)?\s+['\"]([^'\"]+)['\"]", re.M)],
}
SYMBOL_PATTERNS = {
    "python": re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)", re.M),
    "javascript": re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|class\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\()", re.M),
    "typescript": re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|class\s+(\w+)|const\s+(\w+)\s*[:=])", re.M),
    "go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", re.M),
    "java": re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\(", re.M),
    "rust": re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)", re.M),
}
CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".buglens.yml", ".buglens.yaml",
                    ".github/buglens.yml", "docs/STYLE.md", "STYLE.md",
                    ".cursorrules", ".github/copilot-instructions.md")


def language_of(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "python", "pyi": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript",
        "ts": "typescript", "tsx": "typescript", "jsx": "javascript", "java": "java",
        "go": "go", "rs": "rust", "rb": "ruby", "php": "php", "cs": "csharp", "kt": "kotlin",
        "swift": "swift", "c": "c", "h": "c", "cpp": "cpp", "cc": "cpp", "hpp": "cpp",
        "sh": "bash", "yml": "yaml", "yaml": "yaml", "json": "json", "sql": "sql",
    }.get(ext, "plaintext")


def extract_symbols(text: str, language: str, limit: int = 40) -> list[str]:
    pat = SYMBOL_PATTERNS.get(language)
    if not pat:
        return []
    names: list[str] = []
    for m in pat.finditer(text):
        name = next((g for g in m.groups() if g), None)
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def extract_imports(text: str, language: str, limit: int = 25) -> list[str]:
    found: list[str] = []
    for pat in IMPORT_PATTERNS.get(language, []):
        for m in pat.finditer(text):
            spec = m.group(1)
            if spec and spec not in found:
                found.append(spec)
        if len(found) >= limit:
            break
    return found[:limit]


def resolve_import(spec: str, importer: str, tree: list[str], language: str) -> str | None:
    """Map an import specifier onto a path that exists in the repo tree."""
    spec = spec.strip()
    if not spec or spec.startswith(".") is False and "/" not in spec and language in ("python", "java", "go"):
        # absolute module names: python/java packages, go module paths
        tail = spec.replace(".", "/").split("/")[-1] if "." in spec else spec.split("/")[-1]
        cands = [t for t in tree if t.endswith(f"/{tail}.py") or t == f"{tail}.py" or t.endswith(f"{tail}.java")
                 or t.endswith(f"/{tail}.go") or t.endswith(f"{tail}/index.ts") or t.endswith(f"/{tail}.ts")]
        return cands[0] if cands else None
    base = os.path.dirname(importer)
    if spec.startswith("."):
        rel = os.path.normpath(os.path.join(base, spec))
        for ext in ("", ".ts", ".tsx", ".js", ".jsx", ".py", "/index.ts", "/index.js", ".rs", ".go"):
            cand = (rel + ext).replace("\\", "/").lstrip("./")
            if cand in tree:
                return cand
        return None
    tail = spec.split("/")[-1]
    for t in tree:
        if t.endswith(f"/{spec}.ts") or t.endswith(f"/{spec}.tsx") or t.endswith(f"/{spec}.js") or t.endswith(f"/{spec}.py"):
            return t
        if "/" not in spec and t.endswith(f"/{tail}.py"):
            return t
    return None


@dataclass
class RepoContext:
    """Everything the reviewer is allowed to know about the repo."""
    tree: list[str] = field(default_factory=list)
    conventions: dict[str, str] = field(default_factory=dict)
    snippets: dict[str, str] = field(default_factory=dict)   # path -> excerpt (related files)
    symbols: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def prompt_block(self, limit: int = 6000) -> str:
        """Compact text block handed to the model with every file review."""
        parts: list[str] = []
        if self.conventions:
            parts.append("## Repo conventions (authoritative, from the repository itself)")
            for name, text in list(self.conventions.items())[:4]:
                parts.append(f"### {name}\n{text[:1200]}")
        if self.snippets:
            parts.append("## Related files in this repo (for cross-file reasoning)")
            for path, text in list(self.snippets.items())[:8]:
                parts.append(f"### {path}\n```\n{text[:1200]}\n```")
        if self.symbols:
            flat = []
            for path, names in list(self.symbols.items())[:10]:
                if names:
                    flat.append(f"- `{path}`: {', '.join(names[:12])}")
            if flat:
                parts.append("## Symbols defined in related files\n" + "\n".join(flat))
        block = "\n\n".join(parts)
        return block[:limit]


def build_context(tree: list[str], file_texts: dict[str, str], head_sha: str) -> RepoContext:
    """Index the changed files' imports and pull the reachable local files' symbols."""
    ctx = RepoContext(tree=tree)
    ctx.notes.append(f"indexed at {head_sha[:8]}, {len(tree)} files in tree")
    for path in CONVENTION_FILES:
        if path in file_texts:
            ctx.conventions[path] = file_texts[path][:4000]
    for path, text in file_texts.items():
        if path in CONVENTION_FILES or path not in tree:
            continue
        lang = language_of(path)
        for spec in extract_imports(text, lang):
            target = resolve_import(spec, path, tree, lang)
            if not target or target == path or target in ctx.snippets:
                continue
            ctx.snippets[target] = ""      # placeholder: caller fills bodies on demand
    return ctx


def needs_file_body(path: str) -> bool:
    return True

"""Symbol/contract graph and ripple analysis over the code index.

``indexer.py`` answers "which files relate to this change". This module answers the next
question: "which *symbols* changed, and who calls them". It is pure and deterministic over
the index a review already loaded: no model, no network, no extra service.

Two outputs:

* ``ripple(...)`` -> for each definition added or edited by the PR, the in-repo files that
  reference it by name (from the same refs the indexer already collected). This becomes a
  bounded context block (``ripple_block``) so a lens knows a changed function has callers.
* ``ripple_findings(...)`` -> the one case that is a genuine cross-file inconsistency rather
  than a hint: a changed definition that now takes a **required** argument (no default) while
  callers still exist. It is emitted as a normal candidate so the verify gate still has to
  keep it; nothing here bypasses the machine or model gate.
"""
from __future__ import annotations

import re

from .diff import FileDiff, fingerprint

MAX_SYMBOLS = 10
MAX_CALLERS = 12
MAX_FINDINGS = 3
MAX_BLOCK_CHARS = 1400

_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)")
_JS_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"
)
_GO_DEF = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)\)")
_METHOD = re.compile(
    r"^\s*(?:public|private|protected|static|async|export|override|virtual)?\s*"
    r"([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*(?::[^={]*)?\{"
)
# keywords a ``NAME(...){`` line could start with; never a real definition.
_SKIP = {"if", "for", "while", "switch", "catch", "return", "else", "do", "match", "with",
         "function", "new", "typeof", "delete", "await", "yield"}


def _params(raw: str) -> list[str]:
    out: list[str] = []
    for p in (raw or "").split(","):
        p = p.strip()
        if not p or p in ("self", "cls"):
            continue
        out.append(p)
    return out


def _required(params: list[str]) -> list[str]:
    """Params with no default and not rest/spread: adding one of these can break callers."""
    return [p for p in params if "=" not in p and not p.startswith(("...", "*", "**"))]


def changed_defs(picked: list[FileDiff]) -> list[dict]:
    """Signature lines the PR added: [{name, path, line, params, required, raw}]."""
    out: list[dict] = []
    for fd in picked:
        for line in fd.added:
            text = line.text or ""
            for rx in (_PY_DEF, _JS_DEF, _GO_DEF, _METHOD):
                m = rx.match(text)
                if not m:
                    continue
                name = m.group(1)
                if not name or name in _SKIP:
                    break
                params = _params(m.group(2))
                out.append({"name": name, "path": fd.path, "line": line.new_line,
                            "params": params, "required": _required(params),
                            "raw": text.strip()[:160]})
                break
    return out


def _ref_index(index: dict[str, dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path, rec in index.items():
        for r in rec.get("refs", []):
            if r.get("name"):
                out.setdefault(r["name"], set()).add(path)
    return out


def ripple(index: dict[str, dict], changed: list[dict],
           changed_paths: list[str] | None = None) -> list[dict]:
    """For each changed definition, the in-repo files that call it (bounded)."""
    exclude = set(changed_paths or ())
    refs = _ref_index(index)
    out: list[dict] = []
    seen: set[str] = set()
    for d in changed:
        name = d["name"]
        if name in seen:
            continue
        callers = sorted(p for p in refs.get(name, set()) if p not in exclude)
        if not callers:
            continue
        seen.add(name)
        out.append({**d, "callers": callers[:MAX_CALLERS], "caller_count": len(callers)})
        if len(out) >= MAX_SYMBOLS:
            break
    return out


def ripple_block(items: list[dict]) -> str:
    """Compact, deterministic context: changed symbols and who still calls them."""
    if not items:
        return ""
    lines = ["Changed symbols with in-repo callers (check these still line up):"]
    used = len(lines[0])
    for it in items:
        head = it["callers"][:4]
        more = f" (+{it['caller_count'] - len(head)} more)" if it["caller_count"] > len(head) else ""
        sig = ", ".join(it["params"][:6])
        row = (f"- {it['name']}({sig}) at {it['path']}:{it['line']} "
               f"called from {', '.join(head)}{more}")
        if used + len(row) > MAX_BLOCK_CHARS:
            break
        used += len(row)
        lines.append(row)
    return "\n".join(lines)


def ripple_findings(items: list[dict]) -> list[dict]:
    """Cross-file inconsistency candidates: a newly required argument with live callers."""
    out: list[dict] = []
    for it in items:
        if not it.get("required") or not it.get("callers"):
            continue
        arg = it["required"][-1]
        title = f"{it['name']} now requires the argument '{arg}'"
        out.append({
            "path": it["path"],
            "lens": "merge",
            "severity": "major",
            "category": "bug",
            "line": it["line"],
            "title": title,
            "body": (f"{it['name']} gained a required parameter '{arg}' and has "
                     f"{it['caller_count']} caller file(s) in the repo "
                     f"({', '.join(it['callers'][:3])}). Call sites that still pass the old "
                     "argument list will fail."),
            "fix": f"Give '{arg}' a default value, or update the call sites in this PR.",
            "suggestion": None,
            "evidence": it["raw"],
            "confidence": 0.5,
            "deterministic": False,
        })
        out[-1]["fingerprint"] = fingerprint(it["path"], "bug", title)
        if len(out) >= MAX_FINDINGS:
            break
    return out

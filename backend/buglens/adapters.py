"""Deterministic linter/SAST adapters: the gate that is never wrong the way an LLM is.

``secretscan`` proved the pattern: a hit is a fact, not an opinion, so it skips the model
verify gate and posts at high confidence. Adapters extend that same idea to the linters a
project already uses. Run the tool, read its structured output, and merge the results into
the ONE consolidated review at ``confidence: 1.0``.

Design, and why it beats the paid field: the cloud reviewers are LLM-first, so they have no
deterministic baseline. BugLens runs the tool the repo already trusts, offline, for free, and
those findings can never be voted away.

Safety (this module executes subprocesses, so it is deliberately paranoid):

* Only tools named in ``ADAPTERS`` are ever run. Repo config can enable or disable a name and
  nothing more, so a malicious PR cannot turn the bot into a command runner.
* argv is built here, no shell, no shell expansion. ``shlex`` is never used on repo input.
* Every run has a hard timeout and a size cap; failures are skipped and disclosed, never fatal.
* Tools run on the full new-file content we already fetched, so their line numbers are real
  new-file lines. Only findings that land on an **added** line are kept, which is exactly the
  "issues this change introduces" scope the prompts ask the model for.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .diff import FileDiff, fingerprint
from .severity import normalize

log = logging.getLogger("buglens.adapters")

TIMEOUT = int(os.getenv("BUGLENS_LINTER_TIMEOUT", "25"))
MAX_OUTPUT = 2_000_000


@dataclass(frozen=True)
class Rule:
    line: int | None
    severity: str
    code: str
    message: str
    category: str | None = None


@dataclass(frozen=True)
class Adapter:
    name: str
    bin: str
    exts: frozenset[str]
    category: str
    argv: tuple[str, ...]                 # "{file}" is replaced with the temp file path
    parser: str                           # key into PARSERS
    extra_files: dict[str, str] = field(default_factory=dict)


# ── parsers: pure functions over a tool's structured stdout ───────────────────
def _ruff(text: str) -> list[Rule]:
    out: list[Rule] = []
    for item in _json_list(text):
        loc = item.get("location") or {}
        code = str(item.get("code") or "ruff")
        # E9 = syntax errors, F8 = undefined names / real bugs; everything else is style.
        sev = "major" if code[:2] in ("E9", "F8") else "minor"
        out.append(Rule(loc.get("row"), sev, code, str(item.get("message") or ""), "bug" if sev == "major" else "style"))
    return out


def _bandit(text: str) -> list[Rule]:
    out: list[Rule] = []
    data = _json_obj(text)
    for item in data.get("results") or []:
        sev = {"HIGH": "major", "MEDIUM": "minor", "LOW": "nit"}.get(str(item.get("issue_severity") or "").upper(), "minor")
        out.append(Rule(item.get("line_number"), sev, str(item.get("test_id") or "bandit"),
                        str(item.get("issue_text") or ""), "security"))
    return out


def _eslint(text: str) -> list[Rule]:
    out: list[Rule] = []
    for entry in _json_list(text):
        for m in entry.get("messages") or []:
            if m.get("fatal"):
                sev = "major"
            elif int(m.get("severity") or 1) >= 2:
                sev = "minor"
            else:
                sev = "nit"
            code = m.get("ruleId") or "parse-error"
            out.append(Rule(m.get("line"), sev, str(code), str(m.get("message") or ""), "style" if sev != "major" else "bug"))
    return out


def _shellcheck(text: str) -> list[Rule]:
    out: list[Rule] = []
    for item in _json_list(text):
        level = str(item.get("level") or "warning")
        sev = {"error": "major", "warning": "minor", "info": "minor", "style": "nit"}.get(level, "minor")
        out.append(Rule(item.get("line"), sev, f"SC{item.get('code')}", str(item.get("message") or ""), "bug" if sev == "major" else "style"))
    return out


def _hadolint(text: str) -> list[Rule]:
    out: list[Rule] = []
    for item in _json_list(text):
        level = str(item.get("level") or "warning")
        sev = {"error": "major", "warning": "minor", "info": "nit", "style": "nit"}.get(level, "minor")
        out.append(Rule(item.get("line"), sev, str(item.get("code") or "hadolint"), str(item.get("message") or ""), "security" if sev == "major" else "style"))
    return out


def _rubocop(text: str) -> list[Rule]:
    out: list[Rule] = []
    data = _json_obj(text)
    for f in data.get("files") or []:
        for o in f.get("offenses") or []:
            sev = {"error": "major", "fatal": "major", "warning": "minor", "convention": "nit",
                   "refactor": "nit", "info": "nit"}.get(str(o.get("severity") or "").lower(), "minor")
            loc = o.get("location") or {}
            out.append(Rule(loc.get("line"), sev, str(o.get("cop_name") or "rubocop"), str(o.get("message") or ""), "style"))
    return out


def _generic_parsable(text: str) -> list[Rule]:
    """yamllint / flake8 style: `path:line:col: LEVEL message` or `path:line: LEVEL message`."""
    out: list[Rule] = []
    for raw in text.splitlines():
        parts = raw.split(":", 3)
        if len(parts) < 4:
            continue
        try:
            line = int(parts[1])
        except ValueError:
            continue
        rest = parts[3].strip()
        level, _, msg = rest.partition(" ")
        sev = {"error": "major", "warning": "minor", "info": "nit", "style": "nit", "convention": "nit"}.get(level.lower(), "minor")
        out.append(Rule(line, sev, level or "lint", msg.strip() or rest, "style"))
    return out


def _syntax_only(text: str) -> list[Rule]:
    """Runtimes that only report a syntax error on stderr (`node --check`, `php -l`)."""
    out: list[Rule] = []
    for raw in text.splitlines():
        low = raw.lower()
        if "parse error" in low or "syntaxerror" in low or "syntax error" in low:
            out.append(Rule(_first_int(raw), "major", "syntax", raw.strip()[:200], "bug"))
            break
    return out


PARSERS = {
    "ruff": _ruff,
    "bandit": _bandit,
    "eslint": _eslint,
    "shellcheck": _shellcheck,
    "hadolint": _hadolint,
    "rubocop": _rubocop,
    "parsable": _generic_parsable,
    "syntax": _syntax_only,
}


def _json_list(text: str) -> list:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _json_obj(text: str) -> dict:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_int(text: str) -> int | None:
    import re as _re
    m = _re.search(r"\d+", text)
    return int(m.group()) if m else None


# A minimal flat eslint config so the adapter works without the project's node_modules.
_ESLINT_CONFIG = """export default [{
  languageOptions: { ecmaVersion: 2022, sourceType: "module" },
  rules: {
    "no-eval": "error", "no-implied-eval": "error", "no-new-func": "error",
    "no-async-promise-executor": "error", "no-dupe-keys": "error", "no-dupe-args": "error",
    "no-unreachable": "warn", "eqeqeq": "warn", "no-self-compare": "warn",
    "no-constant-condition": "warn", "use-isnan": "warn"
  }
}];
"""


# ── registry ─────────────────────────────────────────────────────────────────
ADAPTERS: list[Adapter] = [
    Adapter("ruff", "ruff", frozenset({"py", "pyi"}), "style",
            ("ruff", "check", "--isolated", "--no-cache", "--output-format=json", "{file}"), "ruff"),
    Adapter("bandit", "bandit", frozenset({"py"}), "security",
            ("bandit", "-q", "-f", "json", "{file}"), "bandit"),
    Adapter("eslint", "eslint", frozenset({"js", "mjs", "cjs", "jsx"}), "style",
            ("eslint", "--no-config-lookup", "--config", "eslint.config.mjs", "--format", "json", "{file}"), "eslint",
            extra_files={"eslint.config.mjs": _ESLINT_CONFIG}),
    Adapter("node-syntax", "node", frozenset({"js", "mjs", "cjs"}), "bug",
            ("node", "--check", "{file}"), "syntax"),
    Adapter("shellcheck", "shellcheck", frozenset({"sh", "bash"}), "bug",
            ("shellcheck", "-f", "json", "{file}"), "shellcheck"),
    Adapter("hadolint", "hadolint", frozenset({"dockerfile"}), "security",
            ("hadolint", "-f", "json", "{file}"), "hadolint"),
    Adapter("rubocop", "rubocop", frozenset({"rb"}), "style",
            ("rubocop", "--format", "json", "--force-exclusion", "{file}"), "rubocop"),
    Adapter("yamllint", "yamllint", frozenset({"yml", "yaml"}), "style",
            ("yamllint", "-f", "parsable", "{file}"), "parsable"),
    Adapter("php-syntax", "php", frozenset({"php"}), "bug",
            ("php", "-l", "{file}"), "syntax"),
]
BY_NAME = {a.name: a for a in ADAPTERS}


def _ext(path: str) -> str:
    low = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if low == "dockerfile" or low.startswith("dockerfile."):
        return "dockerfile"
    return low.rsplit(".", 1)[-1] if "." in low else ""


def _installed(adapter: Adapter) -> bool:
    return shutil.which(adapter.bin) is not None


def enabled_adapters(linters: list[str] | None) -> list[Adapter]:
    """Resolve ``rules.linters`` to adapters that are both requested and installed.

    ``[]``/``off``/None means off. ``["auto"]`` means every supported adapter the host has.
    An explicit list means exactly those (installed ones only, the rest are disclosed).
    """
    names = [str(n).strip().lower() for n in (linters or []) if str(n).strip()]
    if not names or names == ["off"]:
        return []
    if "auto" in names:
        pool = list(ADAPTERS)
    else:
        pool = [BY_NAME[n] for n in names if n in BY_NAME]
    return [a for a in pool if _installed(a)]


def _run(adapter: Adapter, content: str, filename: str) -> str:
    """Run one adapter over one file's full content; return combined stdout+stderr."""
    workdir = tempfile.mkdtemp(prefix="buglens-lint-")
    try:
        name = Path(filename).name or "input"
        target = Path(workdir) / name
        target.write_text(content, encoding="utf-8", errors="replace")
        for extra, body in adapter.extra_files.items():
            (Path(workdir) / extra).write_text(body, encoding="utf-8")
        argv = [str(target) if tok == "{file}" else tok for tok in adapter.argv]
        proc = subprocess.run(
            argv, cwd=workdir, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT,
        )
        return (proc.stdout or "") + "\n" + (proc.stderr or "")
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.info("adapter %s skipped: %s", adapter.name, exc)
        return ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _to_finding(adapter: Adapter, rule: Rule, fd: FileDiff) -> dict | None:
    if rule.line is None:
        return None
    if rule.line not in fd.added_line_numbers:          # only findings this change introduces
        return None
    text = next((l.text for l in fd.added if l.new_line == rule.line), "")
    title = f"{adapter.name} {rule.code}: {rule.message}".strip()[:160]
    sev_norm = normalize(rule.severity)
    finding = {
        "path": fd.path,
        "lens": "lint",
        "severity": sev_norm,
        "category": rule.category or adapter.category,
        "line": rule.line,
        "title": title,
        "body": rule.message[:600] or f"{adapter.name} reported {rule.code}.",
        "fix": f"Address the {adapter.name} finding ({rule.code}) on this line.",
        "suggestion": None,
        "evidence": text[:300],
        "confidence": 1.0,
        "deterministic": True,
        "linter": adapter.name,
    }
    finding["fingerprint"] = fingerprint(fd.path, finding["category"], f"{adapter.name}:{rule.code}")
    return finding


def scan(files: dict[str, str], fds: dict[str, FileDiff], linters: list[str] | None) -> tuple[list[dict], list[str]]:
    """Run enabled adapters over the changed files. Returns (findings, adapters_run)."""
    adapters = enabled_adapters(linters)
    if not adapters:
        return [], []
    findings: list[dict] = []
    ran: list[str] = []
    for path, fd in fds.items():
        content = files.get(path)
        if not content:
            continue
        ext = _ext(path)
        for adapter in adapters:
            if ext not in adapter.exts:
                continue
            output = _run(adapter, content, path)
            if not output.strip():
                continue
            if adapter.name not in ran:
                ran.append(adapter.name)
            for rule in PARSERS[adapter.parser](output):
                finding = _to_finding(adapter, rule, fd)
                if finding:
                    findings.append(finding)
    return findings, ran

"""Deterministic secret scanner, the one gate that is never wrong the way an LLM is.

A regex hit on an added line is a *fact*, not an opinion, so these findings skip the
model verify gate (see ``verify.verify_all``) and post at ``blocker``. Values are
masked before they ever reach a comment, so the bot never reprints a live credential.
This is a scoped-down version of CodeRabbit's "40+ scanners" layer: one scanner, zero
dependencies, high precision.
"""
from __future__ import annotations

import re

from .diff import FileDiff, fingerprint

# (label, compiled pattern). Ordered by confidence.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe secret key", re.compile(r"\bsk_live_[0-9a-zA-Z]{24,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}\b")),
]

_ASSIGN = re.compile(
    r"""(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*"""
    r"""["']([^"']{12,})["']"""
)
_PLACEHOLDER = re.compile(
    r"(?i)(example|sample|placeholder|changeme|your[_-]|xxx+|test|fake|dummy|redacted|<[^>]+>)"
)


def _masked(text: str, start: int, end: int) -> str:
    secret = text[start:end]
    keep = secret[:4]
    return text[:start] + keep + "*" * max(4, len(secret) - len(keep)) + text[end:]


def scan(fd: FileDiff) -> list[dict]:
    """Return blocker findings for secrets found in this file's added lines."""
    findings: list[dict] = []
    for line in fd.added:
        text = line.text or ""
        hit = None
        for label, pattern in PATTERNS:
            m = pattern.search(text)
            if m:
                hit = (label, m.start(), m.end())
                break
        if hit is None:
            m = _ASSIGN.search(text)
            if m and not _PLACEHOLDER.search(m.group(1)):
                hit = ("hardcoded credential", m.start(1), m.end(1))
        if hit is None:
            continue
        label, start, end = hit
        title = f"Possible {label} committed in code"
        findings.append({
            "path": fd.path,
            "lens": "secrets",
            "severity": "blocker",
            "category": "security",
            "line": line.new_line,
            "title": title,
            "body": (f"Detected what looks like a {label} in this line. Secrets in source are "
                     "harvestable from git history even after removal."),
            "fix": ("Remove the value, load it from an environment variable or secret manager, "
                    "and rotate the exposed credential."),
            "suggestion": None,
            "evidence": _masked(text, start, end),
            "confidence": 1.0,
            "deterministic": True,
        })
        findings[-1]["fingerprint"] = fingerprint(fd.path, "security", title)
    return findings

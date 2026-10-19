"""Deterministic per-PR risk tier (L0-L3) for the human reading the review.

Pure module: findings and files in, a tier plus its plain-language reasons out. No model, no
network, no I/O, and no extra review pass: the tier is advisory, composed from signals the engine
already has, so it costs nothing and can never disagree with the findings printed below it.

The tier answers "how closely should a human read this?", not "is it safe to merge" - that is the
verdict's job. It never blocks and never changes the review depth on its own; the depth tiers in
:mod:`premortem` are what decide which passes run.
"""
from __future__ import annotations

from . import severity as sev
from .diff import FileDiff
from .premortem import RISKY_TOKENS, tokens  # one shared path-risk definition
from .walkthrough import classify

LEVELS = ("L0", "L1", "L2", "L3")

_HIGH = frozenset({"blocker"})
_MID = frozenset({"major"})


def _index(level: str) -> int:
    return LEVELS.index(level)


def _raise_to(current: str, level: str) -> str:
    return level if _index(level) > _index(current) else current


def tier(findings: list[dict], fds: list[FileDiff], secrets_flagged: bool = False,
         surprises: list[str] | None = None) -> dict:
    """Compute the tier and the reasons it was raised.

    Returns ``{"level": "L0".."L3", "reasons": [str], "advisory": str}``. L1 is any code change;
    L2 is a mid-severity finding or a large diff; L3 is a high/blocker finding, a secret, a
    schema migration, or a payment/auth/security path. Two overrides raise one level regardless of
    size: an irreversible change and weak evidence (code changed with no test change). Any
    ``surprises`` (from :mod:`surprise`) raise the tier to at least L2 and are named.
    """
    reasons: list[str] = []
    code_files = [fd for fd in fds if classify(fd.path) not in ("docs", "tests")]
    level = "L1" if fds else "L0"

    def bump(to: str, why: str) -> None:
        nonlocal level
        if _index(to) > _index(level):
            level = to
        reasons.append(why)

    if any(sev.normalize(f.get("severity")) in _HIGH for f in findings):
        bump("L3", "high/blocker finding")
    if any(sev.normalize(f.get("severity")) in _MID for f in findings):
        bump("L2", "medium-severity finding")
    if secrets_flagged:
        bump("L3", "secret scanner flagged")
    risky = [fd.path for fd in fds if tokens(fd.path) & RISKY_TOKENS]
    if risky:
        bump("L3", f"payment/auth/security path: {risky[0]}")
    migration = [fd.path for fd in fds if tokens(fd.path) & {"migration", "migrations", "alembic"}]
    if migration:
        bump("L3", "irreversible: schema migration")
    if sum(len(fd.added) for fd in fds) >= 200:
        bump("L2", "large diff (200+ added lines)")
    if code_files and not any(classify(fd.path) == "tests" for fd in fds):
        bump(_raise_to(level, "L2"), "weak evidence: code changed, no test change")
    for s in surprises or []:
        bump(_raise_to(level, "L2"), f"surprise: {s}")

    if not reasons:
        reasons.append("small, low-risk change")
    return {"level": level, "reasons": reasons, "advisory": advisory(level)}


_ADVISORY = {
    "L0": "not reviewed closely, no code changed",
    "L1": "skim",
    "L2": "read the changed hunks",
    "L3": "read closely",
}


def advisory(level: str) -> str:
    return _ADVISORY.get(level, _ADVISORY["L1"])


def render_header_md(level: str, reasons: list[str]) -> str:
    """One advisory line for the top of the consolidated comment, or empty for L0."""
    if level not in LEVELS or level == "L0":
        return ""
    why = ", ".join(reasons[:3])
    return f"> **Review depth recommended: {level}** - {advisory(level)}. _{why}_"

"""Severity taxonomy: v1 names in, ranked v2 names out, plus comment budgets.

v1 used critical|warning|suggestion and posted every finding it got. Competitors
that survive review volume (Bugbot, Greptile, CodeRabbit) all rank by severity
and cap the comment count, because a review with 30 comments gets ignored.
"""
from __future__ import annotations

RANK = {"blocker": 0, "critical": 0, "major": 1, "warning": 1, "minor": 2, "suggestion": 2, "nit": 3, "info": 4}
ALIASES = {
    "blocker": "blocker", "critical": "blocker", "high": "major", "error": "major",
    "major": "major", "warning": "minor", "medium": "minor", "minor": "minor",
    "suggestion": "nit", "low": "nit", "nit": "nit", "info": "nit", "style": "nit",
}
CATEGORIES = {"bug", "security", "performance", "style", "test", "docs", "maintainability"}


def normalize(value: str | None) -> str:
    return ALIASES.get((value or "").strip().lower(), "minor")


def rank(value: str | None) -> int:
    return RANK.get(normalize(value), 2)


def meets(severity: str | None, minimum: str | None) -> bool:
    if not minimum:
        return True
    return rank(severity) <= rank(minimum)


def normalize_category(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in CATEGORIES:
        return v
    return {"perf": "performance", "bugfix": "bug", "correctness": "bug",
            "sec": "security", "vuln": "security", "typo": "docs"}.get(v, "maintainability")


def budget(findings: list, max_comments: int, min_severity: str | None = None):
    """Sort by severity then confidence, cut to budget. Returns (kept, dropped)."""
    eligible = [f for f in findings if meets(f.get("severity"), min_severity)]
    over = [f for f in findings if not meets(f.get("severity"), min_severity)]
    eligible.sort(key=lambda f: (rank(f.get("severity")), -(f.get("confidence") or 0), f.get("path") or "", f.get("line") or 0))
    kept = eligible[:max_comments]
    dropped = over + eligible[max_comments:]
    return kept, dropped


def apply_lens_caps(findings: list, caps: dict | None):
    """Keep at most caps[lens] findings per lens, order preserved. Returns (kept, dropped).

    A lens absent from caps is uncapped. Findings with no lens count as "unknown".
    """
    if not caps:
        return list(findings), []
    used: dict[str, int] = {}
    kept: list = []
    dropped: list = []
    for f in findings:
        lens = str(f.get("lens") or "unknown")
        limit = caps.get(lens)
        if limit is None or used.get(lens, 0) < limit:
            if limit is not None:
                used[lens] = used.get(lens, 0) + 1
            kept.append(f)
        else:
            dropped.append(f)
    return kept, dropped


LABELS = {"blocker": "Blocker", "major": "Major", "minor": "Minor", "nit": "Nit"}
EMOJI = {"blocker": "🔴", "major": "🟠", "minor": "🟡", "nit": "🔵"}

# v1 shipped three severities (critical|warning|suggestion) and the VS Code
# extension keys its colours off those exact strings, so the adapter maps back.
_LEGACY = {"blocker": "critical", "major": "warning", "minor": "warning", "nit": "suggestion"}


def to_legacy(sev: str | None) -> str:
    return _LEGACY.get(normalize(sev), "warning")


def label(sev: str | None) -> str:
    return LABELS.get(normalize(sev), "Minor")


def emoji(sev: str | None) -> str:
    return EMOJI.get(normalize(sev), "🟡")


# The priority label every reviewer already reads (Greptile/CodeRabbit use P1/P2/P3).
# Nits deliberately have none, so a style remark is never dressed up as a priority.
PRIORITY = {"blocker": "P1", "major": "P2", "minor": "P3"}


def priority(sev: str | None) -> str | None:
    """P1/P2/P3 for blocker/major/minor, None for a nit.

    An unknown string normalizes to `minor` first, so it reads as P3 rather than inventing a
    level; only a nit is left without a priority.
    """
    return PRIORITY.get(normalize(sev))


_PENALTY = {"blocker": 25, "major": 12, "minor": 4, "nit": 1}


def score_from_findings(findings: list, base: int = 100) -> int:
    score = base
    for f in findings:
        score -= _PENALTY.get(normalize(f.get("severity")), 4)
    return max(0, min(100, score))

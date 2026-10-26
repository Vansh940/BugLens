"""Pure cross-lens defect merging. No I/O, no model.

`diff.fingerprint` hashes path + category + model-worded title, so the same real
defect described by the security lens and the correctness lens gets two different
fingerprints and posts twice. This module keys a finding on what actually
identifies a defect (its evidence quote, else its anchor, else its title) so the
same defect collapses to one comment no matter which lens found it.
"""
from __future__ import annotations

import hashlib
import re

from . import severity

EVIDENCE_HASH_CHARS = 20


def normalize_evidence(text) -> str:
    """Lowercase, collapse all whitespace runs to a single space, strip. Empty/None -> ''."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _norm_title(title) -> str:
    """Lowercase a title and drop everything that is not a letter, digit, or space."""
    text = re.sub(r"[^a-z0-9 ]", "", str(title or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def defect_key(finding: dict) -> str:
    """A key that is the same for the same real defect reported by different lenses."""
    path = str(finding.get("path") or "")
    evidence = normalize_evidence(finding.get("evidence"))
    if evidence:
        digest = hashlib.sha256(evidence.encode()).hexdigest()[:EVIDENCE_HASH_CHARS]
        return f"{path}|e|{digest}"
    line = finding.get("line")
    if line is not None and line != "":
        try:
            category = severity.normalize_category(finding.get("category"))
            return f"{path}|l|{int(line)}|{category}"
        except (TypeError, ValueError):
            pass
    return f"{path}|t|{_norm_title(finding.get('title'))}"


def _rank_order(index: int, finding: dict) -> tuple[int, float, int]:
    """Sort key: best severity rank, then highest confidence (None counts as 0), then earliest."""
    return (severity.rank(finding.get("severity")), -(finding.get("confidence") or 0), index)


def dedupe(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Collapse findings that describe the same defect; returns (kept, dropped)."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for finding in findings:
        key = defect_key(finding)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(finding)

    kept: list[dict] = []
    dropped: list[dict] = []
    for key in order:
        group = groups[key]
        winner = min(enumerate(group), key=lambda pair: _rank_order(pair[0], pair[1]))[1]
        annotated = dict(winner)
        annotated["lenses"] = sorted({str(f.get("lens")) for f in group if f.get("lens")})
        win_loc = (str(winner.get("path") or ""), winner.get("line"))
        additional: list[dict] = []
        seen: set[tuple] = set()
        for finding in group:
            if finding is winner:
                continue
            loc = (str(finding.get("path") or ""), finding.get("line"))
            if loc == win_loc or loc in seen:
                continue
            seen.add(loc)
            additional.append({"path": loc[0], "line": loc[1]})
        if additional:
            annotated["additional"] = additional
        kept.append(annotated)
        dropped.extend(finding for finding in group if finding is not winner)
    return kept, dropped

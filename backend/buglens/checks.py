"""Pure mapping from BugLens findings to GitHub check-run payloads. No I/O, no model."""
from __future__ import annotations

from . import severity

ANNOTATION_LEVEL = {"blocker": "failure", "major": "warning", "minor": "notice", "nit": "notice"}
CONCLUSIONS = {"action_required", "cancelled", "failure", "neutral", "success", "skipped", "timed_out"}
ANNOTATION_CHUNK = 50
MAX_TITLE = 255
MAX_MESSAGE = 65535
MAX_METADATA = 2000
MAX_INPUT = 60000


def _normalize(value: str | None) -> str:
    return severity.normalize(value)


def annotation_level(severity: str | None) -> str:
    """Map a raw severity onto a GitHub annotation level (failure|warning|notice)."""
    return ANNOTATION_LEVEL.get(_normalize(severity), "notice")


def finding_to_annotation(f: dict) -> dict | None:
    """One finding as an annotation, or None when it cannot be anchored to a line."""
    line = f.get("line")
    path = f.get("path")
    if not line or not path:
        return None
    title = str(f.get("title") or "BugLens finding")
    message = title
    body = f.get("body")
    if body:
        message += "\n\n" + str(body)
    evidence = f.get("evidence")
    if evidence:
        message += "\nEvidence: " + str(evidence)
    return {
        "path": path,
        "start_line": int(line),
        "end_line": int(line),
        "annotation_level": annotation_level(f.get("severity")),
        "title": title[:MAX_TITLE],
        "message": message[:MAX_MESSAGE],
    }


def annotations_from_findings(findings: list[dict], cap: int) -> tuple[list[dict], int]:
    """(kept annotations, dropped count), severity-ordered then confidence-descending."""
    pairs: list[tuple[dict, dict]] = []
    for f in findings:
        ann = finding_to_annotation(f)
        if ann is not None:
            pairs.append((f, ann))
    pairs.sort(key=lambda p: (severity.rank(p[0].get("severity")), -float(p[0].get("confidence") or 0)))
    kept = [ann for _, ann in pairs[: max(0, cap)]]
    return kept, len(findings) - len(kept)


def annotation_chunks(annotations: list[dict], size: int = ANNOTATION_CHUNK) -> list[list[dict]]:
    """Split annotations into consecutive chunks of at most `size`; never reorders."""
    if size <= 0 or not annotations:
        return []
    return [annotations[i:i + size] for i in range(0, len(annotations), size)]


def conclusion_for(verdict: str, base: str, required: bool) -> str:
    """Check-run conclusion from verdict, configured base, and required-status flag."""
    if verdict == "blocking" and required:
        return "failure"
    if base == "failure" and not required:
        return "neutral"
    if base not in CONCLUSIONS:
        return "neutral"
    return base


def input_text(mode: str, res, picked: list) -> str:
    """Deterministic check-run output text for one of the three input modes."""
    if mode == "incremental":
        return _incremental(picked)
    if mode == "full_diff":
        return _full_diff(picked)
    return _pr_metadata(res)


def _pr_metadata(res) -> str:
    """Short deterministic metadata block; smallest payload."""
    lines: list[str] = []
    repo = getattr(res, "repo", "")
    pr = getattr(res, "pr", "")
    if repo and pr:
        lines.append(f"{repo}#{pr}")
    for label, attr in (("Status", "status"), ("Score", "score"), ("Verdict", "verdict")):
        value = getattr(res, attr, None)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    files = getattr(res, "files", None) or []
    findings = getattr(res, "findings", None) or []
    lines.append(f"Files changed: {len(files)}")
    lines.append(f"Findings: {len(findings)}")
    return "\n".join(lines)[:MAX_METADATA]


def _incremental(picked: list) -> str:
    """Only the added lines of the current revision, as `<path>:<line> <text>`."""
    out: list[str] = []
    for fd in picked or []:
        path = getattr(fd, "path", "")
        for line in getattr(fd, "added", None) or []:
            out.append(f"{path}:{line.new_line} {line.text}")
    return "\n".join(out)[:MAX_INPUT]


def _full_diff(picked: list) -> str:
    """Reconstructed new-file text for each picked file under a `### <path>` heading."""
    out: list[str] = []
    for fd in picked or []:
        out.append(f"### {getattr(fd, 'path', '')}")
        out.append(fd.new_file_text())
    return "\n".join(out)[:MAX_INPUT]

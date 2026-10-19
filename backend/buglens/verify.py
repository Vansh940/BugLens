"""Verification gate: the difference between a reviewer people keep and one they mute.

Two stages, cheapest first:

1. **Machine gate** (free, deterministic), is the finding's quoted evidence
   actually present in the added lines? Is its line a real added line? If not, it
   is a hallucination or a stale line and is dropped. This is the single highest
   value check in the whole pipeline.
2. **Model gate** (one strict adversarial call per surviving finding), a second,
   cheaper opinion that defaults to "drop" when the evidence does not prove the
   claim. Bounded by ``rules.verify_top_n`` so big PRs stay affordable.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from . import llm, prompts
from .diff import FileDiff, added_text_contains, anchor_to_evidence, fingerprint, snap_to_added
from .rules import Rules
from .severity import normalize, normalize_category

DEFAULT_EVIDENCE_MIN = 8
VERIFY_CONCURRENCY = int(os.getenv("BUGLENS_VERIFY_CONCURRENCY", "4"))


@dataclass
class Verified:
    kept: list[dict]
    dropped: list[dict]


def normalize_finding(raw: dict, lens_name: str, fallback_category: str, path: str) -> dict | None:
    """Coerce a raw model finding into the canonical shape, or reject it."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    evidence = str(raw.get("evidence") or "").strip()
    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    line = raw.get("line", raw.get("line_number"))
    try:
        line = int(line) if line is not None else None
    except (TypeError, ValueError):
        line = None
    suggestion = raw.get("suggestion")
    suggestion = str(suggestion).strip() if suggestion else None
    # A GitHub suggestion block replaces exactly the commented line(s). Only keep
    # clean single-line replacements; anything else falls back to prose "fix".
    if suggestion and ("\n" in suggestion or "\r" in suggestion or suggestion.startswith("```")
                       or len(suggestion) > 500):
        suggestion = None
    finding = {
        "path": path,
        "lens": lens_name,
        "severity": normalize(str(raw.get("severity") or "minor")),
        "category": normalize_category(str(raw.get("category") or fallback_category)),
        "line": line,
        "title": title,
        "body": str(raw.get("body") or raw.get("description") or "").strip(),
        "fix": str(raw.get("fix") or "").strip(),
        "suggestion": suggestion,
        "evidence": evidence,
        "confidence": max(0.0, min(1.0, confidence)),
    }
    finding["fingerprint"] = fingerprint(path, finding["category"], title)
    return finding


def machine_gate(finding: dict, fd: FileDiff, rules: Rules) -> tuple[bool, str]:
    """Deterministic checks. Returns (ok, reason_if_not)."""
    if not finding["body"] and not finding["fix"]:
        return False, "empty body and fix"
    bypass = finding.get("deterministic") or finding.get("ticket")
    if rules.require_evidence and not bypass:
        # Deterministic findings (e.g. secret scanner) quote a *masked* line, and
        # ticket/spec findings are PR-level verdicts with no single line, so the
        # substring gate cannot apply to either.
        if len(finding["evidence"]) < DEFAULT_EVIDENCE_MIN:
            return False, "no/too-short evidence quote"
        if not added_text_contains(fd, finding["evidence"]):
            return False, "evidence not present in the diff"
    # Position-drift gate: prefer the line the evidence actually comes from over the
    # number the model reported. Only fall back to proximity snapping when the quote
    # matches no added line (masked deterministic findings, ticket verdicts).
    anchored = anchor_to_evidence(fd, finding.get("evidence"), prefer=finding["line"])
    if anchored is not None:
        if finding["line"] is not None and finding["line"] != anchored:
            finding["line_drift"] = anchored - finding["line"]
        finding["line"] = anchored
    else:
        snapped = snap_to_added(fd, finding["line"])
        if finding["line"] is not None and snapped is None:
            return False, "reported line is not an added line of this file"
        finding["line"] = snapped if snapped is not None else finding["line"]
    if finding["confidence"] < 0.35:
        return False, "confidence below floor"
    return True, ""


async def model_gate(finding: dict, fd: FileDiff, timeout: float = 45.0) -> tuple[bool, str]:
    """One adversarial verification call. Never raises: on error, keep the finding."""
    window = fd.window(finding["line"] or 0, before=15, after=15) if finding.get("line") else ""
    messages = [
        {"role": "system", "content": prompts.VERIFY_SYSTEM},
        {"role": "user", "content": prompts.build_verify_prompt(finding, window)},
    ]
    try:
        result = await llm.complete_json(messages, max_tokens=700, timeout=timeout)
    except Exception as exc:                       # LLM hiccup must not drop real work
        finding["verify"] = f"skipped: {exc}"
        return True, "verify-unavailable"
    verdicts = result.data.get("verdicts") or []
    verdict = verdicts[0] if verdicts else {}
    decision = str(verdict.get("verdict", "keep")).lower()
    if decision == "drop" and verdict.get("decidable", True):
        return False, str(verdict.get("reason") or "model-verified false positive")
    finding["verify"] = str(verdict.get("reason") or "kept")
    return True, "kept"


async def verify_all(findings: list[dict], files_by_path: dict[str, FileDiff], rules: Rules,
                     enable_model: bool | None = None) -> Verified:
    """Run both gates over a flat finding list (already normalized)."""
    use_model = rules.verify if enable_model is None else enable_model
    suppressed = getattr(rules, "_suppressed", set())
    machine_kept: list[dict] = []
    dropped: list[dict] = []

    for finding in findings:
        fd = files_by_path.get(finding["path"])
        if fd is None:
            dropped.append({**finding, "drop_reason": "file not parsed"})
            continue
        ok, reason = machine_gate(finding, fd, rules)
        if not ok:
            dropped.append({**finding, "drop_reason": "machine: " + reason})
            continue
        if finding["fingerprint"] in suppressed:
            dropped.append({**finding, "drop_reason": "suppressed by prior feedback"})
            continue
        machine_kept.append(finding)

    if not use_model:
        return Verified(kept=machine_kept, dropped=dropped)

    ordered = sorted(machine_kept, key=lambda f: (f["severity"], -f["confidence"]))
    kept: list[dict] = []
    to_gate: list[dict] = []
    for i, finding in enumerate(ordered):
        if finding.get("deterministic") or finding.get("ticket"):
            kept.append(finding)                   # ground truth / PR-level verdict: no model gate
            continue
        if i >= rules.verify_top_n:
            kept.append(finding)                   # beyond budget: trust the machine gate
            continue
        to_gate.append(finding)

    # Gate concurrently (bounded), serially this added a full round-trip per finding.
    if to_gate:
        gates = asyncio.Semaphore(VERIFY_CONCURRENCY)

        async def _gate(finding: dict) -> tuple[bool, str]:
            async with gates:
                return await model_gate(finding, files_by_path.get(finding["path"]))

        results = await asyncio.gather(*[_gate(f) for f in to_gate])
        for finding, (ok, reason) in zip(to_gate, results):
            if ok:
                kept.append(finding)
            else:
                dropped.append({**finding, "drop_reason": "model: " + reason})

    # keep the severity order of the output stable regardless of completion order
    rank = {id(f): i for i, f in enumerate(ordered)}
    kept.sort(key=lambda f: rank.get(id(f), 0))
    return Verified(kept=kept, dropped=dropped)

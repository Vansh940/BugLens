"""End-to-end orchestration: plan -> context -> review -> verify -> post.

One entry point per surface:

* :func:`review_pr`, a GitHub pull request (webhook).
* :func:`review_snippet`, a chunk of code from the IDE extension.

Everything is best-effort and bounded: any single model call failing degrades the
result instead of failing the review, and posting is skipped cleanly when there is
no GitHub token (so the pipeline is fully testable offline).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import adapters, checks, dedupe, github, indexer, integrations, learn, llm, objects, premortem, prodcontext, prompts, recall, risktier, secretscan, store, surprise, symbols, tickets, verify, walkthrough
from . import lenses as L
from .context import build_context, extract_symbols, language_of
from .diff import FileDiff, DiffLine, ADD, choose_paths, parse_patch
from . import rules as rules_mod
from .rules import Rules, load_rules
from . import severity as sev

REVIEWABLE_EXTS = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts",
    ".go", ".rs", ".rb", ".php", ".cs", ".swift", ".c", ".h", ".cpp", ".cc", ".hpp",
    ".scala", ".sh", ".bash", ".ps1", ".sql", ".html", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte", ".astro", ".yml", ".yaml", ".tf", ".toml", ".json", ".dart",
}
MARKER = "<!-- buglens:v2 -->"
MARKER_CAP = "<!-- buglens:round-cap -->"
MARKER_ERROR = "<!-- buglens:could-not-run -->"
log = logging.getLogger(__name__)
_CHECK_STATUS = {"pass": "✅ Pass", "warn": "⚠️ Warn", "info": "• Info"}
STALE_REPLY = "BugLens: this finding is no longer present at {sha}. Marking the thread outdated."
MAX_LENSES_PER_FILE = 3
CONCURRENCY = 4
RIPPLE_FETCH = 6          # related files whose bodies we fetch to build the symbol graph


@dataclass
class ReviewResult:
    status: str = "ok"                       # ok | skipped | dry-run | error
    summary: str = ""
    score: int = 100
    verdict: str = "looks-good"
    findings: list[dict] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    lenses_run: list[str] = field(default_factory=list)
    dropped: int = 0
    model_used: str = ""
    usage: dict = field(default_factory=dict)
    review_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    posted: bool = False
    repo: str = ""
    pr: int = 0
    head_sha: str = ""
    base_sha: str = ""
    context_sources: list[str] = field(default_factory=list)
    review_effort: int = 1
    ticket_summary: str = ""
    premortem: list[dict] = field(default_factory=list)
    risk_tier: str = ""
    risk_reasons: list[str] = field(default_factory=list)
    wall_seconds: float = 0.0
    round_no: int = 0
    converging: bool = False
    engine_notes: list[str] = field(default_factory=list)
    change_stack: list[dict] = field(default_factory=list)
    pre_merge: list[dict] = field(default_factory=list)
    verdict_reason: str = ""
    confidence: int = 0
    merge_risk: str = ""
    security_risk: str = ""


# ── one file / one lens ───────────────────────────────────────────────────────
def _shuffle_fd(fd: FileDiff, seed: int) -> FileDiff:
    """A copy of the diff with the added/context lines in a different order.

    Bugbot's best trick: run several independent passes over shuffled diffs and
    keep only what a majority of passes agree on. Section order changing is a
    cheap way to break the model's positional bias without losing line numbers.
    """
    rng = random.Random(seed)
    lines = list(fd.lines)
    rng.shuffle(lines)
    clone = FileDiff(path=fd.path, language=fd.language, status=fd.status)
    clone.lines = lines
    return clone


async def _one_pass(fd: FileDiff, lens: L.Lens, rules: Rules, context_block: str,
                    related_block: str, prior_good: list[str], sem: asyncio.Semaphore,
                    seed: int = 0) -> tuple[list[dict], list[str], dict]:
    body = fd if seed == 0 else _shuffle_fd(fd, seed)
    user = prompts.build_review_prompt(body, lens, rules, context_block, related_block, prior_good)
    messages = [
        {"role": "system", "content": prompts.REVIEW_SYSTEM},
        {"role": "user", "content": user},
    ]
    async with sem:
        try:
            result = await llm.complete_json(messages)
        except Exception as exc:
            # Count the failure (numeric, so _merge_usage keeps it) instead of returning a
            # bare error dict that the merge silently dropped. If *every* call fails the
            # review must not report itself as a clean pass.
            return [], [], {"calls": 1, "errors": 1, "error_sample": str(exc)[:200]}
    findings = []
    for item in result.data.get("findings") or []:
        norm = verify.normalize_finding(item, lens.name, lens.category, fd.path)
        if norm:
            findings.append(norm)
    positives = [str(p) for p in (result.data.get("positives") or []) if str(p).strip()]
    usage = dict(result.usage)
    usage["calls"] = usage.get("calls", 0) + 1
    return findings, positives, usage


async def _run_lens(fd: FileDiff, lens: L.Lens, rules: Rules, context_block: str,
                    related_block: str, prior_good: list[str], sem: asyncio.Semaphore) -> tuple[list[dict], list[str], dict]:
    passes = max(1, int(getattr(rules, "consensus", 1) or 1))
    if passes == 1:
        return await _one_pass(fd, lens, rules, context_block, related_block, prior_good, sem, seed=0)

    results = await asyncio.gather(
        *[_one_pass(fd, lens, rules, context_block, related_block, prior_good, sem, seed=i) for i in range(passes)],
        return_exceptions=True,
    )
    votes: dict[str, list[dict]] = {}
    positives: list[str] = []
    usage: dict = {}
    for res in results:
        if isinstance(res, Exception) or not res:
            continue
        f, p, u = res
        for finding in f:
            votes.setdefault(finding["fingerprint"], []).append(finding)
        positives.extend(p)
        usage = _merge_usage(usage, u)

    needed = passes // 2 + 1
    kept = [max(group, key=lambda x: x.get("confidence") or 0)
            for group in votes.values() if len(group) >= needed]
    return kept, positives, usage


async def _review_file(fd: FileDiff, lenses: list[L.Lens], rules: Rules, context_block: str,
                       related_block: str, prior_good: list[str], sem: asyncio.Semaphore):
    tasks = [_run_lens(fd, lens, rules, context_block, related_block, prior_good, sem) for lens in lenses[:MAX_LENSES_PER_FILE]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    findings: list[dict] = []
    positives: list[str] = []
    usage: dict = {}
    for res in results:
        if isinstance(res, Exception) or not res:
            continue
        f, p, u = res
        findings.extend(f)
        positives.extend(p)
        usage = _merge_usage(usage, u)
    return findings, positives, usage, [lens.name for lens in lenses[:MAX_LENSES_PER_FILE]]


def _merge_usage(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, (int, float)):
            out[k] = out.get(k, 0) + v
    return out


def _lens_health(usage: dict) -> tuple[int, int]:
    """(calls attempted, calls that failed) across every lens invocation."""
    return int(usage.get("calls") or 0), int(usage.get("errors") or 0)


def _content_sha(fd: FileDiff) -> str:
    """Deterministic hash of a file's whole diff hunk (kind + text).

    Answers "did this file change since we last reviewed it?" without another network fetch:
    the diff we already hold is the review unit. ``\\x1f`` separates kind from text so a
    context line and an added line with the same body never collide."""
    payload = "".join(f"{l.kind}\x1f{l.text}\n" for l in fd.lines)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def _tokens(usage: dict) -> tuple[int, int, int]:
    """(prompt, completion, total) tokens, tolerant of provider spellings."""
    pin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    pout = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or 0) or (pin + pout)
    return pin, pout, total


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds or 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _cost_estimate(usage: dict) -> str:
    """A dollar figure only when the operator supplied rates, never invent a price.

    Providers reprice constantly and a confidently wrong number is worse than none (this is
    the whole "uncounted usage" backlash). Set ``BUGLENS_COST_PER_1M_IN``/``_OUT`` to opt in.
    """
    try:
        in_rate = float(os.getenv("BUGLENS_COST_PER_1M_IN", "") or 0)
        out_rate = float(os.getenv("BUGLENS_COST_PER_1M_OUT", "") or 0)
    except ValueError:
        return ""
    if in_rate <= 0 and out_rate <= 0:
        return ""
    pin, pout, _ = _tokens(usage)
    if not (pin or pout):
        return ""
    return f"~${pin / 1e6 * in_rate + pout / 1e6 * out_rate:.3f}"


def _engine_stats(usage: dict, wall: float) -> str:
    """One honest line: calls, failures, tokens (only if the provider reported them), cost
    (only if priced), wall-clock. Never fabricate a number the provider did not return."""
    u = usage or {}
    calls = int(u.get("calls") or 0)
    errors = int(u.get("errors") or 0)
    segs: list[str] = []
    if calls:
        segs.append(f"{calls} engine call(s)" + (f", {errors} failed" if errors else ""))
    pin, pout, total = _tokens(u)
    if total:
        segs.append(f"~{_fmt_tokens(total)} tokens ({_fmt_tokens(pin)} in / {_fmt_tokens(pout)} out)")
    elif calls:
        segs.append("provider reported no token usage")
    cost = _cost_estimate(u)
    if cost:
        segs.append(cost)
    if wall:
        segs.append(_fmt_duration(wall))
    return ("Engine: " + " · ".join(segs)) if segs else ""


def _engine_notes(rules: Rules, verified, all_findings: list[dict],
                  ticket: bool, graph_files: int, recall: bool, calls: int, failures: int,
                  converging: bool, linters_run: list[str] | None = None,
                  ripple: int = 0, learned_scopes: int = 0,
                  demoted_lenses: int = 0, depth_tier: str = "",
                  depth_triggers: list[str] | None = None,
                  premortem_items: int = 0) -> list[str]:
    """Plain-language disclosure of every optional layer that changed review depth.

    No feature may alter review depth silently, the non-consensual-defaults lesson."""
    notes: list[str] = []
    if int(getattr(rules, "consensus", 1) or 1) > 1:
        notes.append(f"consensus {rules.consensus} passes")
    if not rules.verify:
        notes.append("verify gate off (config)")
    elif verified.dropped:
        notes.append(f"verify dropped {len(verified.dropped)} of "
                     f"{len(verified.kept) + len(verified.dropped)} candidate(s)")
    if any(f.get("lens") == "secrets" for f in all_findings):
        notes.append("secret scan flagged")
    if linters_run:
        notes.append("linters: " + ", ".join(linters_run))
    if ticket:
        notes.append("linked-ticket check")
    if graph_files:
        notes.append(f"code graph: {graph_files} file(s)")
    if ripple:
        notes.append(f"ripple: {ripple} changed symbol(s) with callers")
    if learned_scopes:
        notes.append(f"learned from feedback: {learned_scopes} dismissal scope(s)")
    if demoted_lenses:
        notes.append(f"adaptive: {demoted_lenses} lens(es) demoted by feedback")
    if recall:
        notes.append("prior-review recall")
    if calls and failures and failures < calls:
        notes.append(f"partial: {failures}/{calls} model call(s) failed, some files may be under-reviewed")
    if converging:
        notes.append(f"{rules.converge_rounds}+ rounds without convergence")
    if depth_tier:
        note = f"review depth: {depth_tier}"
        if depth_triggers:
            note += f" (triggers: {', '.join(depth_triggers)})"
        notes.append(note)
    if premortem_items:
        notes.append(f"pre-mortem: {premortem_items} scenario(s)")
    return notes


def _reanchor_merge_findings(findings: list[dict],
                             line_map: dict[int, tuple[str, int]]) -> list[dict]:
    """Point a PR-level merge-lens finding back at the file line it actually quoted.

    The merge lens reviews a shadow diff that copies every added line under a per-file
    header, so its findings carry shadow line numbers. Mapping them back to the real
    ``(path, line)`` lets semantic dedup collapse a merge finding that merely repeats a
    file-lens finding, instead of counting the same defect twice. A finding anchored to a
    shadow header line (or with no line at all) is a genuine PR-level concern and stays on
    ``<pull-request>``.
    """
    out: list[dict] = []
    for f in findings:
        if f.get("path") == "<pull-request>" and f.get("line"):
            real = line_map.get(int(f["line"]))
            if real:
                f = dict(f)
                f["path"], f["line"] = real
        out.append(f)
    return out


# ── PR-level merge lens ───────────────────────────────────────────────────────
def _pr_shadow(files: list[FileDiff],
               statuses: dict[str, str]) -> tuple[FileDiff, dict[int, tuple[str, int]]]:
    shadow = FileDiff(path="<pull-request>", language="plaintext")
    line_map: dict[int, tuple[str, int]] = {}
    n = 0
    for fd in files:
        n += 1
        shadow.lines.append(DiffLine(ADD, f"# {statuses.get(fd.path, 'changed')}: {fd.path}", n, None, 0, n))
        for l in fd.lines:
            if l.kind == ADD:
                n += 1
                line_map[n] = (fd.path, int(l.new_line))
                shadow.lines.append(DiffLine(ADD, l.text, n, None, 0, n))
    return shadow, line_map


async def _run_merge_lens(files: list[FileDiff], rules: Rules, pi: github.PRInfo | None,
                          statuses: dict[str, str]) -> tuple[list[dict], list[str], dict]:
    shadow = _pr_shadow(files, statuses)[0]
    body = _pr_digest(files, rules, pi, statuses)
    lens = L.MERGE
    user = prompts.build_review_prompt(shadow, lens, rules, context_block=body, related_block="")
    messages = [
        {"role": "system", "content": prompts.REVIEW_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        result = await llm.complete_json(messages)
    except Exception as exc:
        return [], [], {"error": str(exc)}
    findings = []
    for item in result.data.get("findings") or []:
        norm = verify.normalize_finding(item, lens.name, lens.category, "<pull-request>")
        if norm:
            findings.append(norm)
    return findings, [str(p) for p in (result.data.get("positives") or [])], result.usage


def _pr_digest(files: list[FileDiff], rules: Rules, pi: github.PRInfo | None, statuses: dict[str, str]) -> str:
    lines = ["Changed files:"]
    for fd in files:
        lines.append(f"- {fd.path} ({statuses.get(fd.path, 'modified')}), +{len(fd.added)} lines")
        if fd.is_binary:
            lines.append("  (binary)")
    if pi:
        lines.insert(0, f"PR title: {pi.title}")
        lines.insert(1, f"PR author: {pi.author}  draft: {pi.draft}  state: {pi.state}")
        if pi.body:
            lines.append("PR description (untrusted):")
            lines.append(pi.body[:1500])
    return "\n".join(lines)


# ── summary ───────────────────────────────────────────────────────────────────
async def _summarise(findings: list[dict], files: list[str], lenses_run: list[str],
                     skipped: list[dict]) -> dict:
    """One model call for the PR verdict, or an honest deterministic fallback.

    Returns a dict: summary, score, verdict, highlights, positives, verdict_reason,
    confidence, merge_risk, security_risk, usage. The risk axes and confidence are the
    model's judgement over the verified findings and are labelled as such where rendered.
    """
    digest = "\n".join(
        f"- [{sev.label(f.get('severity')).upper()}/{f.get('category')}] {f.get('path')}:{f.get('line')} {f.get('title')}"
        for f in findings
    )
    messages = [
        {"role": "system", "content": prompts.SUMMARY_SYSTEM},
        {"role": "user", "content": prompts.build_summary_prompt(digest, files, lenses_run, skipped)},
    ]
    fallback_score = sev.score_from_findings(findings)
    has_blocker = any(sev.normalize(f.get("severity")) == "blocker" for f in findings)
    try:
        result = await llm.complete_json(messages, max_tokens=900)
        data = result.data
        summary = str(data.get("summary") or "").strip()
        score = int(data.get("score", fallback_score))
        verdict = str(data.get("verdict") or ("blocking" if has_blocker else "looks-good"))
        highlights = [str(h) for h in (data.get("highlights") or [])][:5]
        positives = [str(p) for p in (data.get("positives") or [])][:5]
        return {
            "summary": summary,
            "score": max(0, min(100, score)),
            "verdict": verdict,
            "highlights": highlights,
            "positives": positives,
            "verdict_reason": str(data.get("verdict_reason") or "").strip()[:400],
            "confidence": _clamp_pct(data.get("confidence")),
            "merge_risk": _risk_level(data.get("merge_risk")),
            "security_risk": _risk_level(data.get("security_risk")),
            "usage": result.usage,
        }
    except Exception:
        verdict = "blocking" if has_blocker else ("needs-attention" if findings else "looks-good")
        summary = f"{len(findings)} finding(s) across {len(files)} file(s)."
        return {
            "summary": summary,
            "score": fallback_score,
            "verdict": verdict,
            "highlights": [],
            "positives": [],
            "verdict_reason": "",
            "confidence": 0,
            "merge_risk": "",
            "security_risk": "",
            "usage": {},
        }


_RISK_LEVELS = ("minimal", "none", "low", "medium", "high")


async def _premortem(findings: list[dict], files: list[str],
                     fired: list[str]) -> tuple[list[dict], dict]:
    """One bounded model call for the deep tier's production pre-mortem.

    Returns ``(items, usage)``. The model proposes failure scenarios; ``premortem.select`` is the
    gate that keeps only the ones anchored on a line this PR added, so a scenario is never
    relocated or invented. Any failure yields an empty list: a pre-mortem is a bonus, never a
    source of fabricated claims.
    """
    digest = "\n".join(
        f"- [{sev.label(f.get('severity')).upper()}/{f.get('category')}] "
        f"{f.get('path')}:{f.get('line')} {f.get('title')}"
        for f in findings
    )
    messages = [
        {"role": "system", "content": prompts.PREMORTEM_SYSTEM},
        {"role": "user", "content": prompts.build_premortem_prompt(digest, files, fired)},
    ]
    try:
        result = await llm.complete_json(messages, max_tokens=700)
        raw = result.data.get("items") or []
        items = [
            {"title": str(it.get("title") or "").strip(),
             "why": str(it.get("why") or "").strip(),
             "path": str(it.get("path") or "").strip(),
             "line": it.get("line")}
            for it in raw if isinstance(it, dict)
        ]
        return items, result.usage
    except Exception:
        return [], {}


def _risk_level(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in _RISK_LEVELS else ""


def _clamp_pct(value) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


# ── rendering ─────────────────────────────────────────────────────────────────
def render_finding_md(f: dict) -> str:
    p = sev.priority(f.get("severity"))
    head = (f"{sev.emoji(f.get('severity'))} **{p} · {sev.label(f.get('severity'))} · "
            f"{f.get('category')}**, {f.get('title')}" if p else
            f"{sev.emoji(f.get('severity'))} **{sev.label(f.get('severity'))} · {f.get('category')}**, {f.get('title')}")
    parts = [head]
    if f.get("evidence"):
        parts.append(f"```\n{str(f['evidence'])[:600]}\n```")
    if f.get("body"):
        parts.append(f["body"])
    if f.get("fix"):
        parts.append(f"**Suggested fix**\n\n{f['fix']}")
    if f.get("suggestion") and f.get("line"):
        # One-click apply: GitHub turns this into a "Commit suggestion" button.
        parts.append(f"```suggestion\n{f['suggestion']}\n```")
    lenses = [str(x) for x in (f.get("lenses") or [])] or ([str(f.get("lens"))] if f.get("lens") else [])
    foot = f"lens: {', '.join(lenses)} · confidence: {f.get('confidence')}"
    parts.append(f"<sub>{foot}</sub>")
    additional = f.get("additional") or []
    if additional:
        locs = ", ".join(
            f"`{a.get('path')}:{a.get('line')}`" if a.get("line") else f"`{a.get('path')}`"
            for a in additional[:5]
        )
        parts.append(f"<sub>also reported at {locs}</sub>")
    if f.get("fingerprint"):
        # Hidden marker: lets the reaction webhook map thumbs up/down straight back to this finding.
        parts.append(fingerprint_marker(f["fingerprint"]))
    return "\n\n".join(parts)


_FP_RE = re.compile(r"<!--\s*buglens:fp=([0-9a-f]{16,64})(?:\.([0-9a-f]{16}))?\s*-->")


def _fp_secret() -> bytes:
    return (os.getenv("GITHUB_WEBHOOK_SECRET") or "").encode()


def sign_fingerprint(fingerprint: str) -> str:
    """HMAC tag for a fingerprint, carried in the hidden comment marker.

    Anyone who can comment on a PR can plant a `<!-- buglens:fp=… -->` marker; without a tag
    they could drive fake 👎 reactions and suppress real findings. The secret is the same one
    already required for the webhook, so this adds no configuration.
    """
    secret = _fp_secret()
    if not secret:
        return ""
    return hmac.new(secret, fingerprint.encode(), hashlib.sha256).hexdigest()[:16]


def fingerprint_marker(fingerprint: str) -> str:
    tag = sign_fingerprint(fingerprint)
    return f"<!-- buglens:fp={fingerprint}.{tag} -->" if tag else f"<!-- buglens:fp={fingerprint} -->"


def extract_fingerprint(body: str) -> str | None:
    """Fingerprint from a comment body, verified when a signing secret is configured.

    Unsigned markers are accepted only when no secret exists, so existing comments keep
    working and an unauthenticated deployment behaves as before.
    """
    m = _FP_RE.search(body or "")
    if not m:
        return None
    fingerprint, tag = m.group(1), m.group(2)
    expected = sign_fingerprint(fingerprint)
    if expected:
        if not tag or not hmac.compare_digest(tag, expected):
            return None
    return fingerprint


def _file_cache_key(fd: FileDiff, rules: Rules, lenses: list[L.Lens], context_salt: str = "") -> str:
    """Identity of a review unit: same added lines + same rules + same lenses → same answer.

    ``context_salt`` folds in repo context that can change the answer (the code-graph
    related block, repo rules) so a cached verdict is never reused against other context.
    """
    h = hashlib.sha256()
    h.update(fd.path.encode())
    for line in fd.added:
        h.update(f"{line.new_line}:{line.text}".encode())
    h.update(
        f"|{rules.min_severity}|{rules.consensus}|{rules.skip_nits}|"
        f"{','.join(sorted(l.name for l in lenses))}".encode()
    )
    if context_salt:
        h.update(b"|ctx:" + hashlib.sha256(context_salt.encode()).digest()[:8])
    return h.hexdigest()


def _diff_digest(picked: list[FileDiff], max_chars: int = 5000) -> str:
    """Compact added-lines digest of the whole PR, for the spec lens."""
    parts: list[str] = []
    used = 0
    for fd in picked:
        added = [f"+{l.new_line} | {l.text}" for l in fd.added][:60]
        if not added:
            continue
        block = f"### {fd.path}\n" + "\n".join(added)
        if used + len(block) > max_chars:
            break
        used += len(block)
        parts.append(block)
    return "\n".join(parts)


async def _resolve_ticket(owner: str, repo: str, pr_number: int, pr_info: github.PRInfo) -> str:
    """Ticket text for the spec lens. GraphQL linked issues first, keyword scan as fallback."""
    issues = await github.closing_issues(owner, repo, pr_number)
    if not issues:
        text = (pr_info.body or "") + "\n" + "\n".join(
            await github.get_pr_commit_messages(owner, repo, pr_number, 20)
        )
        slug = f"{owner}/{repo}"
        for ref in tickets.parse_closing_refs(text)[:3]:
            if ref["repo"] and ref["repo"] != slug:
                continue
            found = await github.get_issue(owner, repo, ref["number"])
            if found:
                issues.append({**found, "repo": slug})
    if not issues:
        return ""
    criteria = {i.get("number"): tickets.extract_acceptance_criteria(i.get("body") or "") for i in issues}
    return tickets.format_ticket(issues, criteria)


async def _run_spec_lens(ticket_block: str, diff_digest: str, rules: Rules) -> tuple[list[dict], str, dict]:
    """Check the diff against the ticket. Returns (findings, human summary, usage)."""
    messages = [
        {"role": "system", "content": prompts.SPEC_SYSTEM},
        {"role": "user", "content": prompts.build_spec_prompt(ticket_block, diff_digest)},
    ]
    try:
        result = await llm.complete_json(messages, max_tokens=1200)
    except Exception as exc:
        return [], "", {"error": str(exc)}
    data = result.data or {}
    overall = str(data.get("overall") or "unclear")
    criteria = data.get("criteria") or []
    unrelated = [str(u) for u in (data.get("unrelated_files") or [])][:5]

    findings: list[dict] = []
    for item in criteria:
        verdict = str(item.get("verdict") or "unclear").lower()
        if verdict not in ("not_addressed", "partial"):
            continue
        title = f"Ticket criterion {verdict.replace('_', ' ')}: {str(item.get('criterion') or '')[:80]}"
        norm = verify.normalize_finding(
            {
                "severity": "major" if verdict == "not_addressed" else "minor",
                "category": "merge",
                "line": None,
                "title": title,
                "body": str(item.get("note") or item.get("criterion") or ""),
                "fix": "Address the acceptance criterion or link the ticket that explains why it is out of scope.",
                "evidence": "",
                "confidence": 0.7,
            },
            "spec", "merge", "<pull-request>",
        )
        if norm:
            norm["ticket"] = True
            findings.append(norm)
        if len(findings) >= 3:
            break

    counts = {"addressed": 0, "partial": 0, "not_addressed": 0, "unclear": 0}
    for item in criteria:
        counts[str(item.get("verdict") or "unclear").lower()] = counts.get(str(item.get("verdict") or "unclear").lower(), 0) + 1
    summary = (f"Ticket: **{overall.replace('_', ' ')}** "
               f"({counts['addressed']} addressed, {counts['partial']} partial, "
               f"{counts['not_addressed']} not addressed, {counts['unclear']} unclear)")
    if unrelated:
        summary += f" · possibly unrelated: {', '.join(unrelated)}"
    return findings, summary, result.usage


def _review_effort(picked: list[FileDiff]) -> int:
    """1–5, derived from how much changed, the same "review effort" CodeRabbit shows."""
    chars = sum(fd.added_chars for fd in picked)
    return max(1, min(5, 1 + len(picked) // 3 + chars // 6000))


def _context_sources(tree: list[str], file_texts: dict[str, str], custom_lens_names: list[str],
                     graph_files: int = 0, ticket: bool = False, recall: bool = False) -> list[str]:
    """Which guardrails shaped this review, CodeRabbit's 'source line' idea, cheaply."""
    sources = [p for p in convention_paths(tree) if p in file_texts]
    sources += [f"custom lens: {n}" for n in custom_lens_names]
    if graph_files:
        sources.append(f"code graph ({graph_files} files)")
    if ticket:
        sources.append("linked ticket")
    if recall:
        sources.append("prior reviews")
    return sources[:8]


def _pre_merge_checks(picked: list[FileDiff], ticket_summary: str, linters_run: list[str],
                      secrets_flagged: bool, round_no: int, rules: Rules) -> list[dict]:
    """Deterministic pre-merge checklist as table rows. Asserts only what we know, never a guess.

    Each row is ``{"check", "status", "detail", "fix"}`` where status is pass | warn | info.
    Only ``warn`` rows carry a fix hint, so the table never tells a human to do busywork.
    """
    kinds = [walkthrough.classify(fd.path) for fd in picked]
    code = [k for k in kinds if k not in ("tests", "docs")]
    tests = kinds.count("tests")
    checks: list[dict] = []
    if code and not tests:
        checks.append({"check": "Tests", "status": "warn",
                       "detail": "code changed, no test files touched",
                       "fix": "add or update a test that covers the change"})
    elif tests:
        checks.append({"check": "Tests", "status": "pass",
                       "detail": f"{tests} test file(s) updated", "fix": ""})
    else:
        checks.append({"check": "Tests", "status": "info", "detail": "no code change to cover", "fix": ""})
    if getattr(rules, "check_ticket", True):
        checks.append({"check": "Linked issue", "status": "pass" if ticket_summary else "info",
                       "detail": ticket_summary or "none linked", "fix": ""})
    checks.append({"check": "Secrets", "status": "warn" if secrets_flagged else "pass",
                   "detail": "flagged by the scanner" if secrets_flagged else "none found",
                   "fix": "rotate the value and load it from a secret store" if secrets_flagged else ""})
    if linters_run:
        checks.append({"check": "Linters", "status": "info", "detail": ", ".join(linters_run), "fix": ""})
    if round_no > 1:
        checks.append({"check": "Re-review", "status": "info", "detail": f"round {round_no}", "fix": ""})
    return checks[:8]


def _walkthrough_md(res: ReviewResult, rules: Rules) -> list[str]:
    effort = "⬤" * res.review_effort + "◯" * (5 - res.review_effort)
    counts: dict[str, int] = {}
    for f in res.findings:
        counts[f.get("severity") or "info"] = counts.get(f.get("severity") or "info", 0) + 1
    sev_line = " · ".join(f"{sev.emoji(s)} {sev.label(s)} {c}" for s, c in counts.items()) or "none"
    lines = ["", "<details><summary>Walkthrough</summary>", ""]
    commits = f"`{(res.base_sha or '?')[:7]}` → `{(res.head_sha or '?')[:7]}`"
    lines.append(f"**Commits:** {commits}")
    lines.append(f"**Review effort:** {effort} ({res.review_effort}/5)")
    lines.append(f"**Findings:** {sev_line}")
    if res.ticket_summary:
        lines.append(f"**Ticket:** {res.ticket_summary}")
    if res.change_stack:
        lines += ["", f"**Change stack ({len(res.files)} file(s)):**", ""]
        for ch in res.change_stack[:12]:
            lines.append(f"- **{ch['title']}** ({ch['note']})")
    if getattr(rules, "pre_merge_checks", True) and res.pre_merge:
        lines += ["", "**Pre-merge checks:**", "", "| Check | Status | Detail |", "|---|---|---|"]
        for c in res.pre_merge:
            icon = _CHECK_STATUS.get(str(c.get("status")), "•")
            detail = str(c.get("detail") or "")
            fix = str(c.get("fix") or "")
            if fix:
                detail = f"{detail}. {fix}"
            lines.append(f"| {c.get('check')} | {icon} | {detail} |")
    if res.context_sources:
        lines += ["", f"**Context used:** {', '.join(f'`{s}`' for s in res.context_sources)}"]
    lines += ["", "</details>"]
    return lines


def render_summary_md(res: ReviewResult, rules: Rules) -> str:
    icon = {"looks-good": "✅", "needs-attention": "⚠️", "blocking": "❌"}.get(res.verdict, "🔍")
    lines = [
        MARKER,
        f"## {icon} BugLens Review",
        f"**Score:** `{res.score}/100` · **Findings:** `{len(res.findings)}` · **Verdict:** `{res.verdict}`",
    ]
    meta: list[str] = []
    if res.verdict_reason:
        meta.append(f"_{res.verdict_reason}_")
    if res.confidence:
        meta.append(f"**Confidence:** `{res.confidence}/100`")
    risks = []
    if res.merge_risk:
        risks.append(f"merge `{res.merge_risk}`")
    if res.security_risk:
        risks.append(f"security `{res.security_risk}`")
    if risks:
        meta.append("**Risk:** " + " · ".join(risks))
    if res.head_sha:
        meta.append(f"checked up to `{res.head_sha[:7]}`")
    if meta:
        lines.append("")
        lines.append(" · ".join(meta))
    tier_line = risktier.render_header_md(res.risk_tier, res.risk_reasons)
    if tier_line:
        lines += ["", tier_line]
    lines += ["", res.summary or ""]
    lines += _walkthrough_md(res, rules)
    if res.premortem:
        lines += premortem.render_md(res.premortem)
    if res.highlights:
        lines += ["", "### Highlights"]
        lines += [f"- {h}" for h in res.highlights]
    if res.findings:
        lines += ["", "### Findings"]
        for f in res.findings[: rules.max_comments]:
            loc = f"{f['path']}:{f['line']}" if f.get("line") else f["path"]
            lines.append(f"- {sev.emoji(f.get('severity'))} `{loc}`, {f.get('title')}")
    if res.positives:
        lines += ["", "### What's good"]
        lines += [f"- {p}" for p in res.positives]
    if res.skipped:
        lines += ["", "<details><summary>Files skipped</summary>", ""]
        lines += [f"- `{s['path']}`, {s['reason']}" for s in res.skipped[:25]]
        lines += ["", "</details>"]
    lines.append("")
    stats = _engine_stats(res.usage, res.wall_seconds)
    if stats:
        lines.append(f"<sub>{stats}</sub>")
    if res.engine_notes:
        lines.append(f"<sub>{' · '.join(res.engine_notes)}</sub>")
    lines.append(f"<sub>BugLens v2 · model `{res.model_used or 'n/a'}` · {len(res.files)} file(s) · lenses: {', '.join(res.lenses_run) or 'none'}</sub>")
    return "\n".join(lines)


def render_round_cap_md(rules: Rules) -> str:
    """Final note once a PR has exhausted its re-review budget. Separate marker so it never
    clobbers the last review comment, and upsert keeps it idempotent across pushes."""
    return "\n".join([
        MARKER_CAP,
        "## 🔁 BugLens: review round limit reached",
        f"This PR has had **{rules.max_rounds}** BugLens reviews. To keep the feedback loop "
        "from churning (agent-writes-code / agent-reviews-code is the documented cost sink), "
        "BugLens stops posting new review comments here.",
        "",
        "Re-push a new revision or open a fresh PR to reset the count, or raise `max_rounds` "
        "in `.buglens.yml`.",
    ])


def render_error_md(reason: str) -> str:
    """Honest note when the review could not run (e.g. the PR files could not be fetched).

    Own marker so it never clobbers the last review, and upsert keeps it idempotent. The
    reason is flattened and stripped of backticks: it comes from an exception message, which
    may embed model or API output, so it must not be able to break out of the code span.
    """
    short = " ".join(str(reason).split()).replace("`", "'")[:300]
    return "\n".join([
        MARKER_ERROR,
        "## ⚠️ BugLens could not review this pull request",
        f"**Reason:** `{short}`",
        "",
        "No findings were produced, so **this is not a clean review**. Push a new commit or "
        "comment `@buglens review` to retry.",
    ])


def _convergence_note(rules: Rules, round_no: int) -> str:
    return (
        f"**{rules.converge_rounds} rounds without convergence**: since round "
        f"{rules.converge_rounds} BugLens only reviews code *new* to each round, and nothing "
        "has changed since the last one. Reconsider the approach rather than re-requesting "
        "review."
    )


# ── PR flow ───────────────────────────────────────────────────────────────────
# ── admission control ─────────────────────────────────────────────────────────
# CONCURRENCY bounds the fan-out *inside* one review (files × lenses). These bound how many
# reviews run **at once in the process**, which is what actually protects the LLM quota and
# the SQLite writer when GitHub fires a burst of webhooks.
MAX_ACTIVE_REVIEWS = int(os.getenv("BUGLENS_MAX_CONCURRENT_REVIEWS", "8"))
FETCH_CONCURRENCY = int(os.getenv("BUGLENS_FETCH_CONCURRENCY", "8"))
_REVIEW_SLOTS = asyncio.Semaphore(MAX_ACTIVE_REVIEWS)
_repo_locks: dict[str, list] = {}          # slug -> [asyncio.Lock, refcount]
_GRAPH_CACHE: dict[str, tuple[str, dict, dict]] = {}   # "db\x00slug" -> (version, index, graph)
_GRAPH_CACHE_MAX = 8


def _repo_lock(slug: str) -> list:
    """Serializes reviews of the same repo, keeps the shared index/cache coherent and
    stops ten pushes to one repo from stampeding the LLM. Reference-counted so the dict
    does not retain a lock per repo forever."""
    entry = _repo_locks.get(slug)
    if entry is None:
        entry = _repo_locks[slug] = [asyncio.Lock(), 0]
    entry[1] += 1
    return entry


def _release_repo_lock(slug: str, entry: list) -> None:
    entry[1] -= 1
    if entry[1] <= 0:
        _repo_locks.pop(slug, None)


async def _fetch_texts(owner: str, repo: str, paths: list[str], ref: str,
                       sem: asyncio.Semaphore | None = None) -> dict[str, str]:
    """Fetch several file bodies concurrently (bounded). A failed fetch yields no entry.

    GitHub rate-limits per token, not per connection, so a PR's ~20-30 sequential fetches
    were pure latency for no benefit.
    """
    if not paths:
        return {}
    gate = sem or asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(path: str) -> tuple[str, str | None]:
        async with gate:
            try:
                return path, await github.get_file_content(owner, repo, path, ref)
            except Exception:
                return path, None

    out: dict[str, str] = {}
    for path, text in await asyncio.gather(*[one(p) for p in paths]):
        if text is not None:
            out[path] = text
    return out


async def _load_graph(slug: str, db: str) -> tuple[dict, dict]:
    """Deserialize a repo's code index + import graph, cached by (database, repo, version).

    ``store.get_index`` pulls and JSON-parses every row (megabytes for a large repo) on every
    review. The version is a write counter bumped by every index write, so a cache entry can
    never be stale. The database path is part of the key: the same repo slug in two stores
    (tests, or a second deployment) is not the same index.
    """
    version = await store.index_version(slug, db)
    key = f"{db}\x00{slug}"
    hit = _GRAPH_CACHE.get(key)
    if hit and hit[0] == version:
        return hit[1], hit[2]
    index = await store.get_index(slug, db)
    graph = indexer.build_graph(index)
    if len(_GRAPH_CACHE) >= _GRAPH_CACHE_MAX:
        _GRAPH_CACHE.pop(next(iter(_GRAPH_CACHE)))
    _GRAPH_CACHE[key] = (version, index, graph)
    return index, graph


async def review_pr(owner: str, repo: str, pr_number: int, *,
                    base_sha: str | None = None, head_sha: str | None = None,
                    dry_run: bool = False, delivery_id: str | None = None,
                    db: str | None = None, pr_info: github.PRInfo | None = None,
                    rules: Rules | None = None, force: bool = False) -> ReviewResult:
    """Admission-controlled wrapper: at most MAX_ACTIVE_REVIEWS reviews in flight, and one
    at a time per repo. Args are passed straight through to ``_review_pr``."""
    slug = repo if "/" in repo else f"{owner}/{repo}"
    async with _REVIEW_SLOTS:
        if MAX_ACTIVE_REVIEWS > 1:
            entry = _repo_lock(slug)
            try:
                async with entry[0]:
                    return await _review_pr(owner, repo, pr_number, base_sha=base_sha,
                                            head_sha=head_sha, dry_run=dry_run,
                                            delivery_id=delivery_id, db=db, pr_info=pr_info,
                                            rules=rules, force=force)
            finally:
                _release_repo_lock(slug, entry)
        return await _review_pr(owner, repo, pr_number, base_sha=base_sha,
                                head_sha=head_sha, dry_run=dry_run,
                                delivery_id=delivery_id, db=db, pr_info=pr_info,
                                rules=rules, force=force)


async def _review_pr(owner: str, repo: str, pr_number: int, *, base_sha: str | None = None,
                    head_sha: str | None = None, dry_run: bool = False,
                    delivery_id: str | None = None, db: str | None = None,
                    pr_info: github.PRInfo | None = None,
                    rules: Rules | None = None, force: bool = False) -> ReviewResult:
    await store.init(db)
    t0 = time.monotonic()
    # Resolve the store path once. `db` is optional for callers, but every `if db` gate below
    # decides whether persisted context is *used* (round counting, recall, suppression, the
    # cross-push ledger). A webhook call passes none, so leaving it None silently disabled all
    # of them in production while the tests, which always pass a path, stayed green.
    db = db or store.db_path()
    # One canonical key for everything we persist: owner/repo. The bare repo name is
    # GitHub's URL param, not an identity (two orgs can both own a repo named "api").
    slug = repo if "/" in repo else f"{owner}/{repo}"

    if delivery_id and not await store.record_delivery(delivery_id, slug, pr_number, head_sha or "", db):
        return ReviewResult(status="skipped", repo=repo, pr=pr_number, summary="duplicate delivery")

    if pr_info is None:
        if not github.configured():
            return ReviewResult(status="dry-run", repo=repo, pr=pr_number, summary="GITHUB_TOKEN not configured")
        pr_info = await github.get_pr(owner, repo, pr_number)
    head_sha = head_sha or pr_info.head_sha
    base_sha = base_sha or pr_info.base_sha

    # Rules come from the **base** commit so a PR cannot rewrite how it is reviewed;
    # config_cache keeps the two passes (root, then nested) to one fetch per file.
    explicit_rules = rules is not None
    config_cache: dict[str, str | None] = {}
    if rules is None:
        rules = await load_rules_for_pr(owner, repo, base_sha or head_sha, None, config_cache)

    if rules.skip_drafts and pr_info.draft:
        return ReviewResult(status="skipped", repo=repo, pr=pr_number, head_sha=head_sha, summary="draft PR skipped")

    # `force` (an on-demand @mention re-review) gets a unique dedupe key so the
    # same revision can be reviewed again without disabling idempotency globally.
    run_key = head_sha if not force else f"{head_sha}#{uuid.uuid4().hex[:8]}"
    run_id = await store.claim_run(slug, pr_number, run_key, base_sha, db)
    if run_id is None:
        return ReviewResult(status="skipped", repo=repo, pr=pr_number, head_sha=head_sha, summary="revision already reviewed")

    # Re-review discipline: count this PR's rounds and stop before the agent↔agent loop runs
    # forever. `round_no` includes the run just claimed, so a fresh PR is round 1.
    round_no = await store.count_rounds(slug, pr_number, db) if db else 1
    if rules.max_rounds and round_no > rules.max_rounds:
        res = ReviewResult(status="capped", repo=repo, pr=pr_number, head_sha=head_sha,
                           round_no=round_no, verdict="capped", score=100,
                           summary=f"Review round cap reached ({rules.max_rounds}).")
        res.wall_seconds = time.monotonic() - t0
        if not dry_run and github.configured():
            try:
                await github.upsert_marked_comment(owner, repo, pr_number, MARKER_CAP,
                                                   render_round_cap_md(rules))
                res.posted = True
            except Exception:
                res.posted = False
        await store.finish_run(run_id, "capped", 0, 0, res.posted, db)
        return res

    try:
        raw_files = await github.get_pr_files(owner, repo, pr_number) if github.configured() else []
    except Exception as exc:
        # A review that could not run must never look like a review that found nothing. When the
        # files cannot be fetched (403/429/5xx), say so on the PR. Best-effort: a hard rate limit
        # will also refuse this post, so the failure is logged regardless.
        res = ReviewResult(status="error", repo=repo, pr=pr_number, head_sha=head_sha,
                           summary=f"could not fetch files: {exc}")
        res.wall_seconds = time.monotonic() - t0
        if not dry_run and github.configured():
            try:
                await github.upsert_marked_comment(owner, repo, pr_number, MARKER_ERROR,
                                                   render_error_md(exc))
                res.posted = True
            except Exception as post_exc:
                log.warning("could not post could-not-run notice for %s#%s: %s",
                            slug, pr_number, post_exc)
        await store.finish_run(run_id, "error", 0, 0, res.posted, db)
        return res

    # Second pass: now that we know the changed files, layer the nearest nested config.
    if not explicit_rules and github.configured() and raw_files:
        rules = await load_rules_for_pr(
            owner, repo, base_sha or head_sha,
            [f.get("filename", "") for f in raw_files], config_cache,
        )

    # Linked ticket(s): GraphQL first (default-branch aware), keyword scan as fallback.
    ticket_block = ""
    if rules.check_ticket and github.configured():
        try:
            ticket_block = await _resolve_ticket(owner, repo, pr_number, pr_info)
        except Exception:
            ticket_block = ""

    statuses = {f.get("filename", ""): f.get("status", "modified") for f in raw_files}
    parsed = []
    for f in raw_files:
        fd = parse_patch(f.get("patch", ""), f.get("filename", ""), f.get("status", "modified"))
        fd.language = language_of(fd.path)
        parsed.append(fd)
    picked, skipped = choose_paths(parsed, rules.ignore_paths, REVIEWABLE_EXTS, rules.max_files)
    oversized = [fd for fd in picked if fd.added_chars > rules.max_file_chars]
    if oversized:
        picked = [fd for fd in picked if fd.added_chars <= rules.max_file_chars]
        skipped += [{"path": fd.path, "reason": f"over {rules.max_file_chars} char budget"} for fd in oversized]

    # Convergence discipline: past `converge_rounds`, only review files whose reviewed content
    # changed since an earlier round. Deterministic (content hash), zero model calls.
    converging = bool(rules.converge_rounds) and round_no > rules.converge_rounds
    if converging and picked and db:
        seen = await store.reviewed_contents(slug, pr_number, round_no, db)
        if seen:
            fresh: list[FileDiff] = []
            for fd in picked:
                if _content_sha(fd) in seen.get(fd.path, ()):
                    skipped.append({"path": fd.path, "reason": "unchanged since a prior review round"})
                else:
                    fresh.append(fd)
            picked = fresh
    if converging and not picked:
        res = ReviewResult(status="converged", repo=repo, pr=pr_number, head_sha=head_sha,
                           round_no=round_no, converging=True, verdict="no-op", score=100,
                           summary=_convergence_note(rules, round_no))
        res.wall_seconds = time.monotonic() - t0
        if not dry_run and github.configured():
            try:
                await github.upsert_marked_comment(owner, repo, pr_number, MARKER,
                                                   render_summary_md(res, rules))
                res.posted = True
            except Exception:
                res.posted = False
        await store.finish_run(run_id, "converged", 100, 0, res.posted, db)
        return res

    tree: list[str] = []
    file_texts: dict[str, str] = {}
    if github.configured():
        tree = await github.get_repo_tree(owner, repo, head_sha)
        head_files = {fd.path for fd in picked}
        wanted = [p for p in list(convention_paths(tree)) + sorted(head_files) if p not in file_texts]
        file_texts.update(await _fetch_texts(owner, repo, wanted, head_sha))
    ctx = build_context(tree, file_texts, head_sha)
    # Pull the bodies of the local files this change imports, for cross-file reasoning.
    if github.configured() and ctx.snippets:
        deps = await _fetch_texts(owner, repo, list(ctx.snippets)[:6], head_sha)
        for target, text in deps.items():
            ctx.snippets[target] = text[:2000]
            ctx.symbols[target] = extract_symbols(text, language_of(target))
            file_texts[target] = text              # also feed the code graph
    context_block = ctx.prompt_block()
    if ticket_block:
        context_block = (context_block + "\n\n" + ticket_block)[:9000]

    # Persistent code graph: index the files we already have, then pull callers/tests.
    related_block = ""
    graph_files = 0
    ripple_items: list[dict] = []
    if db and file_texts:
        try:
            changed_paths = [fd.path for fd in picked]
            await indexer.index_files(slug, file_texts, db)
            index, graph = await _load_graph(slug, db)
            graph_files = len(index)
            related = indexer.related_files(index, graph, changed_paths)
            # Fetch a bounded slice of the related files themselves so their *references*
            # enter the index. Without their bodies the symbol graph only sees the changed
            # files and ripple analysis has nothing to ripple to.
            if github.configured():
                todo = [p for p in related if p not in file_texts][:RIPPLE_FETCH]
                if todo:
                    fetched = await _fetch_texts(owner, repo, todo, head_sha)
                    if fetched:
                        file_texts.update({p: t[:4000] for p, t in fetched.items()})
                        await indexer.index_files(slug, file_texts, db)
                        index, graph = await _load_graph(slug, db)
                        graph_files = len(index)
            related_block = indexer.context_pack(index, related)
            ripple_items = symbols.ripple(index, symbols.changed_defs(picked), changed_paths)
            ripple_text = symbols.ripple_block(ripple_items)
            if ripple_text:
                related_block = (related_block + "\n\n" + ripple_text)[:8000]
        except Exception:
            related_block = ""
            ripple_items = []

    # Prior-review / prior-PR recall: never repeat ourselves, note what stayed open.
    recall_text = ""
    if db and picked:
        try:
            changed_paths = [fd.path for fd in picked]
            await recall.remember_history(slug, changed_paths, head_sha, db)
            recall_text = await recall.recall_block(slug, changed_paths, db)
        except Exception:
            recall_text = ""
    if recall_text:
        context_block = (context_block + "\n\n" + recall_text)[:11000]

    lens_pool = list(L.LENSES)
    custom_lens_names: list[str] = []
    if github.configured():
        lens_files = [p for p in tree if p.replace("\\", "/").startswith(".buglens/lenses/") and p.endswith(".md")][:10]
        if lens_files:
            cfg = await _fetch_texts(owner, repo, lens_files, head_sha)
            custom = L.load_custom_lenses(cfg)
            lens_pool += custom
            custom_lens_names = [l.name for l in custom]

    prior_good = await store.confirmed_good(slug, 8, db) if db else []

    # Learning+: turn accumulated reactions into a bounded advisory and a demoted-lens set.
    learn_scopes: list[dict] = []
    demoted: set[str] = set()
    if db:
        try:
            lens_stats = await store.lens_outcome_stats(slug, db)
            learn_scopes = await store.dismissal_scopes(slug, db)
            learn_text = learn.learn_block(learn_scopes, lens_stats)
            if learn_text:
                context_block = (context_block + "\n\n" + learn_text)[:12000]
            demoted = set(learn.lens_ranking(lens_stats)[0])
        except Exception:
            learn_scopes, demoted = [], set()

    units = objects.build_units(picked)
    prod_used: list = []
    if rules.external_context:
        try:
            ext = await integrations.collect(rules.external_context, slug, ticket_block[:2000])
        except Exception:
            ext = {"tickets": [], "signals": [], "used": []}
        parts: list = []
        ext_tickets = ext.get("tickets") or []
        if ext_tickets:
            mapped = prodcontext.ticket_block(" ".join(str(t) for t in ext_tickets), units)
            if mapped:
                parts.append(mapped)
        signals = prodcontext.signals_block(ext.get("signals") or [])
        if signals:
            parts.append(signals)
        if parts:
            context_block = (context_block + "\n\n" + "\n".join(parts))[:13000]
            prod_used = [str(p) for p in (ext.get("used") or [])]

    sem = asyncio.Semaphore(CONCURRENCY)
    all_findings: list[dict] = []
    positives: list[str] = []
    usage: dict = {}
    lenses_run: set[str] = set()

    for fd in picked:
        lenses = L.select_lenses(fd.path, fd.language, rules.focus, pool=lens_pool)
        if demoted:
            lenses = learn.order_lenses(lenses, demoted)
        if not lenses:
            continue
        cache_key = _file_cache_key(
            fd, rules, lenses, (related_block or "")[:2000] + ticket_block[:500]
        )
        cached = await store.get_file_cache(slug, cache_key, path=db) if db else None
        if cached is not None:
            all_findings.extend(cached)
            lenses_run.update(l.name for l in lenses)
            continue
        f, p, u, names = await _review_file(fd, lenses, rules, context_block, related_block, prior_good, sem)
        if db:
            await store.put_file_cache(slug, cache_key, f, db)
        all_findings.extend(f)
        positives.extend(p)
        usage = _merge_usage(usage, u)
        lenses_run.update(names)

    # Deterministic secret scan, no model, no doubt; runs on every added line.
    for fd in picked:
        all_findings.extend(secretscan.scan(fd))
    if any(f.get("deterministic") for f in all_findings):
        lenses_run.add("secrets")

    # Deterministic linters/SAST: the repo's own tools, offline, free, and never
    # vote-suppressed. Off unless `.buglens.yml` opts in (`linters: auto` or a list).
    # `scan` shells out synchronously with a hard timeout, so it runs on a worker thread: on the
    # event loop it would stall every other request for up to the linter timeout per file.
    lint_findings, linters_run = await asyncio.to_thread(
        adapters.scan, file_texts, {fd.path: fd for fd in picked}, getattr(rules, "linters", [])
    )
    if lint_findings:
        all_findings.extend(lint_findings)
        lenses_run.add("lint")

    # Cross-file inconsistency: a changed signature that now demands a new argument while
    # in-repo callers still exist. Still a candidate, the verify gate has to keep it.
    ripple_findings = symbols.ripple_findings(ripple_items)
    if ripple_findings:
        all_findings.extend(ripple_findings)
        lenses_run.add("merge")

    # PR-level merge gate
    if picked:
        mf, mp, mu = await _run_merge_lens(picked, rules, pr_info, statuses)
        all_findings.extend(mf)
        positives.extend(mp)
        usage = _merge_usage(usage, mu)
        lenses_run.add("merge")

    # Ticket/spec validation (CodeRabbit "linked issues"): does the diff do what it claims?
    ticket_summary = ""
    if ticket_block and picked:
        sf, ticket_summary, su = await _run_spec_lens(ticket_block, _diff_digest(picked), rules)
        all_findings.extend(sf)
        usage = _merge_usage(usage, su)
        lenses_run.add("spec")

    files_by_path = {fd.path: fd for fd in picked}
    if picked:
        shadow, shadow_map = _pr_shadow(picked, statuses)
        files_by_path["<pull-request>"] = shadow
        all_findings = _reanchor_merge_findings(all_findings, shadow_map)

    # suppress fingerprints this team has repeatedly rejected
    rules._suppressed = await store.suppressed(slug, 2, db) if db else set()  # type: ignore[attr-defined]

    all_findings, duplicate_findings = dedupe.dedupe(all_findings)
    verified = await verify.verify_all(all_findings, files_by_path, rules)

    # Verifier rationale persistence: explain why candidates were dropped, not just how many.
    if db and verified.dropped:
        try:
            await store.save_drops(run_id, slug, verified.dropped, db)
        except Exception:
            pass

    # cross-push dedupe: never re-post a finding already posted on this repo
    posted_before = await store.posted_fingerprints(slug, db) if db else set()
    fresh = [f for f in verified.kept if f["fingerprint"] not in posted_before]
    if rules.skip_nits:
        fresh = [f for f in fresh if f["severity"] != "nit"]
    kept, over_budget = sev.budget(fresh, rules.max_comments, rules.min_severity)
    kept, capped = sev.apply_lens_caps(kept, getattr(rules, "lens_caps", None))
    over_budget = over_budget + capped
    cap_counts: dict[str, int] = {}
    for f in capped:
        lens = str(f.get("lens") or "unknown")
        cap_counts[lens] = cap_counts.get(lens, 0) + 1
    for f in kept:
        f["symbol"] = objects.symbol_for(f.get("path"), f.get("line"), units)

    s = await _summarise(
        kept, [fd.path for fd in picked], sorted(lenses_run), skipped
    )
    summary, score, verdict = s["summary"], s["score"], s["verdict"]
    highlights, s_positives = s["highlights"], s["positives"]
    usage = _merge_usage(usage, s["usage"])

    # Depth tier: a new route, a migration, an auth/security path or a large diff is where "works
    # on my machine" turns into a 03:00 page, so those PRs get one extra bounded pass. The triggers
    # are deterministic and the chosen tier is always named in the footer, never applied silently.
    depth_triggers_fired = premortem.triggers(picked) if getattr(rules, "depth_triggers", True) else []
    depth_tier = premortem.tier(bool(getattr(rules, "depth_triggers", True)), depth_triggers_fired)
    pm_items: list[dict] = []
    if depth_tier == "deep" and getattr(rules, "premortem", True) and picked:
        proposed, pm_usage = await _premortem(kept, [fd.path for fd in picked], depth_triggers_fired)
        usage = _merge_usage(usage, pm_usage)
        pm_items = premortem.select(proposed, picked)

    # The risk tier is advisory and free: it names how closely a human should read, from signals
    # already on hand (findings, path, test presence). It never blocks and never changes the
    # passes that ran; that is the depth tier above. A surprise (a change that does not match the
    # kind of work the PR claims to be) raises it and is named with the rest.
    surprises = (surprise.detect(surprise.pr_kind(pr_info.title), picked)
                 if getattr(rules, "surprise_detector", True) else [])
    risk = (risktier.tier(kept, picked, secrets_flagged="secrets" in lenses_run, surprises=surprises)
            if getattr(rules, "risk_tiers", True) else {"level": "", "reasons": []})

    # A review where every model call failed must not report itself as a clean pass. The
    # old behaviour posted "looks-good / 100" and the team merged on a review that never ran.
    calls, failures = _lens_health(usage)
    degraded = bool(calls) and failures >= calls
    if degraded:
        res_status = "incomplete"
        verdict = "incomplete"
        score = 0
        summary = (
            "The review could not run: every model call failed "
            f"({failures}/{calls}). This is **not** a clean review, re-run it with "
            "`@buglens review` once the provider is reachable."
        )
        res_note = usage.get("error_sample") or ""
        if res_note:
            summary += f"\n\nLast error: `{res_note}`"
    else:
        res_status = "ok"

    if converging:
        summary = (summary + "\n\n" + _convergence_note(rules, round_no)).strip()

    res = ReviewResult(
        status=res_status, summary=summary, score=score, verdict=verdict, findings=kept,
        positives=(positives + s_positives)[:6], highlights=highlights,
        files=[fd.path for fd in picked], skipped=skipped, lenses_run=sorted(lenses_run),
        dropped=len(verified.dropped) + len(over_budget) + len(duplicate_findings), model_used=llm.model_name(),
        usage=usage, repo=repo, pr=pr_number, head_sha=head_sha, base_sha=base_sha,
        context_sources=_context_sources(tree, file_texts, custom_lens_names,
                                         graph_files=graph_files, ticket=bool(ticket_block),
                                         recall=bool(recall_text)),
        review_effort=_review_effort(picked),
        ticket_summary=ticket_summary,
        wall_seconds=time.monotonic() - t0,
        round_no=round_no,
        converging=converging,
        engine_notes=_engine_notes(rules, verified, all_findings, bool(ticket_block),
                                   graph_files, bool(recall_text), calls, failures, converging,
                                   linters_run=linters_run, ripple=len(ripple_items),
                                   learned_scopes=len(learn_scopes),
                                   demoted_lenses=len(demoted),
                                   depth_tier=depth_tier if getattr(rules, "depth_triggers", True) else "",
                                   depth_triggers=depth_triggers_fired,
                                   premortem_items=len(pm_items)),
        change_stack=walkthrough.build_change_stack(picked, kept),
        pre_merge=_pre_merge_checks(picked, ticket_summary, linters_run,
                                    "secrets" in lenses_run, round_no, rules),
        verdict_reason=s["verdict_reason"], confidence=s["confidence"],
        merge_risk=s["merge_risk"], security_risk=s["security_risk"],
        premortem=pm_items,
        risk_tier=risk["level"], risk_reasons=risk["reasons"],
    )

    try:                                   # findings are best-effort; never cost the review
        await store.save_findings(run_id, kept, db)
    except Exception:
        pass

    obj_note = objects.unit_summary(units)
    if obj_note:
        res.engine_notes.append(f"code objects: {obj_note}")
    if duplicate_findings:
        res.engine_notes.append(f"merged {len(duplicate_findings)} duplicate finding(s)")
    for lens in sorted(cap_counts):
        res.engine_notes.append(f"lens caps: {lens} {cap_counts[lens]}")
    if prod_used:
        res.engine_notes.append(f"external context: {', '.join(sorted(set(prod_used)))}")
    if not (res.verdict_reason or res.confidence or res.merge_risk or res.security_risk):
        res.engine_notes.append("verdict meta: not provided by the model")

    if db:                                 # ledger of what this round actually reviewed
        try:
            await store.record_round_files(slug, pr_number, round_no,
                                           [(fd.path, _content_sha(fd)) for fd in picked], db)
        except Exception:
            pass

    if degraded:
        # Tell the team the bot is broken instead of staying silent: a green check on a
        # review that never ran is the worst possible outcome. Rendering needs no model.
        if not dry_run and github.configured():
            try:
                await github.upsert_marked_comment(owner, repo, pr_number, MARKER,
                                                   render_summary_md(res, rules))
                res.posted = True
            except Exception:
                res.posted = False
        await store.finish_run(run_id, verdict, score, len(kept), res.posted, db)
        return res

    if dry_run or not github.configured():
        res.status = "dry-run"
        await store.finish_run(run_id, verdict, score, len(kept), False, db)
        return res

    acknowledged, retired = await _reconcile_prior_comments(owner, repo, pr_number, head_sha,
                                                            res, rules, db)
    if acknowledged:
        res.engine_notes.append(f"addressed findings acknowledged: {acknowledged}")
    if retired:
        res.engine_notes.append(f"stale comments retired: {retired}")

    res.posted = await _post(owner, repo, pr_number, head_sha, res, rules, db)
    if res.posted:
        await store.mark_posted(run_id, db)
    if rules.post_check_run and head_sha:
        # A check run can gate merges only when the operator asked for it. `failure` is
        # never reachable unless `required_status_check` is true, and a 403 (a PAT cannot
        # create check runs) is swallowed, so the bot stays non-blocking by default.
        try:
            published = await _publish_check_run(owner, repo, head_sha, res, rules, picked)
            if published:
                res.engine_notes.append(f"check run: {published} annotation(s)")
        except Exception:
            pass  # a status check is a nicety, never a reason to fail the review
    await store.finish_run(run_id, verdict, score, len(kept), res.posted, db)
    return res


async def _publish_check_run(owner: str, repo: str, head_sha: str, res: ReviewResult,
                             rules: Rules, picked: list) -> int:
    """Best-effort check run with annotations; returns the annotation count published.

    Annotations are chunked (GitHub accepts 50 per create/update call) and appended by later
    updates. `failure` only ever comes from `checks.conclusion_for`, which needs
    `required_status_check`, so the check never blocks by default.
    """
    name = getattr(rules, "check_run_name", None) or "BugLens"
    required = bool(getattr(rules, "required_status_check", False))
    base = str(getattr(rules, "check_run_conclusion", "neutral") or "neutral").lower()
    conclusion = checks.conclusion_for(str(res.verdict or ""), base, required)
    cap = int(getattr(rules, "check_run_max_annotations", 200) or 0)
    mode = str(getattr(rules, "check_run_input", "pr_metadata") or "pr_metadata")
    text = checks.input_text(mode, res, picked)
    findings = res.findings or []

    if str(getattr(rules, "check_run_grouping", "single") or "single") == "per_lens":
        return await _publish_lens_checks(owner, repo, head_sha, res, name, conclusion,
                                          cap, text, findings)

    annotations, _dropped = checks.annotations_from_findings(findings, cap)
    chunks = checks.annotation_chunks(annotations)
    run = await github.create_check_run(
        owner, repo, head_sha, name, conclusion,
        f"{name}: {res.verdict} ({res.score}/100)", res.summary,
        text=text, annotations=chunks[0] if chunks else None,
    )
    run_id = (run or {}).get("id")
    for chunk in chunks[1:]:
        if not run_id:
            break
        await github.update_check_run(owner, repo, int(run_id), conclusion=conclusion,
                                      annotations=chunk)
    return len(annotations)


async def _publish_lens_checks(owner: str, repo: str, head_sha: str, res: ReviewResult,
                               name: str, conclusion: str, cap: int, text: str,
                               findings: list) -> int:
    """One check run per lens (`check_run_grouping: per_lens`); returns annotations published."""
    by_lens: dict[str, list] = {}
    for f in findings:
        by_lens.setdefault(str(f.get("lens") or "general"), []).append(f)
    published = 0
    for lens, group in sorted(by_lens.items()):
        annotations, _dropped = checks.annotations_from_findings(group, cap)
        chunks = checks.annotation_chunks(annotations)
        lens_name = f"{name}: {lens}"
        run = await github.create_check_run(
            owner, repo, head_sha, lens_name, conclusion,
            f"{lens}: {len(group)} finding(s)", res.summary,
            text=text, annotations=chunks[0] if chunks else None,
        )
        run_id = (run or {}).get("id")
        for chunk in chunks[1:]:
            if not run_id:
                break
            await github.update_check_run(owner, repo, int(run_id), conclusion=conclusion,
                                          annotations=chunk)
        published += len(annotations)
    return published


async def _post(owner: str, repo: str, pr_number: int, head_sha: str, res: ReviewResult,
                rules: Rules, db: str | None = None) -> bool:
    inline = []
    if not rules.summary_only:
        for f in res.findings:
            if not f.get("line") or f["path"] == "<pull-request>":
                continue
            inline.append({
                "path": f["path"], "line": int(f["line"]), "side": "RIGHT",
                "body": render_finding_md(f),
            })
    # Requesting changes is opt-in and only for a blocking verdict, so the default stays
    # COMMENT for every repo that did not ask to be gated.
    event = "REQUEST_CHANGES" if (getattr(rules, "request_changes", False) and res.verdict == "blocking") else "COMMENT"
    try:
        if inline or event != "COMMENT":
            review = await github.post_review(owner, repo, pr_number, head_sha,
                                              render_summary_md(res, rules), inline, event=event)
            await _record_posted_comments(owner, repo, pr_number, res, review, db)
            return True
        await github.upsert_marked_comment(owner, repo, pr_number, MARKER, render_summary_md(res, rules))
        return True
    except Exception:
        try:
            await github.upsert_marked_comment(owner, repo, pr_number, MARKER, render_summary_md(res, rules))
            return True
        except Exception:
            return False


async def _record_posted_comments(owner: str, repo: str, pr_number: int, res: ReviewResult,
                                  review: dict | None, db: str | None) -> None:
    """Persist which inline comment id belongs to which finding fingerprint.

    The id is what a later push needs to retire or answer a stale thread. Strictly best-effort:
    the review is already posted, so nothing here may raise.
    """
    # `db` is optional everywhere in this module: a webhook call passes none and the store
    # falls back to BUGLENS_DB. Never gate on it here, that silently disabled the ledger.
    if not res.findings or not isinstance(review, dict) or not review.get("id"):
        return
    try:
        comments = await github.list_review_comments(owner, repo, pr_number, int(review["id"]))
        by_fp: dict[str, dict] = {}
        for c in comments:
            fp = extract_fingerprint(c.get("body") or "")
            if fp:
                by_fp[fp] = c
        items = []
        for f in res.findings:
            c = by_fp.get(f.get("fingerprint") or "")
            if not c:
                continue
            items.append({
                "fingerprint": f["fingerprint"], "path": f.get("path"), "line": f.get("line"),
                "comment_id": c.get("id"), "node_id": c.get("node_id"),
            })
        if items:
            await store.save_posted_comments(f"{owner}/{repo}", pr_number, res.head_sha, items, db)
    except Exception:
        pass


MAX_RECONCILE = 12


async def _reconcile_prior_comments(owner: str, repo: str, pr_number: int, head_sha: str,
                                    res: ReviewResult, rules: Rules,
                                    db: str | None) -> tuple[int, int]:
    """Close the loop on older comments a new push made stale.

    Returns ``(acknowledged, retired)``. A comment whose finding is still open is left alone.
    A comment whose finding is gone is only *acknowledged* when the quoted evidence is really
    absent at the new head (the same rule ``measure_resolution`` uses), otherwise it is
    collapsed as outdated when the operator opted in. Best-effort: any failure here contributes
    nothing and never fails the review.
    """
    if not head_sha or not github.configured():
        return (0, 0)
    slug = f"{owner}/{repo}"
    try:
        prior = await store.posted_comments(slug, pr_number, db)
    except Exception:
        return (0, 0)
    if not prior:
        return (0, 0)
    current = {f.get("fingerprint") for f in res.findings}
    stale = [row for row in prior if row.get("fingerprint") not in current]
    if not stale:
        return (0, 0)
    try:
        known = {f.get("fingerprint"): f for f in await store.findings_for_pr(slug, pr_number, db)}
    except Exception:
        known = {}
    acknowledged = retired = 0
    cache: dict[str, str | None] = {}
    failed: set[str] = set()
    for row in stale[:MAX_RECONCILE]:
        src = known.get(row.get("fingerprint")) or {}
        path = row.get("path") or src.get("path") or ""
        evidence = str(src.get("evidence") or "")
        if not path:
            continue
        if path not in cache and path not in failed:
            try:
                cache[path] = await github.get_file_content(owner, repo, path, head_sha)
            except Exception:
                failed.add(path)
        if path in failed:
            continue
        # Only a finding whose evidence is really gone may be touched. A finding that is
        # merely absent from this round (budget, dedup, a quieter lens) is still open, and
        # collapsing it would hide a real problem. Never guess from a failed fetch.
        if not _evidence_gone(cache.get(path), evidence):
            continue
        if row.get("comment_id") and getattr(rules, "acknowledge_resolved", True):
            try:
                await github.reply_review_comment(owner, repo, pr_number, int(row["comment_id"]),
                                                  STALE_REPLY.format(sha=head_sha[:7]))
                acknowledged += 1
                await store.forget_posted_comment(slug, pr_number, row["fingerprint"], db)
                continue
            except Exception:
                pass
        if not getattr(rules, "stale_comments", True) or not row.get("node_id"):
            continue
        try:
            if await github.minimize_comment(row["node_id"]):
                retired += 1
                await store.forget_posted_comment(slug, pr_number, row["fingerprint"], db)
        except Exception:
            pass
    return (acknowledged, retired)


def _evidence_gone(text: str | None, evidence: str) -> bool:
    """The one rule for "was this fixed": the file is gone, or the quoted evidence is gone."""
    ev = (evidence or "").strip()
    return text is None or (bool(ev) and ev not in text)


def convention_paths(tree: list[str]) -> list[str]:
    wanted = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".buglens.yml", ".buglens.yaml",
              ".github/buglens.yml", "STYLE.md", "docs/STYLE.md", "README.md",
              ".cursorrules", ".github/copilot-instructions.md")
    picked = [p for p in tree if p in wanted]
    picked += [p for p in tree if p.replace("\\", "/").startswith(".cursor/rules/") and p.endswith(".mdc")][:4]
    return picked


async def load_rules_for_pr(owner: str, repo: str, ref: str,
                            changed_paths: list[str] | None = None,
                            cache: dict | None = None) -> Rules:
    """Repo rules as of `ref`.

    `ref` should be the PR **base** commit, never the head: a PR must not be able to change
    how it is reviewed. Configs are discovered upward from the changed files (root first,
    nearest dir last → nearest wins).
    """
    configs: dict[str, str] = {}
    if github.configured():
        for name in rules_mod.config_candidates(changed_paths):
            if cache is not None and name in cache:
                text = cache[name]
            else:
                text = await github.get_file_content(owner, repo, name, ref)
                if cache is not None:
                    cache[name] = text
            if text:
                configs[name] = text
    return load_rules(configs)


# ── IDE snippet flow ──────────────────────────────────────────────────────────
def _snippet_to_filediff(code: str, language: str, filename: str) -> FileDiff:
    fd = FileDiff(path=filename or "snippet", language=language, status="modified")
    for i, line in enumerate(code.splitlines(), start=1):
        fd.lines.append(DiffLine(ADD, line, i, None, 0, i))
    return fd


async def review_snippet(code: str, language: str, filename: str = "snippet",
                         focus: list[str] | None = None, rules: Rules | None = None,
                         db: str | None = None, repo: str | None = None,
                         related: dict[str, str] | None = None,
                         imports: list[str] | None = None) -> ReviewResult:
    """Review one file from the IDE.

    When the client tells us which repo it is (and optionally hands over a few related
    files it already read), we index them and pull the same code-graph + prior-review
    context the PR bot uses, so IDE findings line up with PR findings.
    """
    rules = rules or load_rules()
    if focus:
        rules.focus = focus
    db = db or store.db_path()
    t0 = time.monotonic()
    fd = _snippet_to_filediff(code, language, filename)

    related_block = ""
    if repo and db:
        try:
            await store.init(db)
            files = {fd.path: code}
            files.update({p: t for p, t in (related or {}).items() if t})
            await indexer.index_files(repo, files, db)
            index, graph = await _load_graph(repo, db)
            related_block = indexer.context_pack(
                index, indexer.related_files(index, graph, [fd.path])
            )
            if imports:
                related_block = "Declared imports: " + ", ".join(imports[:20]) + "\n\n" + related_block
        except Exception:
            related_block = ""

    lenses = L.select_lenses(fd.path, language, rules.focus)
    sem = asyncio.Semaphore(CONCURRENCY)
    # The IDE asks about this file, now. Prior-review recall belongs to the PR bot, which must
    # not repeat itself across rounds; wired in here it quietly demoted known bugs to a prose
    # aside and the panel showed a clean score for a file full of them.
    findings, positives, usage, names = await _review_file(
        fd, lenses, rules, "", related_block, [], sem
    )
    lint_findings, linters_run = await asyncio.to_thread(
        adapters.scan, {fd.path: code}, {fd.path: fd}, getattr(rules, "linters", [])
    )
    if lint_findings:
        findings.extend(lint_findings)
        names = [*names, "lint"]
    findings, duplicate_findings = dedupe.dedupe(findings)
    verified = await verify.verify_all(findings, {fd.path: fd}, rules)
    kept, over = sev.budget(verified.kept, rules.max_comments, rules.min_severity)
    s = await _summarise(kept, [fd.path], names, [])
    merged = _merge_usage(usage, s["usage"])
    calls, failures = _lens_health(merged)
    if calls and failures >= calls:          # IDE: say so instead of showing a 100/100
        return ReviewResult(
            status="incomplete", summary="Review unavailable, the model could not be reached.",
            score=0, verdict="incomplete", findings=[], files=[fd.path], lenses_run=names,
            model_used=llm.model_name(), usage=merged, review_effort=_review_effort([fd]),
            wall_seconds=time.monotonic() - t0,
        )
    return ReviewResult(
        status="ok", summary=s["summary"], score=s["score"], verdict=s["verdict"], findings=kept,
        positives=(positives + s["positives"])[:6], highlights=s["highlights"], files=[fd.path],
        lenses_run=names, dropped=len(verified.dropped) + len(over) + len(duplicate_findings),
        model_used=llm.model_name(), usage=merged,
        review_effort=_review_effort([fd]),
        wall_seconds=time.monotonic() - t0,
        verdict_reason=s["verdict_reason"], confidence=s["confidence"],
        merge_risk=s["merge_risk"], security_risk=s["security_risk"],
        engine_notes=_engine_notes(rules, verified, findings, False, 0, False,
                                   calls, failures, False, linters_run=linters_run),
    )


def to_review_response(res: ReviewResult) -> dict:
    """Adapt a v2 result to the v1 ReviewResponse shape the extension expects."""
    issues = []
    for f in res.findings:
        issues.append({
            "severity": sev.to_legacy(f.get("severity")),
            "category": f.get("category"),
            "line_number": f.get("line"),
            "title": f.get("title"),
            "description": f.get("body"),
            "fix": f.get("fix"),
        })
    return {
        "summary": res.summary,
        "score": res.score,
        "issues": issues,
        "refactored_code": None,
        "positive_aspects": res.positives,
        "review_id": res.review_id,
        "model_used": res.model_used,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── resolution tracking (the metric that keeps the bot honest) ────────────────
async def measure_resolution(owner: str, repo: str, pr_number: int, head_sha: str,
                             db: str | None = None) -> dict:
    """After a PR merges, did our findings actually get fixed?

    This is Cursor's "resolution rate": for each finding we posted, look at the
    merged file. If the quoted evidence is gone (or the file is gone), the author
    acted on it → `resolved`; otherwise `unresolved`. These verdicts are recorded
    separately from 👍/👎 so they never feed the suppression set, they are a
    quality metric, not a preference signal.
    """
    await store.init(db)
    slug = f"{owner}/{repo}"
    findings = await store.findings_for_pr(slug, pr_number, db)
    resolved, unresolved = 0, 0
    cache: dict[str, str | None] = {}
    for finding in findings:
        path = finding.get("path") or ""
        if path not in cache:
            cache[path] = await github.get_file_content(owner, repo, path, head_sha) if github.configured() else None
        text = cache[path]
        evidence = (finding.get("evidence") or "").strip()
        if _evidence_gone(text, evidence):
            await store.record_outcome(slug, finding["fingerprint"], "resolved", finding.get("title") or "", db)
            resolved += 1
        else:
            await store.record_outcome(slug, finding["fingerprint"], "unresolved", finding.get("title") or "", db)
            unresolved += 1
    return {"resolved": resolved, "unresolved": unresolved, "checked": len(findings)}

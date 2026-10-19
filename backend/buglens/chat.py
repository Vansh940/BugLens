"""Threaded `@buglens` Q&A and commands on a pull request.

The review is a broadcast; this is the conversation. It reuses the context the review already
used (a digest of the added lines, the code graph block, the findings we posted) and answers
in the thread. Routing and the Fix-All handoff are deterministic; only the prose costs a
single bounded model call.

Commands (a mention with no known verb is treated as a free-form question):

- `@buglens review` / `re-review` -> hand back to the review pipeline (re-review on demand)
- `@buglens explain` -> what this PR changes, as a Change-Stack narrative
- `@buglens why` -> why a finding matters (the top one, or the one on the line being replied to)
- `@buglens fix` -> a concrete fix plus a copy-ready Fix-All prompt for Claude Code/Cursor/Codex
- `@buglens <anything>` -> answer the question
"""
from __future__ import annotations

import logging
import re

from . import github, llm, prompts, store, walkthrough
from .context import language_of
from .diff import choose_paths, parse_patch
from .rules import load_rules

logger = logging.getLogger("buglens")

MAX_FINDINGS = 8

_REVIEW = {"review", "re-review", "rereview"}
_EXPLAIN = {"explain", "summary", "walkthrough"}
_WHY = {"why"}
_FIX = {"fix", "fix-all", "fixall"}


def parse_command(body: str) -> tuple[str, str]:
    """Route a comment body. Returns ``(command, argument)``.

    ``command`` is one of ``review`` / ``explain`` / ``why`` / ``fix`` / ``ask``.

    A known verb wins ("@buglens why ..." is a question). Otherwise a bare mention, or a
    mention that says "review", means on-demand review, so the historical behaviour holds.
    """
    m = re.search(r"@buglens\b(.*)", body or "", re.IGNORECASE | re.DOTALL)
    if not m:
        return ("ask", "")
    rest = m.group(1).strip().lstrip(":,-").strip()
    if not rest:
        return ("review", "")
    parts = rest.split(None, 1)
    head = parts[0].lower().strip(":,.-")
    arg = parts[1].strip() if len(parts) > 1 else ""
    if head in _EXPLAIN:
        return ("explain", arg)
    if head in _WHY:
        return ("why", arg)
    if head in _FIX:
        return ("fix", arg)
    if head in _REVIEW or re.search(r"\b(?:re-)?review\b", rest, re.IGNORECASE):
        return ("review", rest)
    return ("ask", rest)


def handoff_prompts(findings: list[dict], owner: str, repo: str, pr_number: int,
                    head_sha: str = "") -> str:
    """One copy-ready prompt for a coding agent. Deterministic, no model call."""
    items = [f for f in findings if f.get("path")][:MAX_FINDINGS]
    if not items:
        return "No open BugLens findings to hand off."
    head = f" @ {(head_sha or '')[:7]}" if head_sha else ""
    out = [
        "Paste this into Claude Code, Cursor or Codex from the repo root:",
        "",
        "```",
        f"# BugLens Fix-All for {owner}/{repo}#{pr_number}{head}",
        "Fix these review findings with minimal diffs; run the tests after each:",
    ]
    for i, f in enumerate(items, 1):
        loc = f"{f['path']}:{f.get('line') or '?'}"
        title = str(f.get("title") or "").strip()
        body = str(f.get("body") or f.get("evidence") or "").strip().replace("\n", " ")
        line = f"{i}. {loc} - {title}. {body}".strip()
        out.append(line[:400])
    out.append("```")
    return "\n".join(out)


async def _gather(owner: str, repo: str, pr_number: int, db: str | None):
    """The same context the review used: parsed diff, findings posted, effective rules."""
    from .pipeline import REVIEWABLE_EXTS

    info = await github.get_pr(owner, repo, pr_number)
    raw = await github.get_pr_files(owner, repo, pr_number) if github.configured() else []
    parsed = []
    for f in raw:
        fd = parse_patch(f.get("patch", ""), f.get("filename", ""), f.get("status", "modified"))
        fd.language = language_of(fd.path)
        parsed.append(fd)
    rules = load_rules()
    picked, _skipped = choose_paths(parsed, rules.ignore_paths, REVIEWABLE_EXTS, rules.max_files)
    findings = await store.findings_for_pr(f"{owner}/{repo}", pr_number, db) if db else []
    return info, picked, findings, rules


def _findings_block(findings: list[dict]) -> str:
    return "\n".join(
        f"- {f.get('path')}:{f.get('line') or '?'} [{f.get('severity') or '?'}] "
        f"{f.get('title') or ''} | evidence: {str(f.get('evidence') or '')[:120]}"
        for f in findings[:MAX_FINDINGS]
    )


def _digest(picked, max_chars: int = 5000) -> str:
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


def _change_stack_md(picked) -> str:
    stack = walkthrough.build_change_stack(picked, [])
    if not stack:
        return ""
    lines = ["**What this PR changes:**", ""]
    for ch in stack[:12]:
        lines.append(f"- **{ch['title']}** ({ch['note']})")
        lines += [f"  - `{p}`" for p in ch["files"][:6]]
    return "\n".join(lines)


async def answer(owner: str, repo: str, pr_number: int, command: str, argument: str,
                 db: str | None = None, head_sha: str = "") -> str:
    """Build the markdown reply for a threaded command. Never raises for a model miss."""
    info, picked, findings, rules = await _gather(owner, repo, pr_number, db)

    if command == "explain":
        body = _change_stack_md(picked)
        if not body:
            return "This PR has no reviewable added lines to explain."
        header = f"**{info.title or 'This PR'}**"
        tail = ""
        if findings:
            tail = f"\n\n{len(findings)} open finding(s). Ask `@buglens why` for the reasoning."
        return f"{header}\n\n{body}{tail}"

    if command == "fix":
        if not findings:
            return "No open BugLens findings to fix. Nothing to hand off."
        parts = ["**Fix-All**", ""]
        top = findings[0]
        parts.append(
            f"Top finding: `{top.get('path')}:{top.get('line') or '?'}` "
            f"{top.get('title') or ''}."
        )
        parts += ["", handoff_prompts(findings, owner, repo, pr_number, head_sha)]
        return "\n".join(parts)

    # `why` and free-form questions both go to the model, grounded in the same context.
    question = argument if command != "why" else (
        argument or "Why does the top finding matter, and what breaks if it ships?"
    )
    context_block = ""
    if command == "why":
        context_block = _change_stack_md(picked)
    try:
        result = await llm.complete_text([
            {"role": "system", "content": prompts.CHAT_SYSTEM},
            {"role": "user", "content": prompts.build_chat_prompt(
                question=question, command=command,
                context_block=context_block,
                findings_block=_findings_block(findings),
                diff_digest=_digest(picked),
            )},
        ])
        text = (result.text or "").strip()
    except Exception as exc:
        logger.warning("BugLens chat model call failed: %s", exc)
        text = ""
    if not text:
        # Honest fallback: a broken model must not look like a confident answer.
        fallback = _change_stack_md(picked)
        note = "_(the model is unreachable right now; here is what BugLens knows without it)_"
        return f"{note}\n\n{fallback}" if fallback else note
    return text


def _reply_marker(pr_number: int) -> str:
    return f"<!-- buglens:reply:{pr_number} -->"


async def handle_command(owner: str, repo: str, pr_number: int, body: str,
                         db: str | None = None, head_sha: str = "") -> dict:
    """Parse a mention and post the reply as a threaded issue comment."""
    command, argument = parse_command(body)
    if command == "review":
        return {"command": "review", "handed": "review_pipeline"}
    try:
        reply = await answer(owner, repo, pr_number, command, argument, db=db, head_sha=head_sha)
    except Exception:
        logger.exception("BugLens chat failed for %s/%s#%s", owner, repo, pr_number)
        reply = "BugLens could not answer that just now. Re-try with `@buglens` in a moment."
    if not reply.lstrip().startswith("<!--"):
        reply = _reply_marker(pr_number) + "\n" + reply
    try:
        await github.post_issue_comment(owner, repo, pr_number, reply)
    except Exception:
        logger.warning("BugLens could not post chat reply for %s/%s#%s", owner, repo, pr_number)
    return {"command": command, "posted": True}

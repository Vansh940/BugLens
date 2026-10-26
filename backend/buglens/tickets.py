"""Ticket / linked-issue context.

CodeRabbit validates a diff against linked issues and reports Addressed / Not addressed /
Unclear; Greptile reads Jira/Linear issue text as review context. We do the same with
GitHub alone: resolve the issues a PR closes (GraphQL, default-branch aware), fall back to
the canonical `Closes #N` keyword scan over the PR body and commit messages, pull the issue
body, and hand a compact ticket block + checklist to the spec lens.

No extra services: one GraphQL call, at most a couple of REST calls, all cached by the
caller's normal flow.
"""
from __future__ import annotations

import re

# GitHub's canonical keyword set (case-insensitive, optional colon).
_KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_SAME_REPO = re.compile(r"\b" + _KEYWORDS + r":?\s+#(\d+)\b", re.IGNORECASE)
_CROSS_REPO = re.compile(r"\b" + _KEYWORDS + r":?\s+([\w.-]+/[\w.-]+)#(\d+)\b", re.IGNORECASE)
_URL_RE = re.compile(
    r"\b" + _KEYWORDS + r":?\s+https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)\b",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$", re.M)
_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)$", re.M)


def strip_noise(text: str) -> str:
    """Remove fenced code, inline code, blockquotes and HTML comments before matching."""
    text = _FENCE.sub(" ", text or "")
    text = _INLINE_CODE.sub(" ", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"^\s*>.*$", " ", text, flags=re.M)
    return text


def parse_closing_refs(text: str) -> list[dict]:
    """Linked issues from free text. Returns [{repo(optional), number}] de-duplicated.

    Used only as a fallback/enrichment, the authoritative source is GraphQL
    ``closingIssuesReferences`` (which respects the default-branch rule).
    """
    clean = strip_noise(text)
    refs: list[dict] = []
    seen: set[tuple] = set()

    def add(repo: str | None, number: int) -> None:
        key = (repo, number)
        if key not in seen:
            seen.add(key)
            refs.append({"repo": repo, "number": number})

    for m in _URL_RE.finditer(clean):
        add(m.group(1), int(m.group(2)))
    for m in _CROSS_REPO.finditer(clean):
        add(m.group(1), int(m.group(2)))
    for m in _SAME_REPO.finditer(clean):
        add(None, int(m.group(1)))
    return refs


def extract_acceptance_criteria(body: str, limit: int = 12) -> list[str]:
    """Pull checklist items / bullet lists that follow an acceptance-criteria heading."""
    body = body or ""
    heading_zone = ""
    for m in _HEADING.finditer(body):
        title = m.group(1).lower()
        if any(w in title for w in ("acceptance", "requirement", "criteria", "todo", "task")):
            start = m.end()
            nxt = _HEADING.search(body, start)
            heading_zone += body[start: nxt.start() if nxt else len(body)]
    source = heading_zone or body
    items: list[str] = []
    for m in _CHECKBOX.finditer(source):
        items.append(m.group(1).strip())
    if not items:                       # no checkboxes: fall back to plain bullets in-zone
        for line in source.splitlines():
            s = line.strip()
            if s.startswith(("- ", "* ")) and len(s) > 3:
                items.append(s[2:].strip())
    return [i for i in items if i][:limit]


def format_ticket(issues: list[dict], criteria: dict[int, list[str]] | None = None,
                  budget: int = 1200) -> str:
    """Compact ticket block for the prompt. Empty string when there is no ticket."""
    if not issues:
        return ""
    criteria = criteria or {}
    parts = ["## Linked ticket(s) (authoritative intent for this change)"]
    for issue in issues[:3]:
        label = f"{issue.get('repo', '')}#{issue.get('number')}".strip("#")
        parts.append(f"### {label}, {issue.get('title', '').strip()}")
        items = criteria.get(issue.get("number"), [])[:8]
        body = (issue.get("body") or "").strip()
        if items:                                    # criteria are re-emitted as a clean list
            body = "\n".join(
                ln for ln in body.splitlines() if not _CHECKBOX.match(ln)
            ).strip()
        if body:
            parts.append(body[:600])
        for item in items:
            parts.append(f"- [ ] {item}")
    return "\n".join(parts)[:budget]

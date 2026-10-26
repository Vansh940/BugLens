"""Deterministic risk triggers and a bounded production pre-mortem.

Pure module: a file list in, a depth tier plus validated scenarios plus markdown out. No model,
no network, no I/O. The model may *propose* scenarios (the pipeline makes one bounded call, on
the deep tier only), but :func:`select` is the gate: a scenario survives only when it cites a
path and a line the PR actually added, so a pre-mortem can never become padding about code that
is not in the diff.
"""
from __future__ import annotations

import re

from .diff import FileDiff

MAX_ITEMS = 4
LARGE_ADDED_LINES = 200

# One shared definition of a risky surface: the depth trigger and the risk tier both use it, so a
# payment/auth/security path can never be "deep" for one and "low risk" for the other.
RISKY_TOKENS = frozenset({
    "auth", "authentication", "authorization", "login", "logout", "oauth", "token", "tokens",
    "session", "sessions", "permission", "permissions", "password", "passwords", "crypto",
    "security", "acl", "credential", "credentials", "payment", "payments", "billing", "wallet",
    "checkout", "secret", "secrets", "key", "keys",
})

# A trigger fires on a whole path token (never a substring): `auth` must not fire on `author.py`.
_TRIGGERS: tuple[dict, ...] = (
    {
        "name": "new route",
        "tokens": frozenset({"route", "routes", "router", "routers", "controller", "controllers",
                             "endpoint", "endpoints", "handler", "handlers"}),
        "statuses": frozenset({"added", "renamed"}),
    },
    {
        "name": "migration",
        "tokens": frozenset({"migration", "migrations", "alembic", "migrate"}),
        "statuses": None,
    },
    {
        "name": "auth/security path",
        "tokens": RISKY_TOKENS,
        "statuses": None,
    },
    {
        "name": "large change",
        "tokens": frozenset(),
        "statuses": None,
        "min_added_lines": LARGE_ADDED_LINES,
    },
)


def tokens(path: str) -> set[str]:
    """Word tokens of every path segment (`api/user_controller.py` -> {api, user, controller, py})."""
    out: set[str] = set()
    for seg in str(path).replace("\\", "/").split("/"):
        if not seg:
            continue
        for part in re.split(r"[^A-Za-z0-9]+", seg.rsplit(".", 1)[0] if "." in seg else seg):
            if part:
                out.add(part.lower())
    return out


def triggers(fds: list[FileDiff]) -> list[str]:
    """Names of the risk triggers this diff fires, in declaration order, each at most once."""
    fired: list[str] = []
    total_added = sum(len(fd.added) for fd in fds)
    for spec in _TRIGGERS:
        if spec.get("min_added_lines") is not None:
            if total_added >= int(spec["min_added_lines"]):
                fired.append(str(spec["name"]))
            continue
        allowed = spec["statuses"]
        for fd in fds:
            if allowed is not None and fd.status not in allowed:
                continue
            if tokens(fd.path) & spec["tokens"]:
                fired.append(str(spec["name"]))
                break
    return fired


def tier(enabled: bool, names: list[str]) -> str:
    """`deep` when the mechanism is on and any trigger fired, else `quick`. Never partial."""
    return "deep" if enabled and names else "quick"


def select(items: list[dict], fds: list[FileDiff], max_items: int = MAX_ITEMS) -> list[dict]:
    """Keep only scenarios anchored on a line this PR added, deduped by anchor, capped.

    A proposed item without a real anchor is dropped, never relocated: an unanchored production
    scenario is exactly the kind of confident guess the review gate exists to stop.
    """
    by_path = {fd.path: fd for fd in fds}
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        path = str(it.get("path") or "").strip()
        title = str(it.get("title") or "").strip()
        try:
            line = int(it.get("line"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        fd = by_path.get(path)
        if fd is None or not title or line not in fd.added_line_numbers:
            continue
        if (path, line) in seen:
            continue
        seen.add((path, line))
        out.append({"title": title, "why": str(it.get("why") or "").strip(),
                    "path": path, "line": line})
        if len(out) >= max_items:
            break
    return out


def render_md(items: list[dict]) -> list[str]:
    """Collapsible pre-mortem block, or an empty list when there is nothing real to show."""
    if not items:
        return []
    lines = ["", "<details><summary>Pre-mortem: what could break in production</summary>", ""]
    for it in items:
        why = it.get("why") or ""
        lines.append(f"- **{it['title']}** at `{it['path']}:{it['line']}`. {why}".rstrip())
    lines += ["", "</details>"]
    return lines

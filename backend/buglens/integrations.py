"""Optional external context: tickets (Jira, Linear) and error signals (Sentry, Datadog, PostHog).

Every provider is OFF until its env vars are set. A missing token, a network failure or a
malformed payload is a no-op that contributes nothing; no public function here ever raises.
All calls share one lazy keep-alive pool, closed from the app lifespan.
"""
from __future__ import annotations

import base64
import logging
import os
import re

import httpx

PROVIDERS = ("jira", "linear", "sentry", "datadog", "posthog")
TIMEOUT = 8.0

logger = logging.getLogger("buglens.integrations")

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

_TICKET_PROVIDERS = ("jira", "linear")
_SIGNAL_PROVIDERS = ("sentry", "datadog", "posthog")

_REQUIRED: dict[str, tuple[str, ...]] = {
    "jira": ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN"),
    "linear": ("LINEAR_API_KEY",),
    "sentry": ("SENTRY_TOKEN", "SENTRY_ORG"),
    "datadog": ("DD_API_KEY", "DD_APP_KEY"),
    "posthog": ("POSTHOG_API_KEY", "POSTHOG_PROJECT_ID"),
}

_LINEAR_QUERY = """
query($q:String!){
  issues(filter:{or:[{identifier:{eq:$q}},{title:{containsIgnoreCase:$q}}]}, first:1){
    nodes{ title description }
  }
}
"""


def configured(provider: str) -> bool:
    """True only when every env var this provider needs is set and non-empty."""
    name = (provider or "").strip().lower()
    required = _REQUIRED.get(name)
    if not required:
        return False
    return all((os.getenv(var) or "").strip() for var in required)


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """One process-wide keep-alive pool, reused for every integration call."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _client


async def aclose() -> None:
    """Close the shared pool (called on app shutdown). Safe when nothing was created."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _flatten(value) -> str:
    """Join every ``text`` value in an Atlassian doc tree, else fall back to ``str``."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                parts.append(text)
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    joined = " ".join(p for p in parts if p).strip()
    return joined or str(value)


async def _jira(query: str) -> str | None:
    q = (query or "").strip()
    if not _ISSUE_KEY_RE.match(q):
        return None
    base = (os.getenv("JIRA_BASE_URL") or "").rstrip("/")
    email = os.getenv("JIRA_EMAIL") or ""
    token = os.getenv("JIRA_TOKEN") or ""
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    resp = await _http().get(
        f"{base}/rest/api/3/issue/{q}",
        headers=headers,
        params={"fields": "summary,description"},
    )
    if resp.status_code != 200:
        return None
    fields = (resp.json() or {}).get("fields") or {}
    summary = str(fields.get("summary") or "").strip()
    description = _flatten(fields.get("description")).strip()
    text = "\n".join(p for p in (summary, description) if p).strip()
    return text or None


async def _linear(query: str) -> str | None:
    q = (query or "").strip()
    if not q:
        return None
    headers = {
        "Authorization": os.getenv("LINEAR_API_KEY") or "",
        "Content-Type": "application/json",
    }
    body = {"query": _LINEAR_QUERY, "variables": {"q": q}}
    resp = await _http().post("https://api.linear.app/graphql", headers=headers, json=body)
    if resp.status_code != 200:
        return None
    data = resp.json() or {}
    nodes = (((data.get("data") or {}).get("issues") or {}).get("nodes")) or []
    if not nodes:
        return None
    node = nodes[0] if isinstance(nodes[0], dict) else {}
    title = str(node.get("title") or "").strip()
    description = str(node.get("description") or "").strip()
    text = "\n".join(p for p in (title, description) if p).strip()
    return text or None


async def _sentry(repo: str) -> list[dict]:
    token = os.getenv("SENTRY_TOKEN") or ""
    org = os.getenv("SENTRY_ORG") or ""
    project = os.getenv("SENTRY_PROJECT") or ""
    base = (os.getenv("SENTRY_BASE_URL") or "https://sentry.io").rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await _http().get(
        f"{base}/api/0/projects/{org}/{project}/issues/",
        headers=headers,
        params={"query": repo, "limit": 5},
    )
    if resp.status_code != 200:
        return []
    out: list[dict] = []
    for issue in (resp.json() or [])[:5]:
        if not isinstance(issue, dict):
            continue
        out.append({
            "source": "sentry",
            "title": issue.get("title"),
            "count": issue.get("count"),
            "url": issue.get("permalink"),
        })
    return out


async def _datadog(repo: str) -> list[dict]:
    api_key = os.getenv("DD_API_KEY") or ""
    app_key = os.getenv("DD_APP_KEY") or ""
    site = os.getenv("DD_SITE") or "datadoghq.com"
    headers = {"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key}
    resp = await _http().get(
        f"https://api.{site}/api/v1/events",
        headers=headers,
        params={"query": repo, "limit": 5},
    )
    if resp.status_code != 200:
        return []
    events = (resp.json() or {}).get("events") or []
    out: list[dict] = []
    for event in events[:5]:
        if not isinstance(event, dict):
            continue
        out.append({
            "source": "datadog",
            "title": event.get("title") or event.get("text"),
            "count": None,
            "url": None,
        })
    return out


async def _posthog(repo: str) -> list[dict]:
    api_key = os.getenv("POSTHOG_API_KEY") or ""
    project = os.getenv("POSTHOG_PROJECT_ID") or ""
    base = (os.getenv("POSTHOG_BASE_URL") or "https://us.posthog.com").rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = await _http().get(
        f"{base}/api/projects/{project}/events/",
        headers=headers,
        params={"limit": 5},
    )
    if resp.status_code != 200:
        return []
    results = (resp.json() or {}).get("results") or []
    out: list[dict] = []
    for event in results[:5]:
        if not isinstance(event, dict):
            continue
        out.append({
            "source": "posthog",
            "title": event.get("event"),
            "count": None,
            "url": None,
        })
    return out


async def _signals(name: str, repo: str) -> list[dict]:
    if name == "sentry":
        return await _sentry(repo)
    if name == "datadog":
        return await _datadog(repo)
    return await _posthog(repo)


async def collect(providers: list, repo: str, query: str = "") -> dict:
    """Gather tickets and error signals from the configured providers.

    Unknown, unconfigured or failing providers contribute nothing. Returns
    ``{"tickets": [str], "signals": [dict], "used": [str]}``.
    """
    tickets: list[str] = []
    signals: list[dict] = []
    used: list[str] = []
    seen: set[str] = set()
    for raw in providers or []:
        name = str(raw or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        if not configured(name):
            continue
        if name in _TICKET_PROVIDERS:
            try:
                text = await (_jira(query) if name == "jira" else _linear(query))
            except Exception as exc:
                logger.debug("integration %s ticket lookup failed: %s", name, exc)
                text = None
            if text:
                tickets.append(text)
                used.append(name)
        elif name in _SIGNAL_PROVIDERS:
            try:
                found = await _signals(name, repo)
            except Exception as exc:
                logger.debug("integration %s signal lookup failed: %s", name, exc)
                found = []
            if found:
                signals.extend(found)
                used.append(name)
    return {"tickets": tickets, "signals": signals, "used": used}

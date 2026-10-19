from __future__ import annotations

import httpx

from buglens import integrations

ALL_VARS = (
    "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN", "LINEAR_API_KEY",
    "SENTRY_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT", "SENTRY_BASE_URL",
    "DD_API_KEY", "DD_APP_KEY", "DD_SITE",
    "POSTHOG_API_KEY", "POSTHOG_PROJECT_ID", "POSTHOG_BASE_URL",
)


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, get_response=None, post_response=None, get_by=None, post_by=None):
        self.get_response = get_response
        self.post_response = post_response
        self.get_by = get_by or {}
        self.post_by = post_by or {}
        self.calls = []

    async def get(self, url, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        for key, value in self.get_by.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        return self.get_response

    async def post(self, url, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        for key, value in self.post_by.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        return self.post_response


def clear_env(monkeypatch):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def install(monkeypatch, client):
    monkeypatch.setattr(integrations, "_http", lambda: client)
    return client


def test_configured_false_without_env(monkeypatch):
    clear_env(monkeypatch)
    for name in integrations.PROVIDERS:
        assert integrations.configured(name) is False


def test_configured_true_when_needed_vars_set(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("LINEAR_API_KEY", "lin")
    monkeypatch.setenv("SENTRY_TOKEN", "st")
    monkeypatch.setenv("SENTRY_ORG", "acme")
    assert integrations.configured("linear") is True
    assert integrations.configured("SENTRY") is True


def test_configured_partial_env_is_false(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.test")
    monkeypatch.setenv("JIRA_EMAIL", "me@x")
    assert integrations.configured("jira") is False


def test_configured_unknown_provider(monkeypatch):
    clear_env(monkeypatch)
    assert integrations.configured("github") is False
    assert integrations.configured("") is False


async def test_collect_all_unconfigured_is_noop(monkeypatch):
    clear_env(monkeypatch)
    called = []

    def fake_http():
        called.append(1)
        raise AssertionError("_http must not be called when nothing is configured")

    monkeypatch.setattr(integrations, "_http", fake_http)
    out = await integrations.collect(["jira", "sentry", "linear"], "o/r", "ABC-1")
    assert out == {"tickets": [], "signals": [], "used": []}
    assert called == []


async def test_jira_issue_key_returns_ticket(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.test")
    monkeypatch.setenv("JIRA_EMAIL", "me@x")
    monkeypatch.setenv("JIRA_TOKEN", "tok")
    doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello world"}]}],
    }
    client = install(monkeypatch, FakeClient(
        get_response=FakeResponse(200, {"fields": {"summary": "Fix bug", "description": doc}})
    ))
    out = await integrations.collect(["jira"], "o/r", "ABC-123")
    assert out["tickets"] == ["Fix bug\nHello world"]
    assert out["used"] == ["jira"]
    assert client.calls[0]["headers"]["Authorization"].startswith("Basic ")


async def test_jira_non_key_query_skips(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.test")
    monkeypatch.setenv("JIRA_EMAIL", "me@x")
    monkeypatch.setenv("JIRA_TOKEN", "tok")
    client = install(monkeypatch, FakeClient(get_response=FakeResponse(200, {})))
    out = await integrations.collect(["jira"], "o/r", "just some free text")
    assert out["tickets"] == []
    assert out["used"] == []
    assert client.calls == []


async def test_linear_returns_ticket(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("LINEAR_API_KEY", "lin")
    data = {"data": {"issues": {"nodes": [{"title": "T", "description": "D"}]}}}
    install(monkeypatch, FakeClient(post_response=FakeResponse(200, data)))
    out = await integrations.collect(["linear"], "o/r", "ENG-1")
    assert out["tickets"] == ["T\nD"]
    assert out["used"] == ["linear"]


async def test_sentry_signals(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_TOKEN", "st")
    monkeypatch.setenv("SENTRY_ORG", "acme")
    monkeypatch.setenv("SENTRY_PROJECT", "web")
    issues = [
        {"title": "A", "count": 3, "permalink": "u1"},
        {"title": "B", "count": 5, "permalink": "u2"},
    ]
    install(monkeypatch, FakeClient(get_response=FakeResponse(200, issues)))
    out = await integrations.collect(["sentry"], "o/r")
    assert len(out["signals"]) == 2
    assert all(s["source"] == "sentry" for s in out["signals"])
    assert out["signals"][0]["count"] == 3
    assert out["used"] == ["sentry"]


async def test_failing_provider_is_soft(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_TOKEN", "st")
    monkeypatch.setenv("SENTRY_ORG", "acme")
    monkeypatch.setenv("POSTHOG_API_KEY", "ph")
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "1")
    client = FakeClient(get_by={
        "sentry.io": httpx.ConnectError("boom"),
        "posthog.com": FakeResponse(200, {"results": [{"event": "signup"}]}),
    })
    install(monkeypatch, client)
    out = await integrations.collect(["sentry", "posthog"], "o/r")
    assert out["used"] == ["posthog"]
    assert len(out["signals"]) == 1
    assert out["signals"][0]["source"] == "posthog"


async def test_used_only_when_data_and_deduped(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_TOKEN", "st")
    monkeypatch.setenv("SENTRY_ORG", "acme")
    monkeypatch.setenv("POSTHOG_API_KEY", "ph")
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "1")
    client = FakeClient(get_by={
        "sentry.io": FakeResponse(200, []),
        "posthog.com": FakeResponse(200, {"results": [{"event": "signup"}]}),
    })
    install(monkeypatch, client)
    out = await integrations.collect(["sentry", "SENTRY", "posthog"], "o/r")
    assert out["used"] == ["posthog"]
    sentry_calls = [c for c in client.calls if "sentry.io" in c["url"]]
    assert len(sentry_calls) == 1


class FakeCloseClient:
    def __init__(self):
        self.is_closed = False
        self.closed = 0

    async def aclose(self):
        self.closed += 1
        self.is_closed = True


async def test_aclose_twice_is_safe(monkeypatch):
    fake = FakeCloseClient()
    monkeypatch.setattr(integrations, "_client", fake)
    await integrations.aclose()
    await integrations.aclose()
    assert fake.closed == 1

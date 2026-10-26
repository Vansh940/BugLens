"""Provider-agnostic OpenAI-compatible LLM client.

Works with Groq (default), OpenAI, Together, local vLLM/llama.cpp, or the local
opencode free proxy, anything that speaks ``POST {base}/chat/completions``. This
replaces the v1 Groq-SDK-only path and fixes three of its weaknesses:

* key rotation is guarded by a lock (v1 mutated a global from concurrent tasks);
* malformed JSON is repaired instead of turning into a 500 (v1 raised);
* token usage is captured so cost is measurable (v1 discarded it).

Tests never hit the network: monkeypatch :func:`complete_json`.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


@dataclass
class LLMResult:
    data: dict
    text: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)


def _env_keys() -> list[str]:
    keys: list[str] = []
    for name in ("OPENAI_API_KEY", "LLM_API_KEY", "GROQ_API_KEY", "DASHSCOPE_API_KEY"):
        v = os.getenv(name)
        if v:
            keys.append(v)
    for i in range(1, 6):
        v = os.getenv(f"GROQ_API_KEY_{i}")
        if v:
            keys.append(v)
    seen, unique = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def base_url() -> str:
    return (os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def model_name() -> str:
    return os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or DEFAULT_MODEL


def configured() -> bool:
    return bool(_env_keys())


class _KeyPool:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.index = 0
        self._lock = asyncio.Lock()

    async def current(self) -> str:
        async with self._lock:
            return self.keys[self.index]

    async def rotate(self) -> bool:
        async with self._lock:
            if self.index < len(self.keys) - 1:
                self.index += 1
                return True
            return False

    def reset(self):
        self.index = 0


_pool: _KeyPool | None = None


def _get_pool() -> _KeyPool:
    global _pool
    keys = _env_keys()
    if not keys:
        raise LLMNotConfigured("No LLM API key configured (OPENAI_API_KEY / GROQ_API_KEY).")
    if _pool is None or _pool.keys != keys:
        _pool = _KeyPool(keys)
    return _pool


def reset_pool():
    global _pool
    _pool = None


# ── JSON repair ───────────────────────────────────────────────────────────────
def strip_json_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]).strip()
    return text


def parse_json(text: str) -> dict:
    """Best-effort parse of a model reply: fences, prose, then repair."""
    cleaned = strip_json_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(cleaned)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:                                   # optional dependency
                import json_repair  # type: ignore

                repaired = json_repair.loads(candidate)
                if isinstance(repaired, dict):
                    return repaired
            except Exception:
                pass
    raise LLMError(f"model returned invalid JSON: {cleaned[:200]!r}")


# ── transport ─────────────────────────────────────────────────────────────────
MAX_CONNECTIONS = int(os.getenv("BUGLENS_LLM_MAX_CONNECTIONS", "32"))
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """One process-wide keep-alive pool.

    Creating an ``AsyncClient`` per call (the old behaviour) meant a fresh TCP handshake
    and a fresh TLS handshake for https, for every lens, every consensus pass and every
    verify call. Under a burst of webhooks that is the difference between a few sockets and
    thousands of them.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=90.0,
            limits=httpx.Limits(max_connections=MAX_CONNECTIONS,
                                max_keepalive_connections=MAX_CONNECTIONS // 2),
            follow_redirects=True,
        )
    return _client


async def aclose() -> None:
    """Close the shared pool (called on app shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _post(messages: list[dict], model: str, temperature: float, max_tokens: int,
                json_mode: bool, timeout: float) -> dict:
    pool = _get_pool()
    tried = 0
    last_error: Exception | None = None
    while tried <= len(pool.keys):
        key = await pool.current()
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            resp = await _http().post(f"{base_url()}/chat/completions", json=payload,
                                      headers=headers, timeout=timeout)
            if resp.status_code == 429:
                if await pool.rotate():
                    tried += 1
                    continue
                raise LLMError("All API keys are rate limited.")
            if resp.status_code >= 500:
                last_error = LLMError(f"upstream {resp.status_code}")
                await asyncio.sleep(min(2 ** tried + random.random(), 8))
                tried += 1
                continue
            if resp.status_code >= 400:
                raise LLMError(f"LLM {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            await asyncio.sleep(min(2 ** tried + random.random(), 8))
            tried += 1
    raise LLMError(f"LLM request failed after retries: {last_error}")


async def complete_json(messages: list[dict], model: str | None = None, temperature: float = 0.0,
                        max_tokens: int = 4096, timeout: float = 90.0) -> LLMResult:
    model = model or model_name()
    body = await _post(messages, model, temperature, max_tokens, json_mode=True, timeout=timeout)
    content = body["choices"][0]["message"]["content"]
    return LLMResult(data=parse_json(content), text=content, model=model, usage=body.get("usage", {}) or {})


async def complete_text(messages: list[dict], model: str | None = None, temperature: float = 0.2,
                        max_tokens: int = 1024, timeout: float = 60.0) -> LLMResult:
    model = model or model_name()
    body = await _post(messages, model, temperature, max_tokens, json_mode=False, timeout=timeout)
    content = body["choices"][0]["message"]["content"]
    return LLMResult(data={}, text=content, model=model, usage=body.get("usage", {}) or {})

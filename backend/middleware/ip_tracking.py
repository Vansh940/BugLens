"""
IP tracking middleware for BugLens API.

Logs every request to /api/v1/review (and optionally other endpoints) into
Redis so the analytics dashboard can show:
  - which IPs have used the extension, and how many requests each sent
  - when each IP was first / last seen
  - a rolling log of recent requests per IP (for "active now" detection)

Storage layout in Redis (Upstash):
  buglens:ip:count           HASH   ip -> total request count
  buglens:ip:first_seen      HASH   ip -> ISO8601 timestamp of first request
  buglens:ip:last_seen       HASH   ip -> ISO8601 timestamp of most recent request
  buglens:ip:known           SET    all IPs ever seen (for iteration)

All keys are plain Redis data structures (not per-request keys), so this
scales to millions of requests without key explosion.
"""

from __future__ import annotations

from datetime import datetime, timezone

import redis.asyncio as redis
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from services.cache_service import pool  # reuse the same connection pool

# Only track requests to these path prefixes. Keeps noise (health checks,
# docs, static assets) out of the analytics data.
TRACKED_PATH_PREFIXES = ("/api/v1/review",)

REDIS_KEY_COUNT = "buglens:ip:count"
REDIS_KEY_FIRST_SEEN = "buglens:ip:first_seen"
REDIS_KEY_LAST_SEEN = "buglens:ip:last_seen"
REDIS_KEY_KNOWN = "buglens:ip:known"


def _client_ip(request: Request) -> str:
    """
    Best-effort real client IP extraction.

    Render (and most PaaS providers) sit behind a proxy, so the real client
    IP arrives in X-Forwarded-For, not request.client.host. We take the
    left-most entry, which is the original client.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


class IPTrackingMiddleware(BaseHTTPMiddleware):
    """
    No constructor args needed — reuses the same connection pool as
    cache_service.py (redis.asyncio.ConnectionPool), consistent with how
    the rest of the codebase talks to Redis (a fresh redis.Redis(pool)
    per call, not one long-lived client held on app.state).
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        should_track = any(path.startswith(p) for p in TRACKED_PATH_PREFIXES)

        response = await call_next(request)

        if should_track:
            try:
                await self._record(request)
            except Exception:
                # Analytics must never break the actual API response.
                # Swallow errors here; consider logging them if you add
                # structured logging later.
                pass

        return response

    async def _record(self, request: Request) -> None:
        ip = _client_ip(request)
        now_iso = datetime.now(timezone.utc).isoformat()

        r = redis.Redis(connection_pool=pool)
        pipe = r.pipeline()
        pipe.hincrby(REDIS_KEY_COUNT, ip, 1)
        pipe.hsetnx(REDIS_KEY_FIRST_SEEN, ip, now_iso)  # only sets if absent
        pipe.hset(REDIS_KEY_LAST_SEEN, ip, now_iso)
        pipe.sadd(REDIS_KEY_KNOWN, ip)
        await pipe.execute()
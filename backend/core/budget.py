"""
Simple per-IP daily budget for Groq-backed endpoints, using the existing
Redis connection pool. INCR + EXPIRE, nothing fancier needed at this scale.
"""
from __future__ import annotations

import redis.asyncio as redis
from fastapi import HTTPException, Request

from services.cache_service import pool
from core.security import get_trusted_client_ip

SECONDS_PER_DAY = 86400

async def enforce_daily_budget(request: Request, bucket: str, max_per_day: int) -> None:
    """
    bucket: a short string identifying which endpoint this budget covers
            (e.g. "review", "chat") so /review and /chat get independent caps.
    """
    ip = get_trusted_client_ip(request)
    key = f"buglens:budget:{bucket}:{ip}"

    r = redis.Redis(connection_pool=pool)
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, SECONDS_PER_DAY)

    if count > max_per_day:
        raise HTTPException(
            429,
            f"Daily request limit reached for this endpoint. Try again tomorrow.",
        )

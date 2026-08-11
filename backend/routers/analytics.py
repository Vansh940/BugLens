"""
Analytics endpoints for the BugLens dashboard.

GET /api/v1/analytics/ips
    Returns per-IP request counts + first/last seen timestamps.

GET /api/v1/analytics/downloads
    Returns install/download counts from VS Code Marketplace + Open VSX,
    cached in Redis for a few hours since these are slow-changing numbers
    and we don't want to hammer either API on every dashboard refresh.

Wire this into main.py with:
    from routers import analytics
    app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["analytics"])

Uses the same `pool` (redis.asyncio.ConnectionPool) as cache_service.py and
ip_tracking.py — a fresh redis.Redis(connection_pool=pool) per call. main.py
has no lifespan-created Redis client and no app.state.redis, so nothing here
depends on that.

Env vars read:
    VSCODE_PUBLISHER   e.g. "VanshDev"
    VSCODE_EXTENSION   e.g. "buglens"
    OPENVSX_NAMESPACE  e.g. "VanshDev"   (Open VSX namespace, may differ)
    OPENVSX_EXTENSION  e.g. "buglens"
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx
import redis.asyncio as redis
from fastapi import APIRouter
from pydantic import BaseModel

from middleware.ip_tracking import (
    REDIS_KEY_COUNT,
    REDIS_KEY_FIRST_SEEN,
    REDIS_KEY_LAST_SEEN,
    REDIS_KEY_KNOWN,
)
from services.cache_service import pool  # same connection pool as everywhere else

router = APIRouter()


def _redis() -> redis.Redis:
    return redis.Redis(connection_pool=pool)


VSCODE_PUBLISHER = os.getenv("VSCODE_PUBLISHER", "VanshDev")
VSCODE_EXTENSION = os.getenv("VSCODE_EXTENSION", "buglens")
OPENVSX_NAMESPACE = os.getenv("OPENVSX_NAMESPACE", "VanshDev")
OPENVSX_EXTENSION = os.getenv("OPENVSX_EXTENSION", "buglens")

DOWNLOADS_CACHE_KEY = "buglens:analytics:downloads_cache"
DOWNLOADS_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours

# Consider an IP "active" if it made a request within this window.
ACTIVE_WINDOW_SECONDS = 24 * 60 * 60  # 24 hours


class IPStat(BaseModel):
    ip: str
    request_count: int
    first_seen: str | None
    last_seen: str | None
    active: bool


class IPStatsResponse(BaseModel):
    total_unique_ips: int
    active_ips: int
    ips: list[IPStat]


class DownloadStats(BaseModel):
    vscode_marketplace: int | None
    open_vsx: int | None
    total: int | None
    fetched_at: str
    note: str | None = None


@router.get("/ips", response_model=IPStatsResponse)
async def get_ip_stats(limit: int = 200):
    """
    Per-IP request counts, sorted by request count descending.

    `limit` caps how many IPs are returned (default 200) so a dashboard
    doesn't choke if the extension blows up and you get thousands of
    distinct IPs.
    """
    r = _redis()

    known_ips = await r.smembers(REDIS_KEY_KNOWN)
    known_ips = sorted(known_ips) if known_ips else []

    if not known_ips:
        return IPStatsResponse(total_unique_ips=0, active_ips=0, ips=[])

    counts = await r.hmget(REDIS_KEY_COUNT, known_ips)
    first_seens = await r.hmget(REDIS_KEY_FIRST_SEEN, known_ips)
    last_seens = await r.hmget(REDIS_KEY_LAST_SEEN, known_ips)

    now = datetime.now(timezone.utc)
    stats: list[IPStat] = []

    for ip, count, first_seen, last_seen in zip(known_ips, counts, first_seens, last_seens):
        is_active = False
        if last_seen:
            try:
                last_seen_dt = datetime.fromisoformat(last_seen)
                is_active = (now - last_seen_dt).total_seconds() <= ACTIVE_WINDOW_SECONDS
            except ValueError:
                pass

        stats.append(
            IPStat(
                ip=ip,
                request_count=int(count) if count else 0,
                first_seen=first_seen,
                last_seen=last_seen,
                active=is_active,
            )
        )

    stats.sort(key=lambda s: s.request_count, reverse=True)
    active_count = sum(1 for s in stats if s.active)

    return IPStatsResponse(
        total_unique_ips=len(stats),
        active_ips=active_count,
        ips=stats[:limit],
    )


@router.get("/downloads", response_model=DownloadStats)
async def get_download_stats(force_refresh: bool = False):
    """
    Install/download counts across VS Code Marketplace and Open VSX.

    Cached in Redis for DOWNLOADS_CACHE_TTL_SECONDS. Pass ?force_refresh=true
    to bypass the cache (useful for debugging, but don't call this on every
    dashboard page load — be a good citizen of both APIs).
    """
    r = _redis()

    if not force_refresh:
        cached = await r.get(DOWNLOADS_CACHE_KEY)
        if cached:
            return DownloadStats(**json.loads(cached))

    vscode_count = await _fetch_vscode_marketplace_installs()
    openvsx_count = await _fetch_openvsx_downloads()

    total = None
    note = None
    if vscode_count is not None and openvsx_count is not None:
        total = vscode_count + openvsx_count
    elif vscode_count is not None or openvsx_count is not None:
        total = (vscode_count or 0) + (openvsx_count or 0)
        note = "One source failed to respond; total may be incomplete."
    else:
        note = "Both sources failed to respond."

    result = DownloadStats(
        vscode_marketplace=vscode_count,
        open_vsx=openvsx_count,
        total=total,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        note=note,
    )

    await r.set(
        DOWNLOADS_CACHE_KEY,
        result.model_dump_json(),
        ex=DOWNLOADS_CACHE_TTL_SECONDS,
    )

    return result


async def _fetch_vscode_marketplace_installs() -> int | None:
    """
    Queries the VS Code Marketplace Gallery API (the same one the VS Code
    client uses to browse extensions). No auth required.
    """
    url = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
    payload = {
        "filters": [
            {
                "criteria": [
                    {"filterType": 7, "value": f"{VSCODE_PUBLISHER}.{VSCODE_EXTENSION}"}
                ]
            }
        ],
        "flags": 914,  # includes install count statistic
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json;api-version=3.0-preview.1",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        extension = data["results"][0]["extensions"][0]
        stats = extension.get("statistics", [])
        for stat in stats:
            if stat.get("statisticName") == "install":
                return int(stat.get("value", 0))
        return 0
    except Exception:
        return None


async def _fetch_openvsx_downloads() -> int | None:
    """
    Queries Open VSX's public REST API for the extension's download count.
    Docs: https://open-vsx.org/swagger-ui/index.html
    """
    url = f"https://open-vsx.org/api/{OPENVSX_NAMESPACE}/{OPENVSX_EXTENSION}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        return int(data.get("downloadCount", 0))
    except Exception:
        return None
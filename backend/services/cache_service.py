import redis.asyncio as redis
import json
import os

CACHE_VERSION = "v4"   # bump this number whenever you change ReviewResponse schema

pool = redis.ConnectionPool.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True
)

def make_key(code_hash: str) -> str:
    return f"{CACHE_VERSION}:review:{code_hash}"   # key includes version

async def get_cached_review(code_hash: str) -> dict | None:
    try:Pr
        r = redis.Redis(connection_pool=pool)
        data = await r.get(make_key(code_hash))
        return json.loads(data) if data else None
    except Exception:
        return None   # cache failure is never fatal

async def cache_review(code_hash: str, review_data: dict,
                        ttl_seconds: int = 2592000) -> None:  
    try:
        r = redis.Redis(connection_pool=pool)
        await r.setex(make_key(code_hash), ttl_seconds, json.dumps(review_data))
    except Exception:
        pass   # cache failure is never fatal
"""
Shared security helpers for BugLens API.
"""
from __future__ import annotations

import hmac
import os
from fastapi import Request, HTTPException, Header

# ─── Trusted-proxy IP extraction ──────────────────────────────────────────
def get_trusted_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


# ─── Admin auth (for /api/v1/analytics/*) ─────────────────────────────────
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

async def require_admin_key(x_admin_key: str = Header(default="")) -> None:
    if not ADMIN_API_KEY:
        raise HTTPException(503, "Admin endpoint not configured")
    if not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(401, "Invalid or missing X-Admin-Key")


# ─── Extension shared secret (for /api/v1/review and /api/v1/chat) ───────
EXTENSION_SHARED_SECRET = os.getenv("EXTENSION_SHARED_SECRET", "")

async def require_extension_key(x_extension_key: str = Header(default="")) -> None:
    if not EXTENSION_SHARED_SECRET:
        raise HTTPException(503, "Extension auth not configured")
    if not hmac.compare_digest(x_extension_key, EXTENSION_SHARED_SECRET):
        raise HTTPException(401, "Invalid or missing X-Extension-Key")

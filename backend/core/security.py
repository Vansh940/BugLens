"""Shared security helpers for BugLens API."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os

from fastapi import Header, HTTPException, Request

logger = logging.getLogger("buglens")
_warned_open_extension = False


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


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
async def require_admin_key(x_admin_key: str = Header(default="")) -> None:
    key = _env("ADMIN_API_KEY")
    if not key:
        raise HTTPException(503, "Admin endpoint not configured")
    if not hmac.compare_digest(x_admin_key, key):
        raise HTTPException(401, "Invalid or missing X-Admin-Key")


# ─── Extension shared secret (for /api/v1/review and /api/v1/chat) ───────
# The key ships inside the client, so it is a weak control: when it is unset we
# run open (so a self-hosted instance works out of the box) instead of returning
# 503/401 on every request like v1 did. Set it to require a match.
async def require_extension_key(x_extension_key: str = Header(default="")) -> None:
    global _warned_open_extension
    key = _env("EXTENSION_SHARED_SECRET")
    if not key:
        if not _warned_open_extension:
            logger.warning("EXTENSION_SHARED_SECRET is not set: /review and /chat are unauthenticated.")
            _warned_open_extension = True
        return
    if not hmac.compare_digest(x_extension_key, key):
        raise HTTPException(401, "Invalid or missing X-Extension-Key")


# ─── GitHub webhook signature (fail closed) ──────────────────────────────
def verify_github_signature(body: bytes, signature: str | None, secret: str | None = None) -> bool:
    secret = secret if secret is not None else _env("GITHUB_WEBHOOK_SECRET")
    if not secret:
        return False  # fail closed: no secret configured means no webhooks accepted
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

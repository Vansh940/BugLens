"""Startup configuration validation.

v1 shipped a fail-open webhook (an unset secret made the HMAC forgeable by anyone)
and shipped placeholder credentials in ``.env.example`` that were live if copied.
This module refuses to let an obviously-wrong configuration run silently.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("buglens")

PLACEHOLDERS = {
    "ADMIN_API_KEY": {"ADMIN_API_KEY", "admin", "changeme", "change_me"},
    "EXTENSION_SHARED_SECRET": {"EXTENSION_SHARED_SECRET", "secret", "changeme"},
}


def is_production() -> bool:
    return (
        os.getenv("RENDER") is not None
        or os.getenv("ENVIRONMENT", "").lower() in {"prod", "production"}
    )


def validate_config() -> list[str]:
    """Return a list of fatal configuration problems. Empty list == OK to run."""
    problems: list[str] = []

    for name, placeholders in PLACEHOLDERS.items():
        value = os.getenv(name, "")
        if value and value in placeholders:
            problems.append(f"{name} is set to a placeholder value ({value!r})")

    if not os.getenv("GITHUB_WEBHOOK_SECRET"):
        # Not fatal, the app serves the IDE endpoint fine, but webhooks will
        # refuse every request until it is set (fail closed, never fail open).
        logger.warning("GITHUB_WEBHOOK_SECRET is not set: PR webhooks are disabled.")

    if not os.getenv("GITHUB_TOKEN"):
        logger.warning("GITHUB_TOKEN is not set: the PR bot cannot fetch or comment.")

    if not any(os.getenv(k) for k in ("OPENAI_API_KEY", "LLM_API_KEY", "GROQ_API_KEY")) and not any(
        os.getenv(f"GROQ_API_KEY_{i}") for i in range(1, 6)
    ):
        logger.warning("No LLM key configured: /api/v1/review will return 503.")

    if is_production():
        if os.getenv("ALLOWED_ORIGINS", "*") == "*":
            problems.append("ALLOWED_ORIGINS is '*' in production")

    return problems

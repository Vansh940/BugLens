"""Feedback + engine introspection.

This is the write side of the self-improvement loop described in AGENTS.md /
ITERATION.md: a 👍/👎 on a posted finding is recorded, and after enough 👎 the
fingerprint is suppressed from future reviews.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from buglens import learn, lenses, store
from buglens.rules import describe
from buglens.rules import load_rules
from core.budget import enforce_daily_budget
from core.security import require_admin_key
from models.schemas import FeedbackRequest

logger = logging.getLogger("buglens")
router = APIRouter(prefix="/api/v1", tags=["feedback"])

_VERDICT = {"good": "up", "bad": "down"}


@router.post("/feedback")
async def submit_feedback(body: FeedbackRequest, request: Request):
    await enforce_daily_budget(request, bucket="feedback", max_per_day=2000)
    await store.record_outcome(body.repo, body.fingerprint, _VERDICT[body.verdict], body.note or "")
    suppressed = await store.suppressed(body.repo)
    return {
        "ok": True,
        "recorded": body.verdict,
        "fingerprint": body.fingerprint,
        "now_suppressed": body.fingerprint in suppressed,
    }


@router.get("/engine", tags=["engine"])
async def engine_overview():
    """What the running engine can do, handy for the dashboard and for debugging."""
    rules = load_rules()
    return {
        "lenses": lenses.describe_all(),
        "rules": describe(rules),
        "store": await store.stats(),
    }


@router.get("/engine/repos", tags=["engine"])
async def engine_repos():
    """Repos the engine has reviewed (dashboard picker / IDE repo check)."""
    return {"repos": await store.known_repos()}


@router.get("/engine/learn", dependencies=[Depends(require_admin_key)], tags=["engine"])
async def engine_learn(repo: str):
    """Learned feedback for a repo: per-lens stats, dismissal scopes, candidate rules.

    Read-only. The candidate snippets are suggestions a human copies into `.buglens.yml`;
    the engine never applies them.
    """
    stats = await store.lens_outcome_stats(repo)
    scopes = await store.dismissal_scopes(repo)
    return {"repo": repo, "lens_stats": stats, "scopes": scopes,
            "candidates": learn.candidate_rules(scopes, stats)}


@router.get("/engine/stats", dependencies=[Depends(require_admin_key)], tags=["engine"])
async def engine_stats(repo: str | None = None):
    stats = await store.stats()
    if not stats:
        raise HTTPException(503, "engine store unavailable")
    if repo:
        stats["resolution"] = await store.resolution_stats(repo)
        stats["drops"] = await store.drop_stats(repo)
    return stats

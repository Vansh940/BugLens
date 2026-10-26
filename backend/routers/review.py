"""IDE review endpoint (v2).

Now routes through the same multi-lens engine as the PR bot instead of the v1
"send the whole file to Groq and pray" path. Response shape is unchanged so the
published extension keeps working.
"""
import logging
import time
import traceback

from fastapi import APIRouter, Depends, HTTPException, Request

from buglens import llm, pipeline, store
from core.budget import enforce_daily_budget
from core.security import require_extension_key
from models.database import save_review
from models.schemas import ReviewRequest, ReviewResponse

logger = logging.getLogger("buglens")
router = APIRouter(prefix="/api/v1", tags=["review"])

MAX_CODE_CHARS = 150_000
MIN_CODE_CHARS = 10


@router.post("/review", response_model=ReviewResponse, dependencies=[Depends(require_extension_key)])
async def create_review(request_body: ReviewRequest, request: Request):
    await enforce_daily_budget(request, bucket="review", max_per_day=500)

    if len(request_body.code) > MAX_CODE_CHARS:
        raise HTTPException(400, f"Code exceeds {MAX_CODE_CHARS:,} character limit")
    if len(request_body.code.strip()) < MIN_CODE_CHARS:
        raise HTTPException(400, "Code is too short to review")
    if not llm.configured():
        raise HTTPException(503, "Review engine is not configured (no LLM API key)")

    start = time.time()
    try:
        result = await pipeline.review_snippet(
            request_body.code,
            request_body.language,
            filename=request_body.filename or "snippet",
            focus=request_body.focus,
            db=store.db_path(),
            repo=request_body.repo,
            related=request_body.related,
            imports=request_body.imports,
        )
    except Exception:
        logger.error("Review failed:\n%s", traceback.format_exc())
        raise HTTPException(500, "Review failed. Please try again.")

    latency_ms = int((time.time() - start) * 1000)
    payload = pipeline.to_review_response(result)
    critical_count = sum(1 for i in payload["issues"] if i["severity"] == "critical")

    try:
        await save_review(
            review_id=payload["review_id"],
            language=request_body.language,
            score=payload["score"],
            issue_count=len(payload["issues"]),
            critical_count=critical_count,
            latency_ms=latency_ms,
            model_used=payload["model_used"],
            source="api",
            filename=request_body.filename,
        )
    except Exception:
        logger.warning("DB save failed (non-fatal):\n%s", traceback.format_exc())

    return ReviewResponse(**payload)

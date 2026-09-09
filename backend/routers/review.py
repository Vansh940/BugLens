from fastapi import APIRouter, HTTPException, Depends, Request
from models.schemas import ReviewRequest, ReviewResponse
from services.groq_service import review_code
from models.database import save_review
from core.security import require_extension_key
from core.budget import enforce_daily_budget
import time
import logging
import traceback

logger = logging.getLogger("buglens")
router = APIRouter(prefix="/api/v1", tags=["review"])

@router.post("/review", response_model=ReviewResponse, dependencies=[Depends(require_extension_key)])
async def create_review(request_body: ReviewRequest, request: Request):
    await enforce_daily_budget(request, bucket="review", max_per_day=500)

    if len(request_body.code) > 150_000:
        raise HTTPException(400, "Code exceeds 150,000 character limit")
    if len(request_body.code.strip()) < 10:
        raise HTTPException(400, "Code is too short to review")

    start = time.time()

    try:
        result = await review_code(request_body)
    except Exception:
        logger.error("Review failed:\n%s", traceback.format_exc())
        raise HTTPException(500, "Review failed. Please try again.")

    latency_ms = int((time.time() - start) * 1000)
    critical_count = sum(1 for i in result.issues if i.severity == "critical")

    try:
        await save_review(
            review_id=result.review_id,
            language=request_body.language,
            score=result.score,
            issue_count=len(result.issues),
            critical_count=critical_count,
            latency_ms=latency_ms,
            model_used=result.model_used,
            source="api"
        )
    except Exception:
        logger.warning("DB save failed (non-fatal):\n%s", traceback.format_exc())

    return result

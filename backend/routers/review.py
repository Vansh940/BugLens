from fastapi import APIRouter, HTTPException
from models.schemas import ReviewRequest, ReviewResponse
from services.groq_service import review_code
from models.database import save_review
import time
import traceback

router = APIRouter(prefix="/api/v1", tags=["review"])

@router.post("/review", response_model=ReviewResponse)
async def create_review(request: ReviewRequest):
    if len(request.code) > 50_000:
        raise HTTPException(400, "Code exceeds 50,000 character limit")
    if len(request.code.strip()) < 10:
        raise HTTPException(400, "Code is too short to review")

    start = time.time()

    try:
        result = await review_code(request)
    except Exception as e:
        print(f"\n❌ REVIEW FAILED:\n{traceback.format_exc()}")  # prints full error
        raise HTTPException(500, f"Review failed: {str(e)}")

    latency_ms = int((time.time() - start) * 1000)
    critical_count = sum(1 for i in result.issues if i.severity == "critical")

    try:
        await save_review(
            review_id=result.review_id,
            language=request.language,
            score=result.score,
            issue_count=len(result.issues),
            critical_count=critical_count,
            latency_ms=latency_ms,
            model_used=result.model_used,
            source="api"
        )
    except Exception as e:
        print(f"\n⚠️ DB SAVE FAILED (non-fatal):\n{traceback.format_exc()}")

    return result
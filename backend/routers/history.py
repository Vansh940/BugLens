from fastapi import APIRouter
from sqlalchemy import select, func
from models.database import AsyncSessionLocal, Review

router = APIRouter(prefix="/api/v1", tags=["history"])

@router.get("/history")
async def get_history(limit: int = 20):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Review).order_by(Review.created_at.desc()).limit(limit)
        )
        reviews = result.scalars().all()
        return [
            {
                "review_id":     str(r.id),
                "language":      r.language,
                "score":         r.score,
                "issue_count":   r.issue_count,
                "critical_count":r.critical_count,
                "latency_ms":    r.latency_ms,
                "source":        r.source,
                "created_at":    r.created_at,
            }
            for r in reviews
        ]

@router.get("/stats")
async def get_stats():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                Review.language,
                func.count(Review.id).label("total"),
                func.avg(Review.score).label("avg_score"),
                func.avg(Review.latency_ms).label("avg_latency_ms"),
            ).group_by(Review.language)
        )
        rows = result.all()
        return [
            {
                "language":       r.language,
                "total_reviews":  r.total,
                "avg_score":      round(float(r.avg_score), 1),
                "avg_latency_ms": round(float(r.avg_latency_ms)),
            }
            for r in rows
        ]
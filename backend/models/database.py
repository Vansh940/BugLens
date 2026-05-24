import os
import uuid
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, insert as pg_insert
from sqlalchemy.sql import func

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:12345678@localhost:5432/code_reviewer")
ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(ASYNC_DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class Review(Base):
    __tablename__ = "reviews"
    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    language       = Column(String(50), nullable=False)
    filename       = Column(String(255))
    score          = Column(Integer, nullable=False)
    issue_count    = Column(Integer, default=0)
    critical_count = Column(Integer, default=0)
    latency_ms     = Column(Integer)
    model_used     = Column(String(100))          # ← was missing
    source         = Column(String(20), default="api")
    repo_name      = Column(String(255))
    pr_number      = Column(Integer)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

class IssueRecord(Base):
    __tablename__ = "issues"
    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id   = Column(UUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"))
    severity    = Column(String(20), nullable=False)
    category    = Column(String(20), nullable=False)
    line_number = Column(Integer)
    title       = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    fix         = Column(Text, nullable=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def save_review(review_id, language, score, issue_count,
                       latency_ms, model_used, source="api",
                       filename=None, critical_count=0,
                       repo_name=None, pr_number=None):
    async with AsyncSessionLocal() as session:
        try:
            stmt = pg_insert(Review).values(      # ← fixed: Review not ReviewRecord
                id=review_id,
                language=language,
                score=score,
                issue_count=issue_count,
                critical_count=critical_count,
                latency_ms=latency_ms,
                model_used=model_used,
                source=source,
                filename=filename,
                repo_name=repo_name,
                pr_number=pr_number,
            ).on_conflict_do_nothing(index_elements=["id"])

            await session.execute(stmt)
            await session.commit()
        except Exception as e:
            await session.rollback()
            print(f"DB save skipped: {e}")
"""Async SQLAlchemy models + engine.

v1 hard-required Postgres and used ``postgresql.insert`` unconditionally, so the
app could not run (or be tested) without a database server. This version speaks
SQLite by default and keeps Postgres working, and the upsert is dialect-aware.
"""
import os
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func


def _async_url(raw: str) -> str:
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+asyncpg://", 1)
    if raw.startswith("sqlite://") and "+aiosqlite" not in raw:
        return raw.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return raw


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./buglens.db")
ASYNC_DATABASE_URL = _async_url(DATABASE_URL)

_is_sqlite = ASYNC_DATABASE_URL.startswith("sqlite")
engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    pool_pre_ping=not _is_sqlite,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class Review(Base):
    __tablename__ = "reviews"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    language = Column(String(50), nullable=False)
    filename = Column(String(255))
    score = Column(Integer, nullable=False)
    issue_count = Column(Integer, default=0)
    critical_count = Column(Integer, default=0)
    latency_ms = Column(Integer)
    model_used = Column(String(100))
    source = Column(String(20), default="api")
    repo_name = Column(String(255))
    pr_number = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class IssueRecord(Base):
    __tablename__ = "issues"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    review_id = Column(String(36), ForeignKey("reviews.id", ondelete="CASCADE"))
    severity = Column(String(20), nullable=False)
    category = Column(String(20), nullable=False)
    line_number = Column(Integer)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    fix = Column(Text, nullable=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def save_review(review_id, language, score, issue_count, latency_ms, model_used,
                      source="api", filename=None, critical_count=0,
                      repo_name=None, pr_number=None):
    params = dict(
        id=str(review_id), language=str(language), score=int(score),
        issue_count=int(issue_count), critical_count=int(critical_count),
        latency_ms=int(latency_ms or 0), model_used=model_used, source=source,
        filename=filename, repo_name=repo_name, pr_number=pr_number,
    )
    async with AsyncSessionLocal() as session:
        try:
            stmt = insert(Review).values(**params)
            if engine.dialect.name == "postgresql":
                stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)
            await session.commit()
        except Exception as e:
            await session.rollback()
            print(f"DB save skipped: {e}")

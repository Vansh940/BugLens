from dotenv import load_dotenv
load_dotenv()  # must be first line before any other imports

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from buglens import github, integrations, llm, pipeline, store
from core.config import is_production, validate_config
from core.security import get_trusted_client_ip
from middleware.ip_tracking import IPTrackingMiddleware
from models.database import init_db
from routers import analytics, chat, feedback, history, review, webhook

logger = logging.getLogger("buglens")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await store.init()
    problems = validate_config()
    for problem in problems:
        logger.error("Configuration problem: %s", problem)
    if problems and is_production():
        raise RuntimeError("Refusing to start with an unsafe configuration: " + "; ".join(problems))
    logger.info("BugLens engine up: model=%s, max concurrent reviews=%s",
                llm.model_name(), pipeline.MAX_ACTIVE_REVIEWS)
    sweeper = asyncio.create_task(_sweep_loop())
    yield
    sweeper.cancel()
    with suppress(asyncio.CancelledError):
        await sweeper
    await llm.aclose()               # close the shared HTTP pools cleanly
    await github.aclose()
    await integrations.aclose()
    await store.close_pool()         # aiosqlite worker threads are non-daemon


SWEEP_HOURS = float(os.getenv("BUGLENS_SWEEP_HOURS", "24"))


async def _sweep_loop() -> None:
    """Housekeeping: expire the file cache and drop index history for dormant repos.

    Runs in-process on a timer, no cron, no new service. Errors are logged, never raised,
    so a failed sweep cannot take the app down.
    """
    while True:
        try:
            removed = await store.sweep()
            if any(removed.values()):
                logger.info("Sweep removed %s", removed)
        except Exception as exc:
            logger.warning("Sweep failed: %s", exc)
        await asyncio.sleep(max(SWEEP_HOURS, 0.1) * 3600)


app = FastAPI(
    title="BugLens API",
    description="Diff-native, multi-lens, self-verifying code review engine (PR bot + IDE + dashboard).",
    version="2.0.0",
    lifespan=lifespan,
)

# ─── Rate limiting (Redis when configured, in-memory otherwise) ────────────
limiter = Limiter(
    key_func=get_trusted_client_ip,
    storage_uri=os.getenv("REDIS_URL") or "memory://",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─── CORS, scoped to real origins only ───────────────────────────────────
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Admin-Key", "X-Extension-Key"],
)

app.add_middleware(IPTrackingMiddleware)

app.include_router(review.router)
app.include_router(webhook.router)
app.include_router(history.router)
app.include_router(chat.router)
app.include_router(feedback.router)
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["analytics"])

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "BugAnalytics.html"


@app.get("/dashboard")
def serve_dashboard():
    return FileResponse(DASHBOARD_PATH)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    import traceback
    logging.getLogger("buglens").error(
        "Unhandled exception on %s %s\n%s",
        request.method, request.url.path, traceback.format_exc(),
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
@app.head("/health")
def health_check():
    return {"status": "ok"}

from dotenv import load_dotenv
load_dotenv()  # must be first line before any other imports

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from routers import review, webhook, history, chat, analytics
from middleware.ip_tracking import IPTrackingMiddleware
from models.database import init_db
from core.security import get_trusted_client_ip

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()   # Create DB tables on startup
    yield

app = FastAPI(
    title="AI Code Reviewer",
    description="Review code for bugs, security issues, and style violations using Groq + LLaMA 3.3 70B",
    version="1.0.0",
    lifespan=lifespan
)

# ─── Rate limiting (Redis-backed via REDIS_URL, keyed on the trusted IP) ──
limiter = Limiter(
    key_func=get_trusted_client_ip,
    storage_uri=os.getenv("REDIS_URL", "redis://localhost:6379"),
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─── CORS — scoped to real origins only ───────────────────────────────────
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]
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
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["analytics"])

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "BugAnalytics.html"

@app.get("/dashboard")
def serve_dashboard():
    return FileResponse(DASHBOARD_PATH)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    import logging, traceback
    logging.getLogger("buglens").error(
        "Unhandled exception on %s %s\n%s",
        request.method, request.url.path, traceback.format_exc(),
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

@app.get("/health")
@app.head("/health")
def health_check():
    return {"status": "ok"}

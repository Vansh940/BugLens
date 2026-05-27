from dotenv import load_dotenv
load_dotenv()  # must be first line before any other imports

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import review, webhook, history, chat
from models.database import init_db

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Lock this down in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(review.router)
app.include_router(webhook.router)
app.include_router(history.router)
app.include_router(chat.router)   # ← add this line

@app.get("/health")
@app.head("/health")
def health_check():
    return {"status": "ok"}
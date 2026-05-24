import os
import json
import uuid
import asyncio
import hashlib
from groq import AsyncGroq, RateLimitError
from models.schemas import ReviewRequest, ReviewResponse
from prompts.review_prompt import SYSTEM_PROMPT, build_user_prompt
from services.cache_service import get_cached_review, cache_review

client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
GROQ_MODEL = "llama-3.3-70b-versatile"

def get_code_hash(code: str, language: str) -> str:
    return hashlib.sha256(f"{language}:{code}".encode()).hexdigest()

def strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    return text

async def call_groq_with_retry(messages: list, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            completion = await client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=4096,
                temperature=0.2,
                messages=messages
            )
            return completion.choices[0].message.content
        except RateLimitError:
            if attempt < retries - 1:
                wait = 2 ** attempt
                await asyncio.sleep(wait)
            else:
                raise

async def review_code(request: ReviewRequest) -> ReviewResponse:
    code_hash = get_code_hash(request.code, request.language)

    # Try cache — skip if stale/invalid
    try:
        cached = await get_cached_review(code_hash)
        if cached:
            return ReviewResponse(**cached)
    except Exception as e:
        print(f"Cache miss or stale: {e}")

    # Build prompt and call Groq
    user_prompt = build_user_prompt(
        code=request.code,
        language=request.language,
        filename=request.filename,
        context=request.context
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt}
    ]

    raw = await call_groq_with_retry(messages)
    raw = strip_json_fences(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Groq returned invalid JSON: {e}\nRaw: {raw[:300]}")

    data["review_id"]  = str(uuid.uuid4())
    data["model_used"] = GROQ_MODEL

    response = ReviewResponse(**data)

    # Cache the fresh result
    try:
        await cache_review(code_hash, response.dict())
    except Exception as e:
        print(f"Cache write failed (non-fatal): {e}")

    return response   # ← always returns here now
import os
import json
import uuid
import hashlib
import google.generativeai as genai
from models.schemas import ReviewRequest, ReviewResponse
from prompts.review_prompt import SYSTEM_PROMPT, build_user_prompt
from services.cache_service import get_cached_review, cache_review

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel(
    model_name="gemini-2.0-flash",
    system_instruction=SYSTEM_PROMPT
)

CURRENT_MODEL = "gemini-2.0-flash"

def get_code_hash(code: str, language: str) -> str:
    return hashlib.sha256(f"{language}:{code}".encode()).hexdigest()

def strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    return text

async def review_code(request: ReviewRequest) -> ReviewResponse:
    code_hash = get_code_hash(request.code, request.language)

    try:
        cached = await get_cached_review(code_hash)
        if cached:
            return ReviewResponse(**cached)
    except Exception as e:
        print(f"Cache miss or stale: {e}")

    user_prompt = build_user_prompt(
        code=request.code,
        language=request.language,
        filename=request.filename,
        context=request.context
    )

    try:
        response = model.generate_content(user_prompt)
        raw = strip_json_fences(response.text)
    except Exception as e:
        raise ValueError(f"Gemini API error: {e}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Gemini returned invalid JSON: {e}\nRaw: {raw[:300]}")

    data["review_id"]  = str(uuid.uuid4())
    data["model_used"] = CURRENT_MODEL

    result = ReviewResponse(**data)

    try:
        await cache_review(code_hash, result.dict())
    except Exception as e:
        print(f"Cache write failed (non-fatal): {e}")

    return result
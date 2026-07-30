import os
import json
import uuid
import asyncio
import hashlib
from groq import AsyncGroq, RateLimitError
from models.schemas import ReviewRequest, ReviewResponse
from prompts.review_prompt import SYSTEM_PROMPT, build_user_prompt
from services.cache_service import get_cached_review, cache_review

GROQ_MODEL = "openai/gpt-oss-120b"

# ─── Key rotation setup ───────────────────────────────────────────────────────
def _load_api_keys() -> list[str]:
    """Load all available API keys from environment variables."""
    keys = []
    # Support both single key and numbered keys
    single = os.getenv("GROQ_API_KEY")
    if single:
        keys.append(single)
    for i in range(1, 6):
        key = os.getenv(f"GROQ_API_KEY_{i}")
        if key:
            keys.append(key)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    if not unique:
        raise RuntimeError("No GROQ API keys found. Set GROQ_API_KEY or GROQ_API_KEY_1..5 in .env")
    return unique

API_KEYS = _load_api_keys()
_current_key_index = 0

def _get_client() -> AsyncGroq:
    """Return a client using the current active key."""
    return AsyncGroq(api_key=API_KEYS[_current_key_index])

def _rotate_key() -> bool:
    """
    Rotate to the next available key.
    Returns True if a new key is available, False if all keys are exhausted.
    """
    global _current_key_index
    if _current_key_index < len(API_KEYS) - 1:
        _current_key_index += 1
        print(f"[BugLens] Rate limit hit — rotating to API key {_current_key_index + 1}/{len(API_KEYS)}")
        return True
    return False

def _reset_key_index():
    """Reset to first key (called at startup or manually)."""
    global _current_key_index
    _current_key_index = 0

# ─── Helpers (unchanged) ──────────────────────────────────────────────────────
def get_code_hash(code: str, language: str) -> str:
    return hashlib.sha256(f"{language}:{code}".encode()).hexdigest()

def strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    return text

# ─── Groq call with key rotation ─────────────────────────────────────────────
async def call_groq_with_retry(messages: list, retries: int = 3) -> str:
    keys_tried = 0

    while keys_tried < len(API_KEYS):
        client = _get_client()
        for attempt in range(retries):
            try:
                completion = await client.chat.completions.create(
                    model=GROQ_MODEL,
                    max_tokens=4096,
                    temperature=0,
                    messages=messages,
                    response_format={"type": "json_object"}
                )
                return completion.choices[0].message.content

            except RateLimitError:
                rotated = _rotate_key()
                if rotated:
                    keys_tried += 1
                    break
                else:
                    raise Exception("All API keys have hit their rate limits. Please wait before retrying.")

            except Exception as e:
                error_str = str(e)
                # 413 = request too large — truncate and retry with smaller input
                if '413' in error_str or 'Request too large' in error_str:
                    print(f"[BugLens] Request too large — reduce MAX_CODE_CHARS in review_prompt.py")
                    raise Exception("Code is too large to review. Please select a specific function or section.")
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    print(f"[BugLens] Request failed (attempt {attempt + 1}): {e}. Retrying in {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    raise

        keys_tried += 1

    raise Exception("All API keys exhausted.")

# ─── Main review function (completely unchanged) ──────────────────────────────
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

    return response
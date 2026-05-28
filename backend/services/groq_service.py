import os
import json
import uuid
import asyncio
import hashlib
from groq import AsyncGroq, RateLimitError
from models.schemas import ReviewRequest, ReviewResponse
from prompts.review_prompt import SYSTEM_PROMPT, build_user_prompt
from services.cache_service import get_cached_review, cache_review

# Small/medium files — best quality
PRIMARY_MODEL = "llama-3.3-70b-versatile"
# Large files — higher TPM limit, still good quality
LARGE_MODEL   = "llama-3.1-8b-instant"
# Switch to faster model above this many lines
LARGE_FILE_THRESHOLD = 200

# ─── Key rotation setup ───────────────────────────────────────────────────────
def _load_api_keys() -> list[str]:
    keys = []
    single = os.getenv("GROQ_API_KEY")
    if single:
        keys.append(single)
    for i in range(1, 6):
        key = os.getenv(f"GROQ_API_KEY_{i}")
        if key:
            keys.append(key)
    seen, unique = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    if not unique:
        raise RuntimeError("No GROQ API keys found. Set GROQ_API_KEY in .env")
    return unique

API_KEYS = _load_api_keys()
_current_key_index = 0

def _get_client() -> AsyncGroq:
    return AsyncGroq(api_key=API_KEYS[_current_key_index])

def _rotate_key() -> bool:
    global _current_key_index
    if _current_key_index < len(API_KEYS) - 1:
        _current_key_index += 1
        print(f"[BugLens] Rotating to API key {_current_key_index + 1}/{len(API_KEYS)}")
        return True
    return False

# ─── Helpers ──────────────────────────────────────────────────────────────────
def get_code_hash(code: str, language: str) -> str:
    return hashlib.sha256(f"{language}:{code}".encode()).hexdigest()

def strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    return text

def safe_parse_json(raw: str) -> dict:
    """Try multiple strategies to parse JSON — never crash."""
    # Strategy 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Strategy 2: allow control characters
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        pass
    # Strategy 3: extract outermost { }
    try:
        start = raw.index('{')
        depth, end = 0, start
        for i, ch in enumerate(raw[start:], start):
            if ch == '{':   depth += 1
            elif ch == '}': depth -= 1
            if depth == 0:  end = i; break
        return json.loads(raw[start:end + 1], strict=False)
    except (ValueError, json.JSONDecodeError):
        pass
    raise ValueError(f"Cannot parse JSON. Raw start: {raw[:200]}")

def build_fallback_response() -> dict:
    """Safe fallback when everything fails — extension never crashes."""
    return {
        "summary": "Review could not be completed. The file may be too large. Try selecting a specific function.",
        "score": 50,
        "issues": [{
            "severity": "suggestion",
            "category": "style",
            "line_number": None,
            "title": "File too large to review completely",
            "description": "Select 50-200 lines and press Ctrl+Shift+R to review that section.",
            "fix": "Select a specific function or section for accurate review."
        }],
        "refactored_code": None,
        "positive_aspects": ["File received successfully"]
    }

# ─── Groq call with key rotation + model fallback ────────────────────────────
async def call_groq_with_retry(
    messages: list,
    model: str = PRIMARY_MODEL,
    retries: int = 3
) -> str:
    keys_tried = 0

    while keys_tried < len(API_KEYS):
        client = _get_client()
        for attempt in range(retries):
            try:
                completion = await client.chat.completions.create(
                    model=model,
                    max_tokens=2048,    # reduced from 4096 — keeps responses tight
                    temperature=0.1,
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
                    raise Exception("All API keys have hit rate limits. Please wait.")

            except Exception as e:
                # 413 = token limit exceeded — don't retry, raise immediately
                if "413" in str(e) or "Request too large" in str(e):
                    raise
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    print(f"[BugLens] Attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    raise

        keys_tried += 1

    raise Exception("All API keys exhausted.")

# ─── Main review function ─────────────────────────────────────────────────────
async def review_code(request: ReviewRequest) -> ReviewResponse:
    code_hash = get_code_hash(request.code, request.language)

    # Check cache first
    try:
        cached = await get_cached_review(code_hash)
        if cached:
            return ReviewResponse(**cached)
    except Exception as e:
        print(f"Cache miss: {e}")

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

    # Pick model based on file size
    line_count = request.code.count('\n')
    model = LARGE_MODEL if line_count > LARGE_FILE_THRESHOLD else PRIMARY_MODEL
    print(f"[BugLens] Reviewing {line_count} lines with {model}")

    try:
        raw = await call_groq_with_retry(messages, model=model)
        raw = strip_json_fences(raw)
        data = safe_parse_json(raw)

    except Exception as e:
        # If 413 token limit — retry with faster model (higher TPM)
        if "413" in str(e) or "Request too large" in str(e) or "tokens" in str(e).lower():
            print(f"[BugLens] Token limit hit with {model}, retrying with {LARGE_MODEL}")
            try:
                raw  = await call_groq_with_retry(messages, model=LARGE_MODEL)
                raw  = strip_json_fences(raw)
                data = safe_parse_json(raw)
                model = LARGE_MODEL
            except Exception as e2:
                print(f"[BugLens] Fallback model also failed: {e2}")
                data  = build_fallback_response()
        else:
            print(f"[BugLens] Review failed: {e}")
            data = build_fallback_response()

    # Add metadata
    data["review_id"]  = str(uuid.uuid4())
    data["model_used"] = model

    # Safe defaults for optional fields
    data.setdefault("refactored_code", None)
    data.setdefault("positive_aspects", [])

    # Validate with Pydantic
    try:
        response = ReviewResponse(**data)
    except Exception as e:
        print(f"[BugLens] Pydantic validation failed: {e}")
        fallback = build_fallback_response()
        fallback["review_id"]  = data.get("review_id", str(uuid.uuid4()))
        fallback["model_used"] = model
        response = ReviewResponse(**fallback)

    # Cache result
    try:
        await cache_review(code_hash, response.dict())
    except Exception as e:
        print(f"[BugLens] Cache write failed (non-fatal): {e}")

    return response
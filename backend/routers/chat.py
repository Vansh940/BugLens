from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional
import asyncio
from groq import AsyncGroq, RateLimitError
from services.groq_service import API_KEYS   # ← reuse same keys, no duplication

router = APIRouter(prefix="/api/v1", tags=["chat"])
GROQ_MODEL = "llama-3.3-70b-versatile"

# ─── Same rotation logic as groq_service.py ──────────────────────────────────
_chat_key_index = 0

def _get_chat_client() -> AsyncGroq:
    return AsyncGroq(api_key=API_KEYS[_chat_key_index])

def _rotate_chat_key() -> bool:
    global _chat_key_index
    if _chat_key_index < len(API_KEYS) - 1:
        _chat_key_index += 1
        print(f"[BugLens Chat] Rate limit hit — rotating to key {_chat_key_index + 1}/{len(API_KEYS)}")
        return True
    return False

# ─── Models (unchanged) ───────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role:    str
    content: str

class ChatRequest(BaseModel):
    code:           str
    language:       str
    filename:       Optional[str] = None
    review_summary: Optional[str] = None
    messages:       List[ChatMessage]

class ChatResponse(BaseModel):
    reply: str

# ─── Endpoint ─────────────────────────────────────────────────────────────────
@router.post("/chat", response_model=ChatResponse)
async def chat_about_code(request: ChatRequest):
    system_prompt = f"""You are BugLens, an expert code reviewer assistant.
The user is asking questions about this {request.language} code from {request.filename or 'their file'}.

CODE BEING DISCUSSED:
```{request.language}
{request.code[:4000]}
```
{f'REVIEW SUMMARY: {request.review_summary}' if request.review_summary else ''}

RULES:
- Answer questions about this specific code only
- Be concise and specific — reference exact line numbers
- If asked to explain an issue, give a clear real-world impact example
- If asked for a fix, provide exact working code
- Keep responses concise but complete — never cut off mid-sentence
- For code explanations, show the full fix even if it's longer
- Never make up issues that aren't in the code"""

    messages = [{"role": "system", "content": system_prompt}]
    for msg in request.messages:
        messages.append({"role": msg.role, "content": msg.content})

    # Try all available keys before giving up
    keys_tried = 0
    while keys_tried < len(API_KEYS):
        client = _get_chat_client()
        for attempt in range(3):
            try:
                completion = await client.chat.completions.create(
                    model=GROQ_MODEL,
                    max_tokens=1024,
                    temperature=0.3,
                    messages=messages
                )
                return ChatResponse(reply=completion.choices[0].message.content)

            except RateLimitError:
                rotated = _rotate_chat_key()
                if rotated:
                    keys_tried += 1
                    break   # try next key
                else:
                    return ChatResponse(reply="All API keys have hit their rate limits. Please wait a moment and try again.")

            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return ChatResponse(reply=f"Sorry, I couldn't process that. Error: {str(e)[:100]}")

        keys_tried += 1

    return ChatResponse(reply="All API keys exhausted. Please wait before trying again.")
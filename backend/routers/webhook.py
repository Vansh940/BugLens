import hmac
import hashlib
import os
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from services.github_service import get_pr_files, post_review_comment, format_review_as_markdown
from services.groq_service import review_code
from models.schemas import ReviewRequest

router = APIRouter(prefix="/api/v1", tags=["webhook"])

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

REVIEWABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".go", ".rs", ".cpp", ".c",
    ".html", ".css", ".scss", ".sh", ".rb",
    ".php", ".swift", ".kt", ".cs", ".dart"
}

EXT_TO_LANG = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "jsx": "javascript", "tsx": "typescript", "java": "java",
    "go": "go", "rs": "rust", "cpp": "cpp", "c": "c",
    "html": "html", "css": "css", "scss": "css",
    "sh": "bash", "yml": "yaml", "yaml": "yaml",
    "json": "json", "rb": "python", "php": "python",
    "swift": "python", "kt": "java", "cs": "python",
    "dart": "python",
}

def verify_signature(payload: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

async def process_pr(owner: str, repo: str, pr_number: int):
    try:
        files = await get_pr_files(owner, repo, pr_number)
    except Exception as e:
        print(f"Failed to fetch PR files: {e}")
        return

    for file in files:
        filename = file.get("filename", "")
        ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""

        if ext not in REVIEWABLE_EXTENSIONS:
            continue

        patch = file.get("patch", "")
        if not patch or len(patch) < 20:
            continue

        # ← Fixed: convert extension to full language name
        lang = EXT_TO_LANG.get(ext.lstrip("."), "python")

        try:
            request = ReviewRequest(
                code=patch,
                language=lang,
                filename=filename,
                context=f"Git diff from pull request in {repo}"
            )
            review = await review_code(request)
            comment = format_review_as_markdown(filename, review)
            await post_review_comment(owner, repo, pr_number, comment)
        except Exception as e:
            print(f"Review failed for {filename}: {e}")
            continue

@router.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(body, sig):
        raise HTTPException(401, "Invalid webhook signature")
    payload = await request.json()
    event = request.headers.get("X-GitHub-Event")
    if event == "pull_request" and payload.get("action") in ("opened", "synchronize"):
        pr = payload["pull_request"]
        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        pr_number = pr["number"]
        background_tasks.add_task(process_pr, owner, repo, pr_number)
    return {"status": "processing"}
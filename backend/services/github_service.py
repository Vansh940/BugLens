import httpx
import os

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

REVIEWABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".go", ".rs", ".cpp", ".c",
    ".html", ".css", ".scss", ".sh", ".rb",
    ".php", ".swift", ".kt", ".cs", ".dart"
}

async def get_pr_files(owner: str, repo: str, pr_number: int) -> list:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=HEADERS)
        print(f"Get PR files status: {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

async def post_review_comment(owner: str, repo: str,
                               pr_number: int, body: str) -> None:
    # Post as a regular issue comment (works without commit SHA)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers=HEADERS,
            json={"body": body}
        )
        print(f"Post comment status: {resp.status_code} — {resp.text[:200]}")

def format_review_as_markdown(filename: str, review) -> str:
    emoji = {"critical": "🔴", "warning": "🟡", "suggestion": "🔵"}
    score_emoji = "✅" if review.score >= 80 else "⚠️" if review.score >= 60 else "❌"

    lines = [
        f"## {score_emoji} BugLens Review: `{filename}`",
        f"**Score:** `{review.score}/100` &nbsp;|&nbsp; "
        f"**Issues:** `{len(review.issues)}` &nbsp;|&nbsp; "
        f"**Model:** `{review.model_used}`",
        "",
        f"> {review.summary}",
        "",
    ]

    if review.issues:
        lines.append("### Issues Found")
        for issue in review.issues:
            e = emoji.get(issue.severity, "⚪")
            lines.append(f"\n#### {e} [{issue.severity.upper()}] {issue.title}")
            if issue.line_number:
                lines.append(f"*Line {issue.line_number}*")
            lines.append(f"\n{issue.description}")
            lines.append(f"\n**Fix:** `{issue.fix}`")

    if review.positive_aspects:
        lines.append("\n### ✨ What's Good")
        for p in review.positive_aspects:
            lines.append(f"- {p}")

    lines.append("\n---")
    lines.append("*Reviewed by [BugLens](https://marketplace.visualstudio.com/items?itemName=VanshDev.buglens)*")

    return "\n".join(lines)
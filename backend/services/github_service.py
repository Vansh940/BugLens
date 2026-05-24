import httpx
import os

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

async def get_pr_files(owner: str, repo: str, pr_number: int) -> list:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=HEADERS)
        response.raise_for_status()
        return response.json()

async def post_review_comment(owner: str, repo: str,
                               pr_number: int, body: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    payload = {"body": body, "event": "COMMENT"}
    async with httpx.AsyncClient() as client:
        await client.post(url, headers=HEADERS, json=payload)

def format_review_as_markdown(filename: str, review) -> str:
    emoji_map = {"critical": "🔴", "warning": "🟡", "suggestion": "🔵"}
    lines = [
        f"## 🤖 AI Review (Groq · LLaMA 3.3 70B): `{filename}`",
        f"**Score:** {review.score}/100",
        f"**Summary:** {review.summary}",
        ""
    ]
    if review.issues:
        lines.append("### Issues Found")
        for issue in review.issues:
            e = emoji_map.get(issue.severity, "⚪")
            lines.append(f"{e} **[{issue.severity.upper()}] {issue.title}**")
            if issue.line_number:
                lines.append(f"*Line {issue.line_number}*")
            lines.append(issue.description)
            lines.append(f"> **Fix:** {issue.fix}")
            lines.append("")
    if review.positive_aspects:
        lines.append("### What's Good")
        for p in review.positive_aspects:
            lines.append(f"- {p}")
    return "\n".join(lines)
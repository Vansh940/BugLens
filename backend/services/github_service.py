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
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers=HEADERS,
            json={"body": body}
        )
        print(f"Post comment status: {resp.status_code} — {resp.text[:200]}")

async def get_diff_type(owner: str, repo: str, base_sha: str, head_sha: str) -> str:
    """Compare base..head and return a label describing what kind of change this PR contains."""
    url = f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=HEADERS)
    if resp.status_code != 200:
        return ""
    files = resp.json().get("files", [])
    if not files:
        return ""
    statuses = {f["status"] for f in files}
    if statuses == {"added"}:
        return "🆕 **Completely new code** — no existing files were modified"
    elif "added" not in statuses:
        modified = len(files)
        return f"✏️ **Modifications to existing code** — {modified} file(s) changed"
    else:
        added = sum(1 for f in files if f["status"] == "added")
        modified = sum(1 for f in files if f["status"] in ("modified", "renamed"))
        return f"🔀 **Mixed changes** — {added} new file(s) + {modified} modified file(s)"

def format_review_as_markdown(filename: str, review, diff_label: str = "") -> str:
    emoji = {"critical": "🔴", "warning": "🟡", "suggestion": "🔵"}
    score_emoji = "✅" if review.score >= 80 else "⚠️" if review.score >= 60 else "❌"

    lines = [
        f"## {score_emoji} BugLens Review: `{filename}`",
        f"**Score:** `{review.score}/100` &nbsp;|&nbsp; "
        f"**Issues:** `{len(review.issues)}` &nbsp;|&nbsp; "
        f"**Model:** `{review.model_used}`",
    ]

    if diff_label:
        lines.append(f"\n{diff_label}")

    lines += [
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
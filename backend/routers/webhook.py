"""GitHub webhook receiver (v2).

Hardened against the v1 failure modes:

* fail-closed signature check (v1 skipped verification when the secret was unset),
* filters events/actions and bot/draft PRs so we never review noise,
* hands the PR off to the v2 engine instead of shipping a raw unified diff to a
  model and guessing line numbers.
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from buglens import chat, github, pipeline, store
from core.security import verify_github_signature

logger = logging.getLogger("buglens")
router = APIRouter(prefix="/api/v1", tags=["webhook"])

REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MENTION_RE = re.compile(r"@buglens\b", re.IGNORECASE)


def _is_bot(user: dict | None) -> bool:
    user = user or {}
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]") or login in {"dependabot", "renovate"}


async def process_pr(owner: str, repo: str, number: int, base_sha: str, head_sha: str,
                     delivery_id: str | None, force: bool = False) -> None:
    try:
        result = await pipeline.review_pr(
            owner, repo, number, base_sha=base_sha, head_sha=head_sha,
            delivery_id=delivery_id, force=force,
        )
        logger.info(
            "BugLens reviewed %s/%s#%s: status=%s score=%s findings=%d posted=%s",
            owner, repo, number, result.status, result.score, len(result.findings), result.posted,
        )
    except Exception:
        logger.exception("BugLens review crashed for %s/%s#%s", owner, repo, number)


def _owner_repo_number(payload: dict) -> tuple[str, str, int] | None:
    repo_obj = payload.get("repository") or {}
    owner = (repo_obj.get("owner") or {}).get("login", "")
    repo = repo_obj.get("name", "")
    number = (payload.get("pull_request") or payload.get("issue") or {}).get("number")
    if not (NAME_RE.match(owner) and NAME_RE.match(repo)) or not isinstance(number, int):
        return None
    return owner, repo, number


@router.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    if not verify_github_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Invalid webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"status": "pong"}
    if event not in ("pull_request", "issue_comment", "reaction", "pull_request_review_comment"):
        return {"status": "ignored", "reason": f"event:{event}"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Malformed JSON payload")

    if "reaction" in payload:
        reaction = payload.get("reaction") or {}
        content = reaction.get("content")
        comment = payload.get("comment") or {}
        slug = (payload.get("repository") or {}).get("full_name") or ""
        fingerprint = pipeline.extract_fingerprint(comment.get("body") or "")
        if content in ("+1", "-1") and slug and fingerprint:
            await store.init()
            await store.record_outcome(slug, fingerprint, "up" if content == "+1" else "down")
            return {"status": "feedback-recorded", "verdict": "up" if content == "+1" else "down"}
        return {"status": "ignored", "reason": "reaction"}

    if not github.configured():
        raise HTTPException(503, "GITHUB_TOKEN not configured")

    # ── on-demand review: "@buglens" in a PR comment ──────────────────────────
    if event == "issue_comment":
        if payload.get("action") != "created":
            return {"status": "ignored", "reason": f"action:{payload.get('action')}"}
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        if not issue.get("pull_request"):
            return {"status": "ignored", "reason": "not-a-pr"}
        if _is_bot(comment.get("user")):
            return {"status": "ignored", "reason": "bot-comment"}
        if not MENTION_RE.search(comment.get("body") or ""):
            return {"status": "ignored", "reason": "no-mention"}
        target = _owner_repo_number(payload)
        if not target:
            raise HTTPException(400, "Malformed repository payload")
        owner, repo, number = target
        command, _arg = chat.parse_command(comment.get("body") or "")
        if command == "review":
            background_tasks.add_task(process_pr, owner, repo, number, "", "", request.headers.get("X-GitHub-Delivery"), True)
            return {"status": "processing", "pr": f"{owner}/{repo}#{number}", "trigger": "mention"}
        # Anything else is a conversation: answer in the thread, do not re-review.
        background_tasks.add_task(chat.handle_command, owner, repo, number, comment.get("body") or "")
        return {"status": "chatting", "pr": f"{owner}/{repo}#{number}", "command": command}

    # ── threaded Q&A on an inline review comment: "@buglens why" ──────────────
    if event == "pull_request_review_comment":
        if payload.get("action") != "created":
            return {"status": "ignored", "reason": f"action:{payload.get('action')}"}
        comment = payload.get("comment") or {}
        if _is_bot(comment.get("user")):
            return {"status": "ignored", "reason": "bot-comment"}
        if not MENTION_RE.search(comment.get("body") or ""):
            return {"status": "ignored", "reason": "no-mention"}
        target = _owner_repo_number(payload)
        if not target:
            raise HTTPException(400, "Malformed repository payload")
        owner, repo, number = target
        command, _arg = chat.parse_command(comment.get("body") or "")
        if command == "review":
            background_tasks.add_task(process_pr, owner, repo, number, "", "", request.headers.get("X-GitHub-Delivery"), True)
            return {"status": "processing", "pr": f"{owner}/{repo}#{number}", "trigger": "review-comment"}
        background_tasks.add_task(chat.handle_command, owner, repo, number, comment.get("body") or "")
        return {"status": "chatting", "pr": f"{owner}/{repo}#{number}", "command": command}

    # ── merged PR: measure how many of our findings were actually resolved ────
    if payload.get("action") == "closed":
        pr = payload.get("pull_request") or {}
        if not pr.get("merged"):
            return {"status": "ignored", "reason": "closed-unmerged"}
        target = _owner_repo_number(payload)
        if not target:
            raise HTTPException(400, "Malformed repository payload")
        owner, repo, number = target
        head_sha = pr.get("merge_commit_sha") or (pr.get("head") or {}).get("sha", "")
        background_tasks.add_task(pipeline.measure_resolution, owner, repo, number, head_sha)
        return {"status": "measuring-resolution", "pr": f"{owner}/{repo}#{number}"}

    # ── automatic review on PR lifecycle events ───────────────────────────────
    action = payload.get("action")
    if action not in REVIEW_ACTIONS:
        return {"status": "ignored", "reason": f"action:{action}"}

    pr = payload.get("pull_request") or {}
    if pr.get("draft"):
        return {"status": "ignored", "reason": "draft"}
    if _is_bot(pr.get("user")):
        return {"status": "ignored", "reason": "bot-author"}

    target = _owner_repo_number(payload)
    if not target:
        raise HTTPException(400, "Malformed repository payload")
    owner, repo, number = target

    base_sha = (pr.get("base") or {}).get("sha", "")
    head_sha = (pr.get("head") or {}).get("sha", "")
    delivery_id = request.headers.get("X-GitHub-Delivery")

    background_tasks.add_task(process_pr, owner, repo, number, base_sha, head_sha, delivery_id)
    return {"status": "processing", "pr": f"{owner}/{repo}#{number}"}

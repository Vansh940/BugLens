from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["blocker", "major", "minor", "nit"]
LegacySeverity = Literal["critical", "warning", "suggestion"]


class ReviewRequest(BaseModel):
    code: str
    language: str = "plaintext"
    filename: Optional[str] = None
    context: Optional[str] = None
    focus: Optional[List[str]] = None
    # Repo awareness (v2, optional): lets IDE reviews pull the same code-graph /
    # prior-review context the PR bot uses. Old clients simply omit these.
    repo: Optional[str] = None          # "owner/name"
    branch: Optional[str] = None
    imports: Optional[List[str]] = None               # local module specs this file imports
    related: Optional[dict[str, str]] = None          # path -> excerpt the IDE already read


class Issue(BaseModel):
    severity: str
    category: str
    line_number: Optional[int] = None
    title: str
    description: str
    fix: str = ""
    # v2 additions (optional so v1 clients keep working)
    evidence: Optional[str] = None
    confidence: Optional[int] = None
    fingerprint: Optional[str] = None
    lens: Optional[str] = None


class ReviewResponse(BaseModel):
    summary: str
    score: int
    issues: List[Issue]
    refactored_code: Optional[str] = None
    positive_aspects: List[str] = Field(default_factory=list)
    review_id: str
    model_used: str = "unknown"


class FeedbackRequest(BaseModel):
    """Outcome signal used by the self-improvement loop (store.py)."""

    repo: str
    fingerprint: str
    verdict: Literal["good", "bad"]
    pr_number: Optional[int] = None
    note: Optional[str] = None

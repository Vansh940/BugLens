from pydantic import BaseModel
from typing import List, Optional
from enum import Enum

class Language(str, Enum):
    python     = "python"
    javascript = "javascript"
    typescript = "typescript"
    java       = "java"
    go         = "go"
    rust       = "rust"
    cpp        = "cpp"
    c          = "c"
    json       = "json"      # ← add this
    yaml       = "yaml"      # ← add this
    html       = "html"      # ← add this
    css        = "css"       # ← add this
    bash       = "bash"      # ← add this

class ReviewRequest(BaseModel):
    code: str
    language: Language
    filename: Optional[str] = None
    context:  Optional[str] = None

class Issue(BaseModel):
    severity:    str
    category:    str
    line_number: Optional[int] = None
    title:       str
    description: str
    fix:         str

class ReviewResponse(BaseModel):
    summary:          str
    score:            int
    issues:           List[Issue]
    refactored_code:  Optional[str] = None
    positive_aspects: List[str]
    review_id:        str
    model_used:       str = "llama-3.3-70b-versatile"  # ← add default value
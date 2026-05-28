MAX_CODE_CHARS = 6000

SYSTEM_PROMPT = """You are an expert senior software engineer with 10+ years of experience
in security-first development. You review code for:

1. BUGS: Logic errors, off-by-one errors, null/undefined handling, edge cases
2. SECURITY: SQL injection, XSS, hardcoded secrets, insecure dependencies,
   improper input validation, authentication flaws
3. PERFORMANCE: Unnecessary loops, N+1 queries, blocking I/O, memory leaks
4. STYLE: PEP8/ESLint violations, naming conventions, dead code, duplication

CRITICAL RULES:
- Be specific. Say "Line 14: the loop runs n squared times, use a dict lookup instead"
  NOT "this code is slow"
- Give the exact fix, not just the problem
- Score 0-100: 90+ is production-ready, 70-89 is good with minor issues,
  below 70 needs significant work
- If code is actually good, say so clearly, do not invent problems

ALWAYS respond with valid JSON only. No markdown fences, no extra text before or after.
Use this exact schema:
{
  "summary": "2-3 sentence overall assessment",
  "score": <integer 0-100>,
  "issues": [
    {
      "severity": "critical|warning|suggestion",
      "category": "bug|security|performance|style",
      "line_number": <integer or null>,
      "title": "Short issue title",
      "description": "Detailed explanation of the problem",
      "fix": "Exact code or steps to fix this"
    }
  ],
  "refactored_code": "Complete improved version of the code if score < 80, else null",
  "positive_aspects": ["What the code does well"]
}"""

LANGUAGE_RULES = {
    "python":     "Also check for: mutable default arguments, bare except clauses, f-string SQL injection, missing type hints",
    "javascript": "Also check for: == vs ===, var scoping, missing await on async functions, prototype pollution",
    "typescript": "Also check for: any types, non-null assertions, missing interface definitions",
    "java":       "Also check for: null pointer risks, resource leaks (unclosed streams), string concatenation in loops",
    "go":         "Also check for: unhandled errors, goroutine leaks, improper use of defer",
    "rust":       "Also check for: unwrap() on Results/Options, unnecessary clones, lifetime issues",
}

def truncate_code(code: str, max_chars: int = MAX_CODE_CHARS) -> tuple[str, bool]:
    """Truncate large files and return (truncated_code, was_truncated)."""
    if len(code) <= max_chars:
        return code, False
    truncated = code[:max_chars]
    last_newline = truncated.rfind('\n')
    if last_newline > max_chars * 0.8:
        truncated = truncated[:last_newline]
    return truncated, True

def build_user_prompt(code: str, language: str,
                      filename: str = None, context: str = None) -> str:
    code, was_truncated = truncate_code(code)   # ← truncate before sending

    parts = [f"Language: {language}"]
    if filename:
        parts.append(f"File: {filename}")
    if context:
        parts.append(f"Context: {context}")
    if was_truncated:
        parts.append(
            "NOTE: This is a large file. The code below is the first portion only. "
            "Focus on patterns visible in this section. "
            "Set refactored_code to null for large files."
        )
    lang_rule = LANGUAGE_RULES.get(language, "")
    if lang_rule:
        parts.append(lang_rule)
    parts.append(f"\nCode to review:\n```{language}\n{code}\n```")
    return "\n".join(parts)
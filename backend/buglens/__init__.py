"""BugLens review engine (v2).

A diff-native, context-aware, self-verifying, multi-lens PR reviewer:

    diff.py      unified-diff parsing + new-file line mapping + fingerprints
    severity.py  severity taxonomy, ranking, comment budgets
    rules.py     repo-local .buglens.yml rules
    context.py   cheap codebase index (tree, imports, symbols, conventions)
    lenses.py    the review experts (security, bugs, ui, merge, perf, tests)
    llm.py       provider-agnostic OpenAI-compatible client, key rotation, JSON repair
    prompts.py   review / verify / summary prompts (prompt-injection hardened)
    verify.py    machine + model verification gate that drops unproven findings
    github.py    GitHub REST client: PR files, file content, inline reviews, checks
    store.py     SQLite run/finding/delivery store (idempotency, dedupe, outcomes)
    pipeline.py  orchestration: plan -> context -> review -> verify -> post
"""

__all__ = [
    "diff", "severity", "rules", "context", "lenses",
    "llm", "prompts", "verify", "github", "store", "pipeline",
]
__version__ = "2.0.0"

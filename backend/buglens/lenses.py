"""Review lenses: BugLens reviews a diff through several specialised experts.

v1 asked one generic prompt for "issues". A PR that merges a security fix, a
frontend tweak and a broken API contract deserves a security engineer, a UI
reviewer and a merge gate, not one generalist. Each lens here is an expert
persona with its own focus text, its own comment floor and its own file scope.

Adding a lens is a data change, not a code change: append to LENSES and the
pipeline picks it up.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .severity import CATEGORIES

FRONTEND_EXTS = {"html", "css", "scss", "sass", "less", "jsx", "tsx", "vue", "svelte", "astro"}
FRONTEND_LANGS = {"javascript", "typescript", "html", "css"}
INFRA_EXTS = {"yml", "yaml", "tf", "dockerfile", "sh", "bash", "ps1", "toml", "json"}
SECURITY_EXTS = {"py", "js", "ts", "jsx", "tsx", "java", "go", "rs", "rb", "php", "cs", "kt", "swift", "c", "cpp", "sql", "sh", "yml", "yaml", "dockerfile"}
TEST_HINTS = ("test", "spec", "__tests__", "_test.", ".test.", ".spec.")


@dataclass(frozen=True)
class Lens:
    name: str
    emoji: str
    focus: str
    category: str
    default_min_confidence: float = 0.5
    default: bool = True                      # run on every file unless focus narrows
    exts: frozenset[str] = field(default_factory=frozenset)
    langs: frozenset[str] = field(default_factory=frozenset)

    def applies(self, path: str, language: str) -> bool:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        low = path.lower()
        if self.exts and ext not in self.exts:
            return False
        if self.langs and language not in self.langs:
            return False
        if self.name == "tests" and not any(h in low for h in TEST_HINTS) and self.category == "test":
            return False
        return True


# ── the experts ───────────────────────────────────────────────────────────────
CORRECTNESS = Lens(
    "correctness", "🐞",
    "logic errors, wrong operators, off-by-one, null/undefined handling, unhandled "
    "exceptions, resource leaks, race conditions, incorrect state transitions, "
    "silent-failure paths. Prefer bugs this diff introduces or exposes.",
    category="bug",
)
SECURITY = Lens(
    "security", "🔐",
    "injection (SQL/command/template/path), broken authn/authz, secrets in code, "
    "unsafe deserialization, SSRF, XSS, CSRF, insecure randomness, weak crypto, "
    "path traversal, overly broad permissions, prompt-injection concerns, and risky "
    "dependency/config changes. Treat the diff as hostile input.",
    category="security",
)
PERFORMANCE = Lens(
    "performance", "⚡",
    "accidental quadratic loops, N+1 queries, blocking IO on hot paths, unbounded "
    "growth, repeated work, needless copies/allocations, missing index use.",
    category="performance",
    default=False,
)
UI = Lens(
    "ui", "🎨",
    "frontend correctness: accessibility (labels, roles, focus, keyboard), color "
    "contrast, responsive layout, broken state/DOM handling, unescaped user data in "
    "templates, inconsistent spacing/typography, loading/empty/error states.",
    category="ui",
    exts=frozenset(FRONTEND_EXTS),
)
TESTS = Lens(
    "tests", "🧪",
    "changed behaviour without a matching test, assertions that cannot fail, tests "
    "that would not catch the bug being fixed, flaky timing/dependency on order.",
    category="test",
    default=False,
)
MAINTAINABILITY = Lens(
    "maintainability", "🧹",
    "dead code, duplication, misleading names, leaked abstractions, unclear "
    "contracts, TODO/FIXME left behind, debug prints.",
    category="maintainability",
    default=False,
)

LENSES: list[Lens] = [CORRECTNESS, SECURITY, PERFORMANCE, UI, TESTS, MAINTAINABILITY]
BY_NAME = {lens.name: lens for lens in LENSES}

# Repo-level (runs once per PR, not per file): the merge gate.
MERGE = Lens(
    "merge", "🔀",
    "merge readiness of the whole PR: unresolved conflict markers, base-branch drift, "
    "breaking public API/contract changes, files that look accidental (secrets, build "
    "output, editor files), lockfile/dependency churn without a source change, and "
    "whether the PR description matches what the diff actually does.",
    category="merge",
    default=True,
)


def select_lenses(path: str, language: str, focus: list[str] | None = None,
                  pool: list["Lens"] | None = None) -> list[Lens]:
    """Pick the experts for one file.

    `focus` (from .buglens.yml) is an allow-list; when present only those lenses run.
    Otherwise the defaults run, plus any lens whose scope (exts/langs) matches the
    file, that is how the UI lens turns on for frontend files and how a repo's
    custom lens (`.buglens/lenses/*.md`) turns on for its own file types.

    `pool` lets the caller add repo-defined lenses on top of the built-ins.
    """
    pool = pool if pool is not None else LENSES
    if focus:
        wanted = {f.strip().lower() for f in focus if f.strip()}
        return [lens for lens in pool if lens.name in wanted and lens.applies(path, language)]
    chosen = [lens for lens in pool if lens.default and lens.applies(path, language)]
    for lens in pool:
        if lens in chosen:
            continue
        if (lens.exts or lens.langs) and lens.applies(path, language):
            chosen.append(lens)
    return chosen


def parse_custom_lens(filename: str, text: str) -> Lens | None:
    """Parse a repo-defined lens from a small markdown file.

    Format (all optional except the prose):
        # SQL Safety            <- title becomes the lens name
        exts: sql              <- scope; empty/absent means "every file"
        category: security
        emoji: 🐘
        <free-form focus text the model receives>
    """
    title = filename.rsplit("/", 1)[-1].removesuffix(".md").replace("-", " ").strip() or "custom"
    exts: frozenset[str] = frozenset()
    category, emoji, body = "maintainability", "🧩", []
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("# "):
            title = line.strip()[2:].strip()
        elif low.startswith("exts:"):
            exts = frozenset(s.strip().lstrip(".") for s in line.split(":", 1)[1].split(",") if s.strip())
        elif low.startswith("category:"):
            category = line.split(":", 1)[1].strip().lower()
        elif low.startswith("emoji:"):
            emoji = line.split(":", 1)[1].strip()
        else:
            body.append(line)
    focus = " ".join(p.strip() for p in body if p.strip())[:600]
    if not focus:
        return None
    if category not in CATEGORIES:
        category = "maintainability"
    return Lens(
        name=title.lower().replace(" ", "-"),
        emoji=emoji,
        focus=focus,
        category=category,
        default=False,
        exts=exts,
    )


def load_custom_lenses(files: dict[str, str]) -> list[Lens]:
    """Build lenses from {path: text} where path lives under `.buglens/lenses/`."""
    out: list[Lens] = []
    for path, text in files.items():
        if "/lenses/" not in path.replace("\\", "/") or not path.endswith(".md"):
            continue
        lens = parse_custom_lens(path, text)
        if lens and lens.name not in BY_NAME and all(l.name != lens.name for l in out):
            out.append(lens)
    return out[:8]


def describe_all() -> list[dict]:
    return [{"name": l.name, "emoji": l.emoji, "category": l.category, "default": l.default} for l in LENSES]

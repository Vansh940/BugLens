"""Turn reaction feedback into a bounded prompt block and human-approved rule ideas.

The bot already records a thumbs up/down per finding fingerprint and silences a
fingerprint after it is down-voted twice. This module goes one level up: it reads the
aggregate reaction stats a repo has accumulated and (a) renders a small, hard-budgeted
block the review prompt can carry so the lenses stop repeating the noise the team keeps
dismissing, and (b) proposes candidate ``.buglens.yml`` rules that a human must approve,
never auto-applied.

Everything here is pure: plain dicts and lists in, strings and dicts out. No store, no
model, no network, no I/O, so the same input always gives the same output.
"""
from __future__ import annotations

MIN_SAMPLES = 5
DOWN_RATE = 0.5
MAX_SUGGESTIONS = 5
MAX_BLOCK_CHARS = 700

_HEADING = "## Feedback on this repo (learned from reactions; do not repeat dismissed noise)"
_BULLET_CAP = 4


def _rate(item: dict) -> float:
    raised = int(item.get("raised", 0) or 0)
    down = int(item.get("down", 0) or 0)
    return (down / raised) if raised else 0.0


def lens_ranking(stats: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(demoted, trusted) lens names.

    demoted = lenses with raised >= MIN_SAMPLES and (down / raised) >= DOWN_RATE.
    trusted = the rest that have raised >= MIN_SAMPLES, sorted by down/raised asc.
    Both lists are for lenses that appear in stats. Deterministic ordering: demoted by
    down/raised descending then name ascending; trusted by down/raised ascending then name.
    """
    demoted: list[str] = []
    trusted: list[str] = []
    for name, item in (stats or {}).items():
        if int(item.get("raised", 0) or 0) < MIN_SAMPLES:
            continue
        if _rate(item) >= DOWN_RATE:
            demoted.append(name)
        else:
            trusted.append(name)
    demoted.sort(key=lambda name: (-_rate(stats[name]), name))
    trusted.sort(key=lambda name: (_rate(stats[name]), name))
    return demoted, trusted


def order_lenses(lenses: list, demoted: set[str]) -> list:
    """Stable reorder: lenses whose .name is in demoted move to the end.

    Relative order within the demoted group and within the kept group is preserved.
    Never drops a lens, only demotes it. Returns a new list.
    """
    kept = [lens for lens in lenses if getattr(lens, "name", "") not in demoted]
    dropped = [lens for lens in lenses if getattr(lens, "name", "") in demoted]
    return kept + dropped


def learn_block(scopes: list[dict], stats: dict[str, dict],
                budget_chars: int = MAX_BLOCK_CHARS) -> str:
    """A bounded markdown advisory for the review prompt, or "" when there is nothing.

    Must start with the heading line exactly:
      ## Feedback on this repo (learned from reactions; do not repeat dismissed noise)
    Then, in this order:
      - one bullet per scope item (cap at 4): 
        "- the team repeatedly dismissed <lens> findings in `<scope>` (N times) - raise fewer of these"
        (N = downs - ups, but never print a number < 1; if downs-ups <= 0 skip the item)
      - if lens_ranking(stats)[0] is non-empty, one final line:
        "Lenses this repo down-votes most: a, b"
    Hard-truncate the whole thing to budget_chars. Return "" if no bullets were added
    (heading alone must never be returned).
    """
    bullets: list[str] = []
    ordered = sorted(
        (s for s in (scopes or []) if isinstance(s, dict)),
        key=lambda s: (str(s.get("scope") or ""), str(s.get("lens") or "")),
    )
    for item in ordered:
        n = int(item.get("downs", 0) or 0) - int(item.get("ups", 0) or 0)
        if n <= 0:
            continue
        bullets.append(
            f"- the team repeatedly dismissed {item.get('lens')} findings in "
            f"`{item.get('scope')}` ({n} times) - raise fewer of these"
        )
        if len(bullets) >= _BULLET_CAP:
            break
    demoted, _trusted = lens_ranking(stats)
    if demoted:
        bullets.append("Lenses this repo down-votes most: " + ", ".join(demoted))
    if not bullets:
        return ""
    return "\n".join([_HEADING, *bullets])[:budget_chars]


def candidate_rules(scopes: list[dict], stats: dict[str, dict],
                    min_downs: int = 3) -> list[dict]:
    """Human-approval suggestions for .buglens.yml. NEVER auto-applied.

    Returns at most MAX_SUGGESTIONS dicts, sorted by a stable key (scope asc, then lens asc),
    each shaped:
      {"id": str, "kind": "ignore-glob" | "focus-drop-lens", "scope": str | None,
       "lens": str | None, "reason": str, "yaml": str}

    - For each scope item with (downs - ups) >= min_downs: kind "ignore-glob",
      id "learn:ignore:<scope>", scope set, lens None, yaml is a snippet:
        "ignore_paths:\n  - \"<scope>\"   # <lens> findings dismissed <N> times"
      (2-space indent, the glob double-quoted, the trailing comment on the same line).
    - For each demoted lens (from lens_ranking(stats)[0]): kind "focus-drop-lens",
      id "learn:lens:<name>", lens set, scope None, yaml is a snippet:
        "# focus: []            # <name> down-voted on <pct>% of raised findings"
      percent = round(100 * down / raised).
    - `reason` is a short factual sentence naming the numbers (no filler, no buzzwords).
    Deduplicate by id; cap at MAX_SUGGESTIONS.
    """
    out: list[dict] = []
    for item in (scopes or []):
        if not isinstance(item, dict):
            continue
        n = int(item.get("downs", 0) or 0) - int(item.get("ups", 0) or 0)
        if n < min_downs:
            continue
        scope = str(item.get("scope") or "")
        lens = str(item.get("lens") or "")
        out.append({
            "id": f"learn:ignore:{scope}",
            "kind": "ignore-glob",
            "scope": scope,
            "lens": None,
            "reason": f"{lens} findings in {scope} were dismissed {n} more times than agreed.",
            "yaml": f'ignore_paths:\n  - "{scope}"   # {lens} findings dismissed {n} times',
        })
    demoted, _trusted = lens_ranking(stats)
    for name in demoted:
        item = stats.get(name, {})
        raised = int(item.get("raised", 0) or 0)
        down = int(item.get("down", 0) or 0)
        pct = round(100 * down / raised) if raised else 0
        out.append({
            "id": f"learn:lens:{name}",
            "kind": "focus-drop-lens",
            "scope": None,
            "lens": name,
            "reason": f"{name} findings were down-voted on {pct}% of {raised} raised findings.",
            "yaml": f"# focus: []            # {name} down-voted on {pct}% of raised findings",
        })
    out.sort(key=lambda d: (str(d.get("scope") or ""), str(d.get("lens") or "")))
    seen: set[str] = set()
    unique: list[dict] = []
    for item in out:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)
    return unique[:MAX_SUGGESTIONS]

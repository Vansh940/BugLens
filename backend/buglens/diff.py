"""Unified-diff parsing, new-file line mapping and finding fingerprints.

The v1 reviewer sent raw patch text to the model and reported "line_number" that
referred to a line inside the patch, so findings could never be attached to a
real PR line. Everything downstream depends on this module resolving a finding
to an *added line of the new file*, which is what GitHub inline comments need
(`path`, `line`, `side: RIGHT`).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

ADD, DEL, CTX = "add", "del", "ctx"


@dataclass
class DiffLine:
    kind: str                     # add | del | ctx
    text: str                     # line content without the leading +/-/space
    new_line: int | None          # line number in the new file (None for deletions)
    old_line: int | None
    hunk: int = 0
    raw_index: int = 0            # position in the parsed line list


@dataclass
class FileDiff:
    path: str
    language: str = "plaintext"
    status: str = "modified"
    lines: list[DiffLine] = field(default_factory=list)
    is_binary: bool = False
    truncated: bool = False

    @property
    def added(self) -> list[DiffLine]:
        return [l for l in self.lines if l.kind == ADD]

    @property
    def added_line_numbers(self) -> set[int]:
        return {l.new_line for l in self.added if l.new_line}

    @property
    def added_chars(self) -> int:
        return sum(len(l.text) + 1 for l in self.added)

    def new_file_text(self) -> str:
        """Best-effort reconstruction of the new file from the patch.

        Only lines the hunk carries are present; holes are marked so the model
        cannot mistake a gap for real code.
        """
        out, last = [], 0
        by_new = {l.new_line: l for l in self.lines if l.new_line and l.kind in (ADD, CTX)}
        for n in sorted(by_new):
            if n > last + 1:
                out.append(f"… {n - last - 1} unchanged line(s) omitted …")
            out.append(by_new[n].text)
            last = n
        return "\n".join(out)

    def window(self, line: int, before: int = 12, after: int = 12) -> str:
        """Numbered view of the new file around `line` (for verification prompts)."""
        lo, hi = line - before, line + after
        out = []
        for l in self.lines:
            if l.new_line and lo <= l.new_line <= hi:
                mark = " " if l.kind == CTX else "+"
                out.append(f"{l.new_line:>5} {mark} {l.text}")
        return "\n".join(out)


def parse_patch(patch: str, path: str = "unknown", status: str = "modified") -> FileDiff:
    """Parse one GitHub file entry's `patch` field."""
    fd = FileDiff(path=path, status=status)
    if not patch:
        return fd
    new_no = old_no = 0
    hunk = -1
    for idx, raw in enumerate(patch.splitlines()):
        m = HUNK_RE.match(raw)
        if m:
            hunk += 1
            old_no, new_no = int(m.group(1)), int(m.group(3))
            continue
        if hunk < 0:                      # header noise (diff --git, index, ---, +++)
            continue
        if raw.startswith("\\"):          # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            fd.lines.append(DiffLine(ADD, raw[1:], new_no, None, hunk, idx)); new_no += 1
        elif raw.startswith("-"):
            fd.lines.append(DiffLine(DEL, raw[1:], None, old_no, hunk, idx)); old_no += 1
        elif raw.startswith(" ") or raw == "":
            fd.lines.append(DiffLine(CTX, raw[1:] if raw else "", new_no, old_no, hunk, idx))
            new_no += 1; old_no += 1
    return fd


def snap_to_added(fd: FileDiff, line: int | None, tolerance: int = 6) -> int | None:
    """Map a model-reported line onto a real added line of the new file."""
    added = sorted(fd.added_line_numbers)
    if not added:
        return None
    if line is None:
        return added[0]
    if line in added:
        return line
    best = min(added, key=lambda n: abs(n - line))
    return best if abs(best - line) <= tolerance else None


def anchor_to_evidence(fd: FileDiff, quote: str, prefer: int | None = None, min_chars: int = 8) -> int | None:
    """The added line that actually contains the finding's quoted evidence.

    This is the position-drift gate: a model can quote the right line but report the
    wrong line number, and a comment pinned to the wrong line is the single most common
    "this bot is useless" complaint. When the evidence matches an added line we trust
    that over the reported number. Ties (the same quote on several added lines) resolve
    to the line closest to what the model claimed.
    """
    if not quote or len(quote.strip()) < min_chars:
        return None
    needle = re.sub(r"\s+", " ", quote).strip()
    candidates = [l.new_line for l in fd.added
                  if l.new_line and needle and needle in re.sub(r"\s+", " ", l.text).strip()]
    if not candidates:
        return None
    if prefer is None:
        return candidates[0]
    return min(candidates, key=lambda n: abs(n - prefer))


def join_added(fd: FileDiff) -> str:
    return "\n".join(l.text for l in fd.added)


def added_text_contains(fd: FileDiff, quote: str) -> bool:
    """Does the finding's evidence quote actually appear in the added code?

    Whitespace-insensitive: the model reflows indentation often enough that a
    strict compare would drop true findings.
    """
    if not quote or len(quote.strip()) < 8:
        return False
    norm = lambda s: re.sub(r"\s+", " ", s).strip()          # noqa: E731
    hay = norm(join_added(fd))
    needle = norm(quote)
    return needle in hay


def fingerprint(path: str, category: str, title: str) -> str:
    """Stable id for a finding, so later pushes of the same PR don't re-post it."""
    norm = re.sub(r"[^a-z0-9 ]", "", (title or "").lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(f"{path}|{category}|{norm}".encode()).hexdigest()[:32]


def choose_paths(files: list[FileDiff], ignore_globs, reviewable_ext: set[str], max_files: int):
    """Pick the files worth spending tokens on, biggest change first."""
    from .rules import path_ignored

    picked, skipped = [], []
    for fd in files:
        ext = "." + fd.path.rsplit(".", 1)[-1].lower() if "." in fd.path else ""
        why = None
        if path_ignored(fd.path, ignore_globs):
            why = "ignored by rules"
        elif ext not in reviewable_ext:
            why = f"unsupported file type {ext or '(none)'}"
        elif not fd.added_line_numbers:
            why = "no added lines (rename/binary)"
        elif fd.added_chars < 20:
            why = "trivial change"
        if why:
            skipped.append({"path": fd.path, "reason": why})
        else:
            picked.append(fd)
    picked.sort(key=lambda f: f.added_chars, reverse=True)
    overflow = [{"path": f.path, "reason": "over max_files budget"} for f in picked[max_files:]]
    return picked[:max_files], skipped + overflow

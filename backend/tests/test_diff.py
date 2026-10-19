from buglens.diff import (
    added_text_contains,
    choose_paths,
    fingerprint,
    parse_patch,
    snap_to_added,
)

PATCH = """@@ -1,4 +1,6 @@
 def f(x):
-    return x
+    if x is None:
+        return 0
+    return x + 1
"""


def test_parses_new_file_line_numbers():
    fd = parse_patch(PATCH, "a.py")
    assert fd.added_line_numbers == {2, 3, 4}
    deleted = [l for l in fd.lines if l.kind == "del"]
    assert deleted and deleted[0].old_line == 2


def test_snap_to_added_tolerates_small_drift_and_rejects_far():
    fd = parse_patch(PATCH, "a.py")
    assert snap_to_added(fd, 3) == 3
    assert snap_to_added(fd, 5) == 4          # within tolerance
    assert snap_to_added(fd, 99) is None      # too far -> hallucinated line
    assert snap_to_added(fd, None) == 2       # no line -> first added line


def test_evidence_match_is_whitespace_insensitive():
    fd = parse_patch(PATCH, "a.py")
    assert added_text_contains(fd, "    if   x is None:")
    assert not added_text_contains(fd, "os.system(")


def test_fingerprint_is_stable_and_distinct():
    assert fingerprint("a.py", "bug", "Null deref") == fingerprint("a.py", "bug", "null  deref!")
    assert fingerprint("a.py", "bug", "Null deref") != fingerprint("a.py", "security", "Null deref")


def test_choose_paths_skips_ignored_and_unsupported():
    py = parse_patch(PATCH, "src/a.py")
    md = parse_patch(PATCH, "docs/readme.md")
    tiny = parse_patch("@@ -1 +1 @@\n-a\n+x\n", "b.py")
    picked, skipped = choose_paths([py, md, tiny], ["vendor/*"], {".py"}, 10)
    assert [f.path for f in picked] == ["src/a.py"]
    reasons = {s["path"]: s["reason"] for s in skipped}
    assert "docs/readme.md" in reasons and "b.py" in reasons

"""Fail-closed parsing for Product Patch inspection."""

from traceh.artifacts.unified_diff import PatchLineKind, parse_unified_diff


def test_complete_patch_projects_file_status_totals_and_line_numbers() -> None:
    content = b"""diff --git a/added.py b/added.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/added.py
@@ -0,0 +1,2 @@
+first
+second
diff --git a/changed.py b/changed.py
index 2222222..3333333 100644
--- a/changed.py
+++ b/changed.py
@@ -1,2 +1,2 @@
 keep
-old
+new
diff --git a/deleted.py b/deleted.py
deleted file mode 100644
index 4444444..0000000
--- a/deleted.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
-also gone
"""

    parsed = parse_unified_diff(
        content,
        expected_paths=("added.py", "changed.py", "deleted.py"),
    )

    assert parsed.summary.complete
    assert parsed.summary.additions == 3
    assert parsed.summary.deletions == 3
    assert tuple(
        (item.path, item.status, item.additions, item.deletions)
        for item in parsed.summary.files
    ) == (
        ("added.py", "added", 2, 0),
        ("changed.py", "modified", 1, 1),
        ("deleted.py", "deleted", 0, 2),
    )
    changed = parsed.files[1]
    assert tuple(
        (line.kind, line.old_line, line.new_line, line.text)
        for line in changed.lines
    ) == (
        (PatchLineKind.CONTEXT, 1, 1, "keep"),
        (PatchLineKind.DELETION, 2, None, "old"),
        (PatchLineKind.ADDITION, None, 2, "new"),
    )


def test_unicode_line_separators_and_bidi_stay_in_one_untrusted_body_line() -> None:
    body = "visible\u2028still-here\u2029bidi\u202eend"
    content = (
        "diff --git a/control.txt b/control.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/control.txt\n"
        "@@ -0,0 +1 @@\n"
        f"+{body}\n"
    ).encode()

    parsed = parse_unified_diff(content, expected_paths=("control.txt",))

    assert parsed.summary.complete
    assert len(parsed.files[0].lines) == 1
    assert parsed.files[0].lines[0].text == body


def test_malformed_hunk_or_manifest_path_mismatch_has_no_partial_numbers() -> None:
    malformed = b"""diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1,2 +1,2 @@
-old
+new
"""

    for expected_paths in (("value.py",), ("another.py",)):
        parsed = parse_unified_diff(
            malformed,
            expected_paths=expected_paths,
        )
        assert not parsed.summary.complete
        assert tuple(
            (item.path, item.status, item.additions, item.deletions)
            for item in parsed.summary.files
        ) == ((expected_paths[0], "unknown", None, None),)
        assert parsed.summary.additions is None
        assert parsed.summary.deletions is None
        assert parsed.files == ()


def test_binary_patch_keeps_proven_file_identity_but_not_line_counts() -> None:
    content = b"""diff --git a/picture.bin b/picture.bin
new file mode 100644
index 0000000..1111111
GIT binary patch
literal 3
KcmZQzU|?Vb0000
"""

    parsed = parse_unified_diff(content, expected_paths=("picture.bin",))

    assert not parsed.summary.complete
    assert parsed.summary.additions is None
    assert parsed.summary.deletions is None
    assert len(parsed.summary.files) == 1
    summary = parsed.summary.files[0]
    assert summary.path == "picture.bin"
    assert summary.status == "added"
    assert summary.binary
    assert summary.additions is None
    assert summary.deletions is None
    assert all(
        line.kind is PatchLineKind.METADATA for line in parsed.files[0].lines
    )
    assert tuple(line.text for line in parsed.files[0].lines) == (
        "GIT binary patch",
        "literal 3",
        "KcmZQzU|?Vb0000",
    )


def test_non_utf8_body_is_retained_for_the_safe_display_boundary() -> None:
    parsed = parse_unified_diff(
        b"diff --git a/value.py b/value.py\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/value.py\n"
        b"@@ -0,0 +1 @@\n"
        b"+\xff\n",
        expected_paths=("value.py",),
    )

    assert parsed.summary.complete
    assert parsed.summary.additions == 1
    assert parsed.summary.deletions == 0
    assert parsed.files[0].lines[0].text == "\udcff"


def test_git_c_quoted_utf8_path_is_bound_to_the_manifest_path() -> None:
    content = br"""diff --git "a/\344\270\255.py" "b/\344\270\255.py"
--- "a/\344\270\255.py"
+++ "b/\344\270\255.py"
@@ -1 +1 @@
-old
+new
"""

    parsed = parse_unified_diff(content, expected_paths=("\u4e2d.py",))

    assert parsed.summary.complete
    assert parsed.summary.files[0].path == "\u4e2d.py"
    assert parsed.summary.additions == 1
    assert parsed.summary.deletions == 1


def test_mode_and_no_newline_metadata_are_preserved_without_diff_headers() -> None:
    mode_only = b"""diff --git a/script.py b/script.py
old mode 100644
new mode 100755
"""
    mode = parse_unified_diff(mode_only, expected_paths=("script.py",))
    assert mode.summary.complete
    assert mode.summary.additions == 0
    assert mode.summary.deletions == 0
    assert tuple((line.kind, line.text) for line in mode.files[0].lines) == (
        (PatchLineKind.METADATA, "old mode 100644"),
        (PatchLineKind.METADATA, "new mode 100755"),
    )

    no_newline = br"""diff --git a/value.txt b/value.txt
index 1111111..2222222 100644
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
\ No newline at end of file
+new
\ No newline at end of file
"""
    parsed = parse_unified_diff(no_newline, expected_paths=("value.txt",))
    assert parsed.summary.complete
    assert tuple(line.kind for line in parsed.files[0].lines) == (
        PatchLineKind.DELETION,
        PatchLineKind.METADATA,
        PatchLineKind.ADDITION,
        PatchLineKind.METADATA,
    )
    assert all(
        not line.text.startswith(("diff --git", "index ", "--- ", "+++ "))
        for line in parsed.files[0].lines
    )

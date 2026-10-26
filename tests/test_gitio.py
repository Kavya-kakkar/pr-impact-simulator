from __future__ import annotations

from pathlib import Path

import pytest

from pr_impact import gitio

SAMPLE_DIFF = """diff --git a/pkg/mod.py b/pkg/mod.py
index 1111111..2222222 100644
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -3,0 +4,2 @@ def f():
+    value = 1
+    return value
@@ -10,2 +12,2 @@ def g():
-    old = 1
+    new = 2
     keep = 3
"""


def test_parse_diff_line_numbers():
    diffs = gitio.parse_diff(SAMPLE_DIFF)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.path == "pkg/mod.py"
    assert diff.status == "modified"
    assert len(diff.hunks) == 2

    first = diff.hunks[0]
    assert first.new_start == 4
    assert [line.new_line for line in first.lines] == [4, 5]
    assert diff.added_lines() == {4, 5, 12}

    second = diff.hunks[1]
    removals = [line for line in second.lines if line.kind == "-"]
    additions = [line for line in second.lines if line.kind == "+"]
    assert removals[0].old_line == 10
    assert additions[0].new_line == 12
    assert diff.deleted_lines() == {10}
    context = [line for line in second.lines if line.kind == " "]
    assert context[0].old_line == 11
    assert context[0].new_line == 13


def test_parse_diff_new_and_deleted_files():
    text = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+print("hi")
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-print("bye")
"""
    diffs = gitio.parse_diff(text)
    assert [diff.status for diff in diffs] == ["added", "deleted"]
    assert diffs[0].added_lines() == {1}
    assert diffs[1].deleted_lines() == {1}


def test_parse_diff_rename():
    text = """diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
"""
    diffs = gitio.parse_diff(text)
    assert diffs[0].status == "renamed"
    assert diffs[0].path == "new.py"
    assert diffs[0].old_path == "old.py"


def test_parse_diff_ignores_diff_content_markers():
    text = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
--- not a header
+++ also content
 same
"""
    diffs = gitio.parse_diff(text)
    assert len(diffs[0].hunks) == 1
    assert [line.text for line in diffs[0].hunks[0].lines] == [
        "-- not a header",
        "++ also content",
        "same",
    ]


def test_repo_helpers(fixture_repo):
    repo = fixture_repo.repo
    assert gitio.is_git_repo(repo)
    files = gitio.list_files(repo, "main")
    assert "README.md" in files
    assert "sample_app/services.py" in files

    head = gitio.resolve_rev(repo, "pr/signature-break")
    assert len(head) == 40
    assert gitio.short_sha(repo, head) == head[:12]

    diffs = gitio.diff_files(repo, "main", "pr/signature-break")
    assert [diff.path for diff in diffs] == ["sample_app/services.py"]

    content = gitio.read_file(repo, "main", "sample_app/services.py")
    assert content is not None and b"def create_user" in content
    assert gitio.read_file(repo, "main", "does/not/exist.py") is None


def test_unknown_revision_raises(fixture_repo):
    with pytest.raises(gitio.GitError):
        gitio.resolve_rev(fixture_repo.repo, "no-such-branch")


def test_repo_root_detection(fixture_repo):
    nested = Path(fixture_repo.repo) / "sample_app"
    assert gitio.repo_root(nested) == Path(fixture_repo.repo).resolve()
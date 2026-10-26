from __future__ import annotations

from pathlib import Path

from pr_impact.cov import CoverageOutcome, _relative_source, correlate
from pr_impact.model import DiffLine, FileDiff, Hunk


def _diff() -> FileDiff:
    lines = (
        DiffLine("+", None, 1, "a"),
        DiffLine("+", None, 2, "b"),
        DiffLine("+", None, 3, "c"),
    )
    hunk = Hunk(old_start=0, old_len=0, new_start=1, new_len=3, lines=lines)
    return FileDiff(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,))


def test_correlate_reports_uncovered_added_lines():
    head = CoverageOutcome(
        available=True,
        revision="head",
        executed={"pkg/mod.py": {2}},
        missing={"pkg/mod.py": {1, 3}},
        percent=50.0,
        tests_passed=True,
    )
    base = CoverageOutcome(available=True, revision="base", percent=80.0, tests_passed=True)
    report = correlate(
        diffs=[_diff()],
        head=head,
        base=base,
        base_label="base",
        head_label="head",
    )
    assert report.available
    assert report.head_percent == 50.0
    assert report.base_percent == 80.0
    assert report.uncovered_added_lines == 2
    assert report.files[0].uncovered_added == (1, 3)
    assert report.files[0].covered_added == 1


def test_unavailable_coverage_is_reported_not_estimated():
    head = CoverageOutcome(available=False, revision="head", limitations=["boom"])
    base = CoverageOutcome(available=False, revision="base", limitations=["boom base"])
    report = correlate(
        diffs=[_diff()],
        head=head,
        base=base,
        base_label="base",
        head_label="head",
    )
    assert not report.available
    assert report.files == []
    assert report.uncovered_added_lines == 0
    assert any("boom" in limitation for limitation in report.limitations)
    assert any("omitted rather than estimated" in item for item in report.limitations)


def test_test_outcome_mismatch_is_a_limitation():
    head = CoverageOutcome(
        available=True,
        revision="head",
        executed={"pkg/mod.py": {1, 2, 3}},
        tests_passed=False,
    )
    base = CoverageOutcome(
        available=True, revision="base", executed={"pkg/mod.py": {1}}, tests_passed=True
    )
    report = correlate(
        diffs=[_diff()],
        head=head,
        base=base,
        base_label="base",
        head_label="head",
    )
    assert any("not under equivalent conditions" in item for item in report.limitations)
    assert report.uncovered_added_lines == 0


def test_deleted_and_non_python_files_are_skipped():
    python_deleted = FileDiff(
        path="pkg/mod.py", old_path="pkg/mod.py", status="deleted", hunks=()
    )
    text_file = FileDiff(path="README.md", old_path=None, status="modified", hunks=())
    head = CoverageOutcome(available=True, revision="head", executed={"pkg/mod.py": {1}})
    report = correlate(
        diffs=[python_deleted, text_file],
        head=head,
        base=CoverageOutcome(available=True, revision="base"),
        base_label="base",
        head_label="head",
    )
    assert report.files == []


def test_relative_source_accepts_absolute_and_relative_paths(tmp_path: Path):
    worktree = tmp_path / "tree"
    source = worktree / "pkg" / "mod.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")

    assert _relative_source(str(source), worktree) == "pkg/mod.py"
    assert _relative_source("pkg/mod.py", worktree) == "pkg/mod.py"
    assert _relative_source(str(tmp_path / "other.py"), worktree) is None
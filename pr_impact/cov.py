"""Coverage execution and diff correlation.

Coverage is executed in throwaway ``git worktree`` checkouts of both revisions
with the same interpreter and command. When execution is impossible the report
says so; no coverage number is ever synthesised.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .gitio import GitError, run_git
from .model import CoverageFile, CoverageReport, FileDiff

COVERAGE_COMMAND = "python -m coverage run -m pytest -q"
DEFAULT_TIMEOUT = 180


def _default_timeout() -> int:
    raw = os.environ.get("PR_IMPACT_COVERAGE_TIMEOUT")
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_TIMEOUT


@dataclass
class CoverageOutcome:
    available: bool
    revision: str
    executed: dict[str, set[int]] = field(default_factory=dict)
    missing: dict[str, set[int]] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    tests_passed: bool | None = None
    percent: float | None = None


def run_coverage(repo: Path, revision: str, timeout: int | None = None) -> CoverageOutcome:
    timeout = timeout or _default_timeout()
    outcome = CoverageOutcome(available=False, revision=revision)
    temp_root = Path(tempfile.mkdtemp(prefix="pr-impact-cov-"))
    worktree = temp_root / "tree"
    coverage_file = temp_root / ".coverage"
    try:
        try:
            run_git(repo, "worktree", "add", "--detach", str(worktree), revision)
        except GitError as exc:
            outcome.limitations.append(f"could not create coverage worktree: {exc}")
            return outcome

        env = {
            **os.environ,
            "COVERAGE_FILE": str(coverage_file),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "",
        }
        try:
            run = subprocess.run(
                [sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q"],
                cwd=worktree,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            outcome.limitations.append("python interpreter for coverage was not found")
            return outcome
        except subprocess.TimeoutExpired:
            outcome.limitations.append(
                f"coverage run timed out after {timeout}s at revision {revision}"
            )
            return outcome

        if run.returncode == 0:
            outcome.tests_passed = True
        elif run.returncode == 5:
            outcome.tests_passed = None
            outcome.limitations.append(
                f"no tests were collected at revision {revision} (pytest exit 5)"
            )
        else:
            outcome.tests_passed = False
            tail = (run.stdout or "")[-800:] + (run.stderr or "")[-800:]
            outcome.limitations.append(
                f"pytest exited {run.returncode} at revision {revision}; coverage "
                "still collected but the environment is not green:\n" + tail.strip()
            )

        if not coverage_file.exists():
            outcome.limitations.append(
                f"no coverage data was produced at revision {revision}"
            )
            return outcome

        report_path = temp_root / "coverage.json"
        report = subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                "-o",
                str(report_path),
                "--pretty-print",
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        if report.returncode != 0 or not report_path.exists():
            outcome.limitations.append(
                "coverage json report failed: "
                + (report.stderr or report.stdout or "").strip()[-500:]
            )
            return outcome

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        for raw_path, data in payload.get("files", {}).items():
            relative = _relative_source(raw_path, worktree)
            if relative is None:
                continue
            outcome.executed[relative] = set(data.get("executed_lines", []))
            outcome.missing[relative] = set(data.get("missing_lines", []))
        totals = payload.get("totals", {})
        if isinstance(totals.get("percent_covered"), (int, float)):
            outcome.percent = round(float(totals["percent_covered"]), 2)
        outcome.available = bool(outcome.executed)
        if not outcome.available:
            outcome.limitations.append(
                f"coverage collected no executed source lines at revision {revision}"
            )
        return outcome
    finally:
        try:
            run_git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        except (GitError, FileNotFoundError):
            pass
        shutil.rmtree(temp_root, ignore_errors=True)


def correlate(
    *,
    diffs: list[FileDiff],
    head: CoverageOutcome,
    base: CoverageOutcome,
    base_label: str,
    head_label: str,
) -> CoverageReport:
    report = CoverageReport(
        available=head.available,
        base_rev=base_label,
        head_rev=head_label,
        command=COVERAGE_COMMAND,
        tests_passed=head.tests_passed,
        base_percent=base.percent,
        head_percent=head.percent,
        base_tests_passed=base.tests_passed,
    )
    report.limitations.extend(head.limitations)
    if not base.available:
        report.limitations.extend(base.limitations)
    if (
        base.available
        and head.available
        and base.tests_passed is not None
        and head.tests_passed is not None
        and base.tests_passed != head.tests_passed
    ):
        report.limitations.append(
            "test outcomes differ between base and head; coverage comparison is "
            "not under equivalent conditions"
        )

    if not head.available:
        report.limitations.append(
            "coverage is unavailable; uncovered-line counts are omitted rather "
            "than estimated"
        )
        return report

    for diff in diffs:
        if not diff.path.endswith((".py", ".pyi")) or diff.status == "deleted":
            continue
        added = diff.added_lines()
        if not added:
            continue
        executed = head.executed.get(diff.path, set())
        missing = head.missing.get(diff.path, set())
        uncovered = tuple(sorted(line for line in added if line not in executed))
        covered = len(added) - len(uncovered)
        report.files.append(
            CoverageFile(
                path=diff.path,
                added_lines=tuple(sorted(added)),
                uncovered_added=uncovered,
                covered_added=covered,
                missing_lines=tuple(sorted(missing)),
            )
        )
    if report.files and report.uncovered_added_lines == 0:
        report.limitations.append(
            "every Python line added by this PR was executed by the test suite"
        )
    report.limitations.append(
        "regressions on unchanged lines are not attributed: line numbers shift "
        "between revisions, so only lines added by the PR are correlated"
    )
    return report


def _relative_source(raw_path: str, worktree: Path) -> str | None:
    path = Path(raw_path)
    if not path.is_absolute():
        path = worktree / path
    try:
        return path.resolve().relative_to(worktree.resolve()).as_posix()
    except ValueError:
        return None
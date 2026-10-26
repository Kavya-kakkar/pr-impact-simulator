"""Git plumbing for the simulator.

Only read-only git operations plus throwaway worktrees are used. Nothing here
writes to the analysed repository's branches or index.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .model import DiffLine, FileDiff, Hunk

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$")


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    stdout: str
    returncode: int


def run_git(repo: Path, *args: str, check: bool = True) -> GitResult:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_PAGER": "cat"})
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if check and completed.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return GitResult(tuple(args), completed.stdout, completed.returncode)


def is_git_repo(repo: Path) -> bool:
    try:
        result = run_git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise GitError("git executable not found") from exc
    return result.returncode == 0 and result.stdout.strip() == "true"


def resolve_rev(repo: Path, rev: str) -> str:
    result = run_git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}", check=False)
    if result.returncode != 0:
        raise GitError(f"unknown revision: {rev}")
    return result.stdout.strip()


def short_sha(repo: Path, rev: str) -> str:
    return run_git(repo, "rev-parse", "--short=12", rev).stdout.strip()


def repo_root(repo: Path) -> Path:
    return Path(run_git(repo, "rev-parse", "--show-toplevel").stdout.strip())


def list_files(repo: Path, rev: str) -> list[str]:
    out = run_git(repo, "ls-tree", "-r", "--name-only", "-z", rev).stdout
    return sorted(path for path in out.split("\0") if path)


def read_file(repo: Path, rev: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


@dataclass
class _HunkBuilder:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    old_cursor: int
    new_cursor: int
    lines: list[DiffLine] = field(default_factory=list)

    def build(self) -> Hunk:
        return Hunk(
            old_start=self.old_start,
            old_len=self.old_len,
            new_start=self.new_start,
            new_len=self.new_len,
            lines=tuple(self.lines),
        )


def parse_diff(text: str) -> list[FileDiff]:
    """Parse ``git diff --unified=0`` output into typed file diffs."""

    files: list[FileDiff] = []
    path: str | None = None
    old_path: str | None = None
    status = "modified"
    hunks: list[Hunk] = []
    current: _HunkBuilder | None = None

    def flush_file() -> None:
        nonlocal path, old_path, status, hunks, current
        if path is not None:
            files.append(
                FileDiff(
                    path=path,
                    old_path=old_path,
                    status=status,
                    hunks=tuple(hunks),
                )
            )
        path, old_path, status, hunks, current = None, None, "modified", [], None

    def flush_hunk() -> None:
        nonlocal current
        if current is not None:
            hunks.append(current.build())
            current = None

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            flush_hunk()
            flush_file()
            match = _DIFF_RE.match(raw)
            path = match.group(2) if match else raw.split(" b/", 1)[-1]
            old_path = match.group(1) if match else None
            continue
        if raw.startswith("new file mode"):
            status = "added"
            continue
        if raw.startswith("deleted file mode"):
            status = "deleted"
            continue
        if raw.startswith("rename from "):
            old_path = raw[len("rename from ") :]
            status = "renamed"
            continue
        if raw.startswith("rename to "):
            path = raw[len("rename to ") :]
            continue
        if raw.startswith("@@ "):
            flush_hunk()
            match = _HUNK_RE.match(raw)
            if not match:
                continue
            old_start = int(match.group(1))
            old_len = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_len = int(match.group(4) or "1")
            current = _HunkBuilder(
                old_start=old_start,
                old_len=old_len,
                new_start=new_start,
                new_len=new_len,
                old_cursor=old_start,
                new_cursor=new_start,
            )
            continue
        if current is None:
            continue
        if raw.startswith("\\"):
            continue
        old_line = current.old_cursor
        new_line = current.new_cursor
        if raw.startswith("+"):
            current.lines.append(DiffLine("+", None, new_line, raw[1:]))
            current.new_cursor = new_line + 1
            continue
        if raw.startswith("-"):
            current.lines.append(DiffLine("-", old_line, None, raw[1:]))
            current.old_cursor = old_line + 1
            continue
        if raw.startswith(" "):
            current.lines.append(DiffLine(" ", old_line, new_line, raw[1:]))
            current.old_cursor = old_line + 1
            current.new_cursor = new_line + 1
    flush_hunk()
    flush_file()
    return files


def diff_files(repo: Path, base: str, head: str) -> list[FileDiff]:
    result = run_git(
        repo,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--unified=0",
        "-M",
        base,
        head,
        "--",
        check=True,
    )
    return parse_diff(result.stdout)


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    paths: set[str] = set()
    for file_diff in diff_files(repo, base, head):
        paths.add(file_diff.path)
        if file_diff.old_path:
            paths.add(file_diff.old_path)
    return sorted(paths)


def log_subject(repo: Path, rev: str) -> str:
    result = run_git(repo, "log", "-1", "--format=%s", rev, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""
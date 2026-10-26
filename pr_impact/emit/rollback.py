"""Rollback checklist and generated pytest skeletons."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from ..model import (
    Change,
    ChangeKind,
    Classification,
    CoverageReport,
    Dossier,
    Evidence,
    TestSkeleton,
)
from ..parsing import module_name


def rollback_steps(
    changes: list[Change],
    coverage: CoverageReport,
    docs_drift: list[object],
    head_rev: str,
) -> list[str]:
    breaking = [c for c in changes if c.classification is Classification.BREAKING]
    needs_human = [c for c in changes if c.classification is Classification.NEEDS_HUMAN]
    steps: list[str] = [
        f"Revert the head commit(s) for `{head_rev}` on a branch and open a revert "
        "PR; do not force-push the contributor branch.",
    ]

    if not breaking:
        steps.append(
            "No breaking surface was detected: the revert is expected to restore "
            "the prior contract without a compatibility shim. Verify with the test "
            "suite before closing."
        )
    for change in breaking:
        symbol = change.symbol.split("::", 1)[-1]
        if change.kind is ChangeKind.FUNCTION_REMOVED:
            steps.append(
                f"Restore `{symbol}` with its previous signature "
                f"`{change.base_signature.render(symbol) if change.base_signature else ''}`; "
                f"evidence {_first_source(change)}."
            )
        elif change.kind is ChangeKind.SIGNATURE_CHANGED:
            steps.append(
                f"Restore the previous parameters of `{symbol}` "
                f"`{change.base_signature.render(symbol) if change.base_signature else ''}` "
                f"or add a shim that accepts both shapes; evidence {_first_source(change)}."
            )
        elif change.kind is ChangeKind.ROUTE_REMOVED:
            steps.append(
                f"Re-register route `{change.route_before.route_id if change.route_before else ''}` "
                f"for `{symbol}`; evidence {_first_source(change)}."
            )
        elif change.kind is ChangeKind.ROUTE_CHANGED:
            steps.append(
                f"Restore route "
                f"`{change.route_before.route_id if change.route_before else ''}` (was "
                f"changed to `{change.route_after.route_id if change.route_after else ''}`); "
                f"evidence {_first_source(change)}."
            )
        elif change.kind is ChangeKind.STATUS_CHANGED:
            steps.append(
                f"Restore status code "
                f"`{change.route_before.status_code if change.route_before else ''}` on "
                f"`{change.route_after.route_id if change.route_after else ''}`; "
                f"evidence {_first_source(change)}."
            )

    broken_consumers = {
        f"{consumer.path}:{consumer.line} ({consumer.symbol})"
        for change in breaking
        for consumer in change.consumers
        if consumer.impact == "broken"
    }
    if broken_consumers:
        steps.append(
            "Until the revert lands, fix or feature-flag these call sites: "
            + "; ".join(sorted(broken_consumers))
            + "."
        )

    if needs_human:
        steps.append(
            "Re-check these judgement calls with the owning team: "
            + "; ".join(
                f"{change.symbol.split('::', 1)[-1]} ({change.kind.value})"
                for change in needs_human
            )
            + "."
        )

    if coverage.available and coverage.uncovered_added_lines:
        steps.append(
            f"Add tests for the {coverage.uncovered_added_lines} newly added "
            "uncovered lines before re-landing, or record an explicit risk acceptance."
        )
    if not coverage.available:
        steps.append(
            "Coverage was unavailable: run the test suite manually against the "
            "revert and compare results before declaring the rollback complete."
        )
    if docs_drift:
        steps.append(
            "Revert or update the drifted documentation alongside the code so the "
            "README and API reference match the deployed revision."
        )
    steps.append(
        "Deploy the revert, verify the affected routes return their previous "
        "status codes, and re-run this analyser with `--no-coverage` to confirm "
        "no breaking surface remains."
    )
    return steps


def generate_skeletons(
    changes: list[Change],
    coverage: CoverageReport,
    head_rev: str,
) -> list[TestSkeleton]:
    if not coverage.available:
        return []
    uncovered_paths = {
        file.path: set(file.uncovered_added) for file in coverage.files if file.uncovered_added
    }
    skeletons: list[TestSkeleton] = []
    for change in changes:
        if change.kind not in (ChangeKind.FUNCTION_ADDED, ChangeKind.SIGNATURE_CHANGED):
            continue
        if not uncovered_paths.get(change.file):
            continue
        function_name = change.symbol.split("::", 1)[-1].split(".")[-1]
        module = module_name(change.file)
        stem = re.sub(r"[^A-Za-z0-9_]+", "_", function_name)
        path = f".pr-impact/tests/test_{stem}_contract.py"
        evidence = [
            Evidence("source", change.file, head_rev, None, None, detail="generated target")
            if not change.evidence
            else change.evidence[0]
        ]
        content = _skeleton_content(
            module=module,
            function_name=function_name,
            change=change,
            uncovered=sorted(uncovered_paths[change.file]),
        )
        skeletons.append(
            TestSkeleton(
                path=path,
                content=content,
                target_symbol=change.symbol,
                evidence=evidence,
            )
        )
    return skeletons


def _skeleton_content(
    *, module: str, function_name: str, change: Change, uncovered: list[int]
) -> str:
    signature = (
        change.head_signature.render(change.symbol.split("::", 1)[-1])
        if change.head_signature
        else "def (...)"
    )
    params = [
        param
        for param in (change.head_signature.params if change.head_signature else ())
        if param.kind in ("positional", "posonly", "keyword_only")
    ]
    args = ", ".join(
        f"{param.name}=..." if param.kind == "keyword_only" else "..."
        for param in params
    )
    lines = [
        '"""Generated contract-test skeleton - INCOMPLETE.',
        "",
        f"Target: {change.symbol}",
        f"Head signature: {signature}",
        f"Uncovered added lines at head: {uncovered}",
        "",
        "Generated by PR Impact Simulator. Fill in the TODOs; this file skips",
        "itself so it cannot turn the suite green by accident.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import pytest",
        "",
        f"from {module} import {function_name}  # TODO: confirm import path",
        "",
        'pytestmark = pytest.mark.skip(reason="TODO: implement generated contract tests")',
        "",
        "",
        f"def test_{function_name}_contract() -> None:",
        f"    # TODO: pin the contract that this PR changes; evidence "
            f"{change.evidence[0].loc() if change.evidence else change.file}.",
    ]
    if change.base_signature and change.head_signature:
        lines.append(
            f"    # Before: {change.base_signature.render(change.symbol.split('::', 1)[-1])}"
        )
        lines.append(
            f"    # After:  {change.head_signature.render(change.symbol.split('::', 1)[-1])}"
        )
    lines.extend(
        [
            f"    result = {function_name}({args})  # TODO: supply real arguments",
            "    assert result is not None  # TODO: replace with the real contract",
            "",
            "",
            f"def test_{function_name}_rejects_invalid_input() -> None:",
            "    # TODO: assert the documented error/status behaviour.",
            "    with pytest.raises(Exception):  # TODO: narrow the exception type",
            f"        {function_name}({args})",
            "",
        ]
    )
    if change.head_signature and change.head_signature.is_async:
        lines.append("# TODO: target is async; use an async test with a pytest plugin")
    return "\n".join(lines)


def _first_source(change: Change) -> str:
    for evidence in change.evidence:
        if evidence.kind == "source":
            return evidence.loc()
    return change.evidence[0].loc()
"""One-command local demo.

Builds the seeded sample repository, analyses each PR branch exactly as a
webhook would, writes the artifacts and prints the dossier that would be posted
as the PR comment. Exits non-zero if the seeded expectations are not met.

Usage::

    python demo/run_demo.py [--no-coverage] [--summary]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from build_fixture import build_fixture  # noqa: E402

from pr_impact.emit.dossier import render_dossier  # noqa: E402
from pr_impact.model import ChangeKind, Classification  # noqa: E402
from pr_impact.pipeline import AnalyzeOptions, analyze, write_artifacts  # noqa: E402

OUTPUT = ROOT / "demo" / "output"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-coverage", action="store_true")
    parser.add_argument("--summary", action="store_true", help="print only checks")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the fixture repo")
    args = parser.parse_args(argv)

    fixture = build_fixture(force=args.rebuild)
    options = AnalyzeOptions(coverage=not args.no_coverage, llm=False)
    failures: list[str] = []

    for name, branch in fixture.branches.items():
        started = time.perf_counter()
        dossier, _graphs = analyze(fixture.repo, "main", branch, options)
        elapsed = time.perf_counter() - started
        artifacts = write_artifacts(dossier, OUTPUT / name)
        if not args.summary:
            print("=" * 78)
            print(render_dossier(dossier))
            print("=" * 78)
        print(
            f"[{name}] {dossier.risk.band} {dossier.risk.total}/100 - "
            f"{len(dossier.breaking_changes)} breaking - "
            f"{dossier.coverage.uncovered_added_lines} uncovered new lines - "
            f"{len(dossier.doc_drift)} docs drift - {elapsed:.2f}s"
        )
        print(
            f"[{name}] artifacts: {artifacts['dossier']}, "
            f"{artifacts['rollback']}, {artifacts['json']}"
        )
        failures.extend(_check(name, dossier))

    if failures:
        print("\nSEEDED EXPECTATIONS NOT MET:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll seeded expectations met.")
    return 0


def _check(name: str, dossier) -> list[str]:
    failures: list[str] = []
    if name == "signature-break":
        signature_changes = [
            change
            for change in dossier.changes
            if change.kind is ChangeKind.SIGNATURE_CHANGED
            and "create_user" in change.symbol
        ]
        if not signature_changes:
            failures.append("signature-break: create_user signature change not detected")
        else:
            change = signature_changes[0]
            if change.classification is not Classification.BREAKING:
                failures.append("signature-break: change was not classified breaking")
            broken = {
                consumer.symbol
                for consumer in change.consumers
                if consumer.impact == "broken"
            }
            if len(broken) != 3:
                failures.append(
                    f"signature-break: expected 3 broken consumers, traced {len(broken)}"
                )
    if name == "untested-endpoint":
        if dossier.coverage.available and dossier.coverage.uncovered_added_lines <= 0:
            failures.append("untested-endpoint: no uncovered added lines were reported")
        if dossier.coverage.available and not dossier.skeletons:
            failures.append("untested-endpoint: no test skeleton was generated")
    if name == "docs-drift":
        if len(dossier.doc_drift) < 2:
            failures.append(
                f"docs-drift: expected at least 2 drift items, found {len(dossier.doc_drift)}"
            )
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
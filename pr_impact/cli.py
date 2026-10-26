"""Command-line interface.

The local CLI is the source of truth: it analyses a git repository without any
GitHub installation and writes the dossier, rollback checklist, JSON payload and
generated test skeletons.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import gitio
from .emit.dossier import render_dossier
from .pipeline import AnalyzeOptions, analyze, has_breaking, write_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-impact",
        description="Evidence-linked impact dossier for a pull request.",
    )
    parser.add_argument("repo", nargs="?", default=".", help="path to a git repository")
    parser.add_argument("--base", default="main", help="base revision (default: main)")
    parser.add_argument("--head", default="HEAD", help="head revision (default: HEAD)")
    parser.add_argument(
        "--out",
        default=".pr-impact",
        help="artifact directory (default: .pr-impact)",
    )
    parser.add_argument("--depth", type=int, default=3, help="consumer trace depth")
    parser.add_argument("--no-coverage", action="store_true", help="skip coverage runs")
    parser.add_argument("--no-llm", action="store_true", help="disable LLM enrichment")
    parser.add_argument(
        "--print-comment",
        action="store_true",
        help="print the dossier markdown (the would-be PR comment) to stdout",
    )
    parser.add_argument(
        "--fail-on-breaking",
        action="store_true",
        help="exit non-zero when any breaking change is detected",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        dossier, _graphs = analyze(
            Path(args.repo),
            args.base,
            args.head,
            AnalyzeOptions(
                coverage=not args.no_coverage,
                llm=not args.no_llm,
                max_depth=args.depth,
            ),
        )
    except gitio.GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    paths = write_artifacts(dossier, args.out)
    if args.print_comment:
        print(render_dossier(dossier))
    else:
        print(
            f"{dossier.risk.band} {dossier.risk.total}/100 - "
            f"{len(dossier.breaking_changes)} breaking change(s), "
            f"{dossier.coverage.uncovered_added_lines} uncovered new line(s), "
            f"{len(dossier.doc_drift)} documentation drift item(s)"
        )
        print(f"dossier:  {paths['dossier']}")
        print(f"rollback: {paths['rollback']}")
        print(f"json:     {paths['json']}")
        for skeleton in dossier.skeletons:
            print(f"skeleton: {paths[skeleton.path]}")

    if args.fail_on_breaking and has_breaking(dossier):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
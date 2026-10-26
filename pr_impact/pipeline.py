"""End-to-end analysis pipeline.

Order matters: static facts first, then coverage, then docs, then the optional
LLM pass, and finally the risk score so every factor reflects the emitted
changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import gitio
from .cov import correlate, run_coverage
from .detectors.api import detect_changes
from .detectors.docs import detect_doc_drift
from .emit.dossier import dossier_to_dict, render_dossier
from .emit.rollback import generate_skeletons, rollback_steps
from .graph import RepoGraph, build_graph
from .llm import apply_reasoner, build_reasoner
from .model import (
    Classification,
    CoverageReport,
    Dossier,
    ModuleInfo,
    Resolution,
)
from .parsing import detect_language, parse_module
from .risk import compute_risk

MAX_SOURCE_FILES = 5000
MAX_DOC_BYTES = 250_000


@dataclass(frozen=True)
class AnalyzeOptions:
    coverage: bool = True
    llm: bool = True
    max_depth: int = 3


def analyze(
    repo_path: str | Path,
    base: str,
    head: str,
    options: AnalyzeOptions | None = None,
) -> tuple[Dossier, dict[str, RepoGraph]]:
    options = options or AnalyzeOptions()
    repo = Path(repo_path).resolve()
    if not repo.exists():
        raise gitio.GitError(f"repository path does not exist: {repo}")
    if not gitio.is_git_repo(repo):
        raise gitio.GitError(f"not a git repository: {repo}")

    base_sha = gitio.resolve_rev(repo, base)
    head_sha = gitio.resolve_rev(repo, head)
    limitations: list[str] = []

    base_paths = _source_files(repo, base, limitations)
    head_paths = _source_files(repo, head, limitations)
    base_modules, base_errors = _parse_revision(repo, base, base_paths)
    head_modules, head_errors = _parse_revision(repo, head, head_paths)
    for error in sorted({*base_errors, *head_errors}):
        limitations.append(error)

    base_graph = build_graph(base, base_modules)
    head_graph = build_graph(head, head_modules)
    _note_graph_gaps(base_graph, head_graph, limitations)

    diffs = gitio.diff_files(repo, base, head)
    supported_changed = [
        diff for diff in diffs if detect_language(diff.path) is not None
    ]
    changes = detect_changes(
        base_modules=base_modules,
        head_modules=head_modules,
        base_graph=base_graph,
        head_graph=head_graph,
        diffs=supported_changed,
        base_rev=base,
        head_rev=head,
        max_depth=options.max_depth,
    )

    base_routes = [route for module in base_modules.values() for route in module.routes]
    head_routes = [route for module in head_modules.values() for route in module.routes]
    docs = _read_docs(repo, head, gitio.list_files(repo, head))
    doc_drift = detect_doc_drift(
        head_modules=head_modules,
        changes=changes,
        docs=docs,
        base_routes=base_routes,
        head_routes=head_routes,
        base_rev=base,
        head_rev=head,
    )

    if options.llm:
        reasoner = build_reasoner()
        limitations.extend(apply_reasoner(changes, reasoner))
        if reasoner.name == "disabled":
            limitations.append(
                "LLM enrichment is disabled (set PR_IMPACT_LLM_URL to enable it); "
                "semantic classifications are rule-based."
            )
    else:
        limitations.append("LLM enrichment disabled by --no-llm; classifications are rule-based.")

    coverage = _coverage(repo, diffs, base, head, options.coverage)
    limitations.extend(coverage.limitations)

    risk = compute_risk(changes, coverage, doc_drift)
    skeletons = generate_skeletons(changes, coverage, head)
    steps = rollback_steps(changes, coverage, doc_drift, head)

    if any(diff.path.endswith((".ts", ".tsx", ".js", ".jsx")) for diff in supported_changed):
        limitations.append(
            "TypeScript/JavaScript support is best-effort: decorators, overloads "
            "and dynamic dispatch are not resolved, so consumers may be under-counted."
        )

    total_consumers = len(
        {
            (consumer.symbol, consumer.path)
            for change in changes
            if change.classification is Classification.BREAKING
            for consumer in change.consumers
            if consumer.impact == "broken"
        }
    )
    dossier = Dossier(
        repo=str(repo),
        base_rev=base,
        head_rev=head,
        base_sha=gitio.short_sha(repo, base_sha),
        head_sha=gitio.short_sha(repo, head_sha),
        changes=changes,
        coverage=coverage,
        doc_drift=doc_drift,
        risk=risk,
        skeletons=skeletons,
        rollback_steps=steps,
        limitations=_dedupe(limitations),
        files_changed=len(diffs),
        supported_files_changed=len(supported_changed),
        total_consumers=total_consumers,
    )
    return dossier, {"base": base_graph, "head": head_graph}


def write_artifacts(dossier: Dossier, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    dossier_md = out / "dossier.md"
    dossier_md.write_text(render_dossier(dossier), encoding="utf-8")
    paths["dossier"] = dossier_md

    rollback = out / "rollback.md"
    rollback.write_text(_render_rollback(dossier), encoding="utf-8")
    paths["rollback"] = rollback

    data = out / "dossier.json"
    data.write_text(
        json.dumps(dossier_to_dict(dossier), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    paths["json"] = data

    for skeleton in dossier.skeletons:
        relative = PurePosixPath(skeleton.path)
        if relative.parts and relative.parts[0] == ".pr-impact":
            relative = PurePosixPath(*relative.parts[1:])
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(skeleton.content, encoding="utf-8")
        paths[skeleton.path] = destination
    return paths


def _render_rollback(dossier: Dossier) -> str:
    lines = [
        "# Rollback checklist",
        "",
        f"Generated for `{dossier.head_rev}` ({dossier.head_sha}) against "
        f"`{dossier.base_rev}` ({dossier.base_sha}).",
        "",
        "Risk verdict: "
        f"**{dossier.risk.band} - {dossier.risk.total}/100**.",
        "",
    ]
    lines.extend(f"{index}. {step}" for index, step in enumerate(dossier.rollback_steps, 1))
    lines.extend(
        [
            "",
            "## Evidence",
            "",
        ]
    )
    for change in dossier.breaking_changes:
        lines.append(
            f"- `{change.symbol}` ({change.kind.value}) - "
            f"{change.evidence[0].loc() if change.evidence else 'no evidence'}"
        )
    if not dossier.breaking_changes:
        lines.append("- No breaking surface detected.")
    lines.append("")
    return "\n".join(lines)


def _source_files(repo: Path, revision: str, limitations: list[str]) -> list[str]:
    paths = [
        path
        for path in gitio.list_files(repo, revision)
        if detect_language(path) is not None
    ]
    if len(paths) > MAX_SOURCE_FILES:
        limitations.append(
            f"repository snapshot at {revision} has {len(paths)} supported files; "
            f"only the first {MAX_SOURCE_FILES} were parsed"
        )
        paths = paths[:MAX_SOURCE_FILES]
    return paths


def _parse_revision(
    repo: Path, revision: str, paths: list[str]
) -> tuple[dict[str, ModuleInfo], list[str]]:
    modules: dict[str, ModuleInfo] = {}
    errors: list[str] = []
    for path in paths:
        source = gitio.read_file(repo, revision, path)
        if source is None:
            errors.append(f"could not read {path} at {revision}")
            continue
        try:
            module = parse_module(path, source)
        except Exception as exc:  # defensive: one bad file must not abort the run
            errors.append(f"parser crashed on {path} at {revision}: {exc}")
            continue
        modules[path] = module
    parse_failures = [module.path for module in modules.values() if module.parse_error]
    for path in parse_failures[:5]:
        errors.append(
            f"{path} contains syntax the parser only partially understood; "
            "its facts are incomplete"
        )
    if len(parse_failures) > 5:
        errors.append(f"{len(parse_failures) - 5} more files had partial parse results")
    return modules, errors


def _read_docs(repo: Path, revision: str, paths: list[str]) -> dict[str, str]:
    docs: dict[str, str] = {}
    for path in paths:
        if not path.lower().endswith((".md", ".mdx")):
            continue
        if any(part in path.split("/") for part in ("node_modules", "vendor", ".venv")):
            continue
        raw = gitio.read_file(repo, revision, path)
        if raw is None or len(raw) > MAX_DOC_BYTES:
            continue
        docs[path] = raw.decode("utf-8", "replace")
    return dict(sorted(docs.items()))


def _coverage(
    repo: Path, diffs, base: str, head: str, enabled: bool
) -> CoverageReport:
    if not enabled:
        return CoverageReport(
            available=False,
            base_rev=base,
            head_rev=head,
            limitations=["coverage disabled with --no-coverage; no gap is claimed"],
        )
    head_outcome = run_coverage(repo, head)
    base_outcome = run_coverage(repo, base)
    return correlate(
        diffs=diffs,
        head=head_outcome,
        base=base_outcome,
        base_label=base,
        head_label=head,
    )


def _note_graph_gaps(
    base_graph: RepoGraph, head_graph: RepoGraph, limitations: list[str]
) -> None:
    for label, graph in (("base", base_graph), ("head", head_graph)):
        ambiguous = [
            edge for edge in graph.edges if edge.resolution is Resolution.AMBIGUOUS
        ]
        unresolved = [
            edge for edge in graph.edges if edge.resolution is Resolution.UNRESOLVED
        ]
        if ambiguous:
            sample = ", ".join(
                f"{edge.path}:{edge.line}" for edge in ambiguous[:3]
            )
            limitations.append(
                f"{len(ambiguous)} call edge(s) at {label} are ambiguous "
                f"(examples: {sample}); those consumers are not counted"
            )
        if unresolved:
            sample = ", ".join(
                f"{edge.path}:{edge.line}" for edge in unresolved[:3]
            )
            limitations.append(
                f"{len(unresolved)} call edge(s) at {label} are unresolved "
                f"(examples: {sample})"
            )


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def has_breaking(dossier: Dossier) -> bool:
    return any(
        change.classification is Classification.BREAKING
        for change in dossier.changes
    )
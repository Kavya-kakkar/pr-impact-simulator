from __future__ import annotations

from pathlib import Path

import pytest

from pr_impact import gitio
from pr_impact.model import ChangeKind, Classification
from pr_impact.pipeline import AnalyzeOptions, analyze


def test_signature_break_dossier(dossier_signature_break):
    dossier = dossier_signature_break
    assert len(dossier.breaking_changes) == 1
    change = dossier.breaking_changes[0]
    assert change.kind is ChangeKind.SIGNATURE_CHANGED
    assert change.classification is Classification.BREAKING
    broken = [consumer for consumer in change.consumers if consumer.impact == "broken"]
    assert len(broken) == 3
    assert {consumer.symbol for consumer in broken} == {
        "sample_app/admin.py::seed_admin",
        "sample_app/clients.py::create_owner",
        "sample_app/clients.py::invite_member",
    }
    assert dossier.total_consumers == 3
    assert dossier.risk.total == 29
    assert dossier.coverage.available is False
    assert any("coverage disabled" in item for item in dossier.limitations)
    assert dossier.files_changed == 1
    assert dossier.supported_files_changed == 1
    assert dossier.base_sha and dossier.head_sha


def test_docs_drift_dossier(dossier_docs_drift):
    dossier = dossier_docs_drift
    assert len(dossier.breaking_changes) == 2
    kinds = {change.kind for change in dossier.breaking_changes}
    assert kinds == {ChangeKind.SIGNATURE_CHANGED, ChangeKind.ROUTE_CHANGED}
    assert len(dossier.doc_drift) == 2
    lines = sorted(drift.doc_line for drift in dossier.doc_drift)
    assert lines == [10, 16]
    assert all(drift.evidence for drift in dossier.doc_drift)
    assert dossier.risk.band == "HIGH"
    assert dossier.risk.total == 46
    assert dossier.skeletons == []


def test_untested_endpoint_dossier(dossier_untested):
    dossier = dossier_untested
    assert dossier.breaking_changes == []
    kinds = {change.kind for change in dossier.changes}
    assert ChangeKind.FUNCTION_ADDED in kinds
    assert ChangeKind.ROUTE_ADDED in kinds
    assert dossier.coverage.available is False
    assert dossier.skeletons == []
    assert dossier.risk.total == 0  # coverage disabled contributes no invented lines


def test_pipeline_with_real_coverage(fixture_repo):
    dossier, graphs = analyze(
        fixture_repo.repo,
        "main",
        "pr/untested-endpoint",
        AnalyzeOptions(coverage=True, llm=False),
    )
    assert dossier.coverage.available
    assert dossier.coverage.tests_passed is True
    assert dossier.coverage.uncovered_added_lines >= 5
    assert dossier.coverage.head_percent is not None
    assert dossier.coverage.head_percent < (dossier.coverage.base_percent or 0)
    changed = {file.path for file in dossier.coverage.files}
    assert changed == {"sample_app/api.py", "sample_app/services.py"}
    assert dossier.skeletons
    assert all(skeleton.path.startswith(".pr-impact/tests/") for skeleton in dossier.skeletons)
    assert "head" in graphs and "base" in graphs


def test_pipeline_without_coverage_says_so(fixture_repo):
    dossier, _ = analyze(
        fixture_repo.repo,
        "main",
        "pr/signature-break",
        AnalyzeOptions(coverage=False, llm=False),
    )
    assert not dossier.coverage.available
    assert dossier.coverage.uncovered_added_lines == 0
    assert any("no gap is claimed" in item for item in dossier.coverage.limitations)


def test_pipeline_rejects_missing_and_non_git_paths(tmp_path: Path):
    with pytest.raises(gitio.GitError):
        analyze(tmp_path / "missing", "main", "HEAD")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(gitio.GitError):
        analyze(plain, "main", "HEAD")


def test_pipeline_rejects_unknown_revision(fixture_repo):
    with pytest.raises(gitio.GitError):
        analyze(fixture_repo.repo, "main", "does-not-exist")


def test_analysis_is_deterministic(fixture_repo):
    first, _ = analyze(
        fixture_repo.repo, "main", "pr/signature-break", AnalyzeOptions(coverage=False, llm=False)
    )
    second, _ = analyze(
        fixture_repo.repo, "main", "pr/signature-break", AnalyzeOptions(coverage=False, llm=False)
    )
    assert first.risk == second.risk
    assert [change.symbol for change in first.changes] == [
        change.symbol for change in second.changes
    ]
    assert first.limitations == second.limitations
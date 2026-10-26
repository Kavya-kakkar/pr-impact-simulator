from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from build_fixture import build_fixture  # noqa: E402

from pr_impact.model import Change, ChangeKind, Classification, Confidence, Evidence  # noqa: E402
from pr_impact.pipeline import AnalyzeOptions, analyze  # noqa: E402


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory) -> object:
    destination = tmp_path_factory.mktemp("fixture") / "target_repo"
    return build_fixture(dest=destination, force=True)


@pytest.fixture(scope="session")
def dossier_signature_break(fixture_repo):
    dossier, _graphs = analyze(
        fixture_repo.repo,
        "main",
        "pr/signature-break",
        AnalyzeOptions(coverage=False, llm=False),
    )
    return dossier


@pytest.fixture(scope="session")
def dossier_docs_drift(fixture_repo):
    dossier, _graphs = analyze(
        fixture_repo.repo,
        "main",
        "pr/docs-drift",
        AnalyzeOptions(coverage=False, llm=False),
    )
    return dossier


@pytest.fixture(scope="session")
def dossier_untested(fixture_repo):
    dossier, _graphs = analyze(
        fixture_repo.repo,
        "main",
        "pr/untested-endpoint",
        AnalyzeOptions(coverage=False, llm=False),
    )
    return dossier


@pytest.fixture()
def make_change():
    def factory(
        *,
        kind: ChangeKind = ChangeKind.SIGNATURE_CHANGED,
        classification: Classification = Classification.BREAKING,
        symbol: str = "pkg/mod.py::f",
        file: str = "pkg/mod.py",
        evidence: list[Evidence] | None = None,
        **kwargs,
    ) -> Change:
        return Change(
            kind=kind,
            symbol=symbol,
            summary=kwargs.pop("summary", "summary"),
            classification=classification,
            confidence=kwargs.pop("confidence", Confidence.OBSERVED),
            evidence=evidence or [Evidence("source", file, "head", 1, 1)],
            file=file,
            **kwargs,
        )

    return factory
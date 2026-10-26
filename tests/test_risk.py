from __future__ import annotations

from pr_impact.model import (
    ChangeKind,
    Classification,
    ConsumerHit,
    CoverageFile,
    CoverageReport,
    DocDrift,
    Evidence,
    Resolution,
)
from pr_impact.risk import FACTOR_SPECS, compute_risk


def _coverage(uncovered: int = 0) -> CoverageReport:
    files = []
    if uncovered:
        files.append(
            CoverageFile(
                path="pkg/mod.py",
                added_lines=tuple(range(1, uncovered + 1)),
                uncovered_added=tuple(range(1, uncovered + 1)),
                covered_added=0,
            )
        )
    return CoverageReport(available=True, base_rev="base", head_rev="head", files=files)


def _drift() -> DocDrift:
    return DocDrift(
        doc_path="README.md",
        doc_line=1,
        snippet="f()",
        reason="r",
        evidence=[Evidence("docs", "README.md", "head", 1, 1)],
    )


def test_zero_risk_when_nothing_changed():
    risk = compute_risk([], _coverage(), [])
    assert risk.total == 0
    assert risk.band == "LOW"
    assert len(risk.factors) == len(FACTOR_SPECS)
    assert all(factor.contribution == 0 for factor in risk.factors)


def test_each_factor_is_visible_in_the_formula(make_change):
    consumers = [
        ConsumerHit(
            symbol=f"pkg/clients.py::c{index}",
            path="pkg/clients.py",
            line=index,
            depth=1,
            resolution=Resolution.RESOLVED,
            revision="head",
            impact="broken",
        )
        for index in (1, 2)
    ]
    change = make_change(
        kind=ChangeKind.SIGNATURE_CHANGED,
        consumers=consumers,
    )
    risk = compute_risk([change], _coverage(5), [_drift()])
    contributions = {factor.name: factor.contribution for factor in risk.factors}
    assert contributions == {
        "breaking_changes": 10,
        "affected_consumers": 6,
        "uncovered_new_lines": 5,
        "docs_drift": 3,
        "migration_complexity": 10,
    }
    assert risk.total == 34
    assert risk.band == "MODERATE"
    assert {factor.name for factor in risk.factors} == set(contributions)
    for contribution in contributions.values():
        assert f"={contribution:g}" in risk.formula.replace(" ", "")


def test_caps_are_applied_and_documented(make_change):
    changes = [
        make_change(symbol=f"pkg/mod.py::f{index}", classification=Classification.BREAKING)
        for index in range(10)
    ]
    risk = compute_risk(changes, _coverage(100), [_drift(), _drift()])
    factors = {factor.name: factor for factor in risk.factors}
    assert factors["breaking_changes"].capped == 5
    assert factors["uncovered_new_lines"].capped == 40
    assert "capped at" in factors["breaking_changes"].explanation
    assert risk.total == 100
    assert risk.band == "CRITICAL"
    assert min(100, risk.raw_total) == 100


def test_only_broken_consumers_count(make_change):
    consumers = [
        ConsumerHit(
            symbol="pkg/clients.py::compatible",
            path="pkg/clients.py",
            line=1,
            depth=1,
            resolution=Resolution.RESOLVED,
            revision="head",
            impact="compatible",
        ),
        ConsumerHit(
            symbol="pkg/clients.py::broken",
            path="pkg/clients.py",
            line=2,
            depth=1,
            resolution=Resolution.RESOLVED,
            revision="head",
            impact="broken",
        ),
    ]
    risk = compute_risk([make_change(consumers=consumers)], _coverage(), [])
    factor = next(item for item in risk.factors if item.name == "affected_consumers")
    assert factor.raw == 1


def test_risk_formula_is_manual_math(make_change):
    change = make_change(kind=ChangeKind.ROUTE_REMOVED, file="pkg/api.py")
    risk = compute_risk([change], _coverage(3), [])
    # 10*1 (breaking) + 5*3 (route removal migration) + 3 (uncovered lines)
    assert risk.raw_total == 28
    assert risk.total == 28
"""Transparent, factor-based risk scoring.

The score is a pure function of observed counts. Every factor is reported with
its raw value, cap, weight and contribution so a reviewer can reproduce the
number by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    Change,
    ChangeKind,
    Classification,
    CoverageReport,
    DocDrift,
    RiskFactor,
    RiskScore,
)


@dataclass(frozen=True)
class FactorSpec:
    name: str
    label: str
    weight: float
    cap: int
    explanation: str


FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        "breaking_changes",
        "Breaking changes",
        10.0,
        5,
        "change(s) classified breaking by the static rules",
    ),
    FactorSpec(
        "affected_consumers",
        "Affected consumers",
        3.0,
        20,
        "distinct function(s) whose call sites are incompatible with head",
    ),
    FactorSpec(
        "uncovered_new_lines",
        "Uncovered new lines",
        1.0,
        40,
        "Python line(s) added by the PR that no test executed",
    ),
    FactorSpec(
        "docs_drift",
        "Documentation drift",
        3.0,
        5,
        "documented example(s) or endpoint(s) contradicted by head",
    ),
    FactorSpec(
        "migration_complexity",
        "Migration complexity",
        5.0,
        5,
        "severity-weighted migration point(s) from breaking change kinds",
    ),
)

MIGRATION_WEIGHTS: dict[ChangeKind, int] = {
    ChangeKind.FUNCTION_REMOVED: 3,
    ChangeKind.ROUTE_REMOVED: 3,
    ChangeKind.ROUTE_CHANGED: 2,
    ChangeKind.SIGNATURE_CHANGED: 2,
    ChangeKind.STATUS_CHANGED: 1,
    ChangeKind.FUNCTION_ADDED: 0,
    ChangeKind.ROUTE_ADDED: 0,
}

BANDS = ((20, "LOW"), (40, "MODERATE"), (65, "HIGH"))


def compute_risk(
    changes: list[Change], coverage: CoverageReport, doc_drift: list[DocDrift]
) -> RiskScore:
    breaking = [c for c in changes if c.classification is Classification.BREAKING]
    affected = {
        (consumer.symbol, consumer.path)
        for change in breaking
        for consumer in change.consumers
        if consumer.impact == "broken"
    }
    migration = sum(MIGRATION_WEIGHTS.get(change.kind, 1) for change in breaking)

    raws = {
        "breaking_changes": len(breaking),
        "affected_consumers": len(affected),
        "uncovered_new_lines": coverage.uncovered_added_lines,
        "docs_drift": len(doc_drift),
        "migration_complexity": migration,
    }

    factors: list[RiskFactor] = []
    total = 0.0
    for spec in FACTOR_SPECS:
        raw = raws[spec.name]
        contribution = round(min(raw, spec.cap) * spec.weight, 2)
        total += contribution
        factors.append(
            RiskFactor(
                name=spec.name,
                raw=raw,
                cap=spec.cap,
                weight=spec.weight,
                contribution=contribution,
                explanation=(
                    f"{raw} {spec.explanation}"
                    + (f" (capped at {spec.cap})" if raw > spec.cap else "")
                ),
            )
        )

    score = min(100, int(round(total)))
    return RiskScore(
        total=score,
        raw_total=round(total, 2),
        band=_band(score),
        factors=factors,
        formula=_formula(factors, total, score),
    )


def _band(score: int) -> str:
    for threshold, label in BANDS:
        if score < threshold:
            return label
    return "CRITICAL"


def _formula(factors: list[RiskFactor], raw_total: float, score: int) -> str:
    parts = [
        f"{factor.weight:g}xmin({factor.raw},{factor.cap})={factor.contribution:g}"
        for factor in factors
    ]
    return (
        f"risk = min(100, {' + '.join(parts)}) "
        f"= min(100, {raw_total:g}) = {score}"
    )
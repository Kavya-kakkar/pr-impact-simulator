"""Optional LLM enrichment.

The static layer owns every fact. An LLM may only (a) escalate a change to
NEEDS_HUMAN when it explains remaining uncertainty, or (b) add prose that agrees
with the rule-based classification. It can never soften a breaking verdict and
its text is never used as a source of paths, line numbers or call relationships.

Enrichment is off unless ``PR_IMPACT_LLM_URL`` is set; the deterministic demo
runs without it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .model import Change, Classification


@dataclass(frozen=True)
class SemanticVerdict:
    classification: Classification
    rationale: str
    uncertainty: str = ""


class Reasoner(Protocol):
    name: str

    def review(self, change: Change, facts: dict[str, object]) -> SemanticVerdict | None:
        ...


class NullReasoner:
    name = "disabled"

    def review(self, change: Change, facts: dict[str, object]) -> SemanticVerdict | None:
        return None


class HttpReasoner:
    """Minimal OpenAI-compatible chat-completions client (stdlib only)."""

    name = "http"

    def __init__(self, url: str, model: str, api_key: str | None, timeout: int = 30):
        self.url = url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def review(self, change: Change, facts: dict[str, object]) -> SemanticVerdict | None:
        system = (
            "You classify API changes for an impact report. Use only the supplied "
            "facts. Never invent file paths, line numbers, call relationships or "
            "test results. Reply with strict JSON: "
            '{"classification": "breaking|additive|needs-human", '
            '"rationale": "<= 40 words", "uncertainty": "<= 25 words"}.'
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"rule_classification": change.classification.value, **facts},
                        sort_keys=True,
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **(
                    {"Authorization": f"Bearer {self.api_key}"}
                    if self.api_key
                    else {}
                ),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            verdict = json.loads(content)
            classification = Classification(str(verdict["classification"]).strip().lower())
            return SemanticVerdict(
                classification=classification,
                rationale=str(verdict.get("rationale", ""))[:400],
                uncertainty=str(verdict.get("uncertainty", ""))[:200],
            )
        except (KeyError, IndexError, TypeError, ValueError, urllib.error.URLError):
            return None


def build_reasoner() -> Reasoner:
    url = os.environ.get("PR_IMPACT_LLM_URL")
    if not url:
        return NullReasoner()
    return HttpReasoner(
        url=url,
        model=os.environ.get("PR_IMPACT_LLM_MODEL", "gpt-4o-mini"),
        api_key=os.environ.get("PR_IMPACT_LLM_KEY"),
    )


def change_facts(change: Change) -> dict[str, object]:
    """Only facts already established by the static layer."""

    return {
        "kind": change.kind.value,
        "symbol": change.symbol,
        "base_signature": (
            change.base_signature.render(change.symbol.split("::", 1)[-1])
            if change.base_signature
            else None
        ),
        "head_signature": (
            change.head_signature.render(change.symbol.split("::", 1)[-1])
            if change.head_signature
            else None
        ),
        "route_before": change.route_before.route_id if change.route_before else None,
        "route_after": change.route_after.route_id if change.route_after else None,
        "status_before": change.route_before.status_code if change.route_before else None,
        "status_after": change.route_after.status_code if change.route_after else None,
        "consumers_total": change.downstream_count,
        "consumers_broken": sum(
            1 for consumer in change.consumers if consumer.impact == "broken"
        ),
        "reasons": change.reasons,
    }


def apply_reasoner(changes: list[Change], reasoner: Reasoner) -> list[str]:
    """Apply verdicts under the escalation-only rule; return limitations."""

    limitations: list[str] = []
    for change in changes:
        if reasoner.name == "disabled":
            continue
        verdict = reasoner.review(change, change_facts(change))
        if verdict is None:
            limitations.append(
                f"LLM enrichment returned no usable verdict for {change.symbol}; "
                "rule-based classification retained"
            )
            continue
        if verdict.classification is Classification.NEEDS_HUMAN:
            if change.classification is not Classification.NEEDS_HUMAN:
                change.classification = Classification.NEEDS_HUMAN
                change.rationale = (
                    f"Escalated by LLM: {verdict.rationale} "
                    f"Uncertain: {verdict.uncertainty}"
                ).strip()
            continue
        if verdict.classification == change.classification:
            change.rationale = verdict.rationale
            continue
        limitations.append(
            f"ignored LLM verdict {verdict.classification.value!r} for "
            f"{change.symbol}: it contradicted the rule-based "
            f"{change.classification.value!r} classification"
        )
    return limitations
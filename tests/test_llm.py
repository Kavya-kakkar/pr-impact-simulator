from __future__ import annotations

import json

from pr_impact.llm import (
    HttpReasoner,
    NullReasoner,
    SemanticVerdict,
    apply_reasoner,
    build_reasoner,
    change_facts,
)
from pr_impact.model import Classification


class FakeReasoner:
    name = "fake"

    def __init__(self, verdict: SemanticVerdict | None):
        self.verdict = verdict
        self.calls = 0

    def review(self, change, facts):
        self.calls += 1
        return self.verdict


def test_null_reasoner_is_default_without_env(monkeypatch):
    monkeypatch.delenv("PR_IMPACT_LLM_URL", raising=False)
    assert isinstance(build_reasoner(), NullReasoner)


def test_build_reasoner_uses_env(monkeypatch):
    monkeypatch.setenv("PR_IMPACT_LLM_URL", "https://example.invalid/v1/chat/completions")
    monkeypatch.setenv("PR_IMPACT_LLM_MODEL", "test-model")
    reasoner = build_reasoner()
    assert isinstance(reasoner, HttpReasoner)
    assert reasoner.model == "test-model"


def test_llm_can_escalate_but_not_soften(make_change):
    change = make_change(classification=Classification.BREAKING)
    reasoner = FakeReasoner(
        SemanticVerdict(
            classification=Classification.NEEDS_HUMAN,
            rationale="Contract depends on external client behaviour.",
            uncertainty="Clients may tolerate the added argument.",
        )
    )
    limitations = apply_reasoner([change], reasoner)
    assert change.classification is Classification.NEEDS_HUMAN
    assert "Escalated by LLM" in change.rationale
    assert limitations == []

    softening = make_change(classification=Classification.BREAKING)
    reasoner = FakeReasoner(
        SemanticVerdict(classification=Classification.ADDITIVE, rationale="looks fine")
    )
    limitations = apply_reasoner([softening], reasoner)
    assert softening.classification is Classification.BREAKING
    assert any("ignored LLM verdict" in item for item in limitations)


def test_llm_agreement_adds_rationale(make_change):
    change = make_change(classification=Classification.BREAKING)
    reasoner = FakeReasoner(
        SemanticVerdict(classification=Classification.BREAKING, rationale="Callers break.")
    )
    limitations = apply_reasoner([change], reasoner)
    assert change.rationale == "Callers break."
    assert limitations == []


def test_missing_verdict_is_a_limitation(make_change):
    change = make_change()
    limitations = apply_reasoner([change], FakeReasoner(None))
    assert any("no usable verdict" in item for item in limitations)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_http_reasoner_parses_strict_json(make_change, monkeypatch):
    import urllib.request

    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "classification": "needs-human",
                            "rationale": "Needs a human.",
                            "uncertainty": "Unknown clients.",
                        }
                    )
                }
            }
        ]
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))
    reasoner = HttpReasoner("https://example.invalid", "m", None)
    verdict = reasoner.review(make_change(), {})
    assert verdict is not None and verdict.classification is Classification.NEEDS_HUMAN


def test_http_reasoner_failure_returns_none(make_change, monkeypatch):
    import urllib.error
    import urllib.request

    def explode(*args, **kwargs):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    reasoner = HttpReasoner("https://example.invalid", "m", None)
    assert reasoner.review(make_change(), {}) is None


def test_change_facts_are_static(make_change):
    change = make_change()
    facts = change_facts(change)
    assert facts["kind"] == "signature-changed"
    assert facts["symbol"] == "pkg/mod.py::f"
    assert facts["consumers_broken"] == 0
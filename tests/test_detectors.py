from __future__ import annotations

import pytest

from pr_impact.detectors.api import call_mismatch, compare_signatures, detect_changes
from pr_impact.detectors.docs import detect_doc_drift
from pr_impact.graph import build_graph
from pr_impact.model import (
    Change,
    ChangeKind,
    Classification,
    Confidence,
    DocDrift,
    Evidence,
    FileDiff,
    Param,
    RouteInfo,
    Signature,
)
from pr_impact.parsing import parse_module


def _sig(*params: Param, returns: str | None = None, is_async: bool = False) -> Signature:
    return Signature(params=tuple(params), returns=returns, is_async=is_async)


def test_added_required_parameter_is_breaking():
    base = _sig(Param("name", "positional"))
    head = _sig(Param("name", "positional"), Param("tenant", "positional"))
    verdict = compare_signatures(base, head)
    assert verdict.breaking
    assert not verdict.additive


def test_added_optional_parameter_is_additive():
    base = _sig(Param("name", "positional"))
    head = _sig(Param("name", "positional"), Param("flag", "keyword_only", default="False"))
    verdict = compare_signatures(base, head)
    assert not verdict.breaking
    assert verdict.additive


def test_renamed_parameter_is_breaking():
    verdict = compare_signatures(
        _sig(Param("amount", "positional")),
        _sig(Param("amount_cents", "positional")),
    )
    assert any("removed or renamed" in reason for reason in verdict.breaking)


def test_default_and_return_changes_need_human():
    verdict = compare_signatures(
        _sig(Param("percent", "positional", default="10"), returns="int"),
        _sig(Param("percent", "positional", default="15"), returns="float"),
    )
    assert not verdict.breaking
    assert len(verdict.needs_human) == 2


def test_call_mismatch_handles_keywords_and_variadics():
    signature = _sig(
        Param("amount_cents", "positional"),
        Param("percent", "positional"),
    )

    class Call:
        arg_count = 0
        kwargs = ("amount", "percent")
        has_star_args = False

    incompatible, reason = call_mismatch(signature, Call())
    assert incompatible
    assert "amount_cents" in reason and "not accepted" in reason

    class GoodCall:
        arg_count = 2
        kwargs = ()
        has_star_args = False

    assert not call_mismatch(signature, GoodCall())[0]

    class StarCall:
        arg_count = None
        kwargs = ()
        has_star_args = True

    incompatible, reason = call_mismatch(signature, StarCall())
    assert not incompatible
    assert "unpacking" in reason


def test_call_mismatch_rejects_too_many_positionals():
    signature = _sig(Param("a", "positional"))

    class Call:
        arg_count = 2
        kwargs = ()
        has_star_args = False

    incompatible, reason = call_mismatch(signature, Call())
    assert incompatible
    assert "at most 1" in reason


def test_evidence_is_mandatory():
    with pytest.raises(ValueError):
        Change(
            kind=ChangeKind.SIGNATURE_CHANGED,
            symbol="a.py::f",
            summary="s",
            classification=Classification.BREAKING,
            confidence=Confidence.OBSERVED,
            evidence=[],
            file="a.py",
        )
    with pytest.raises(ValueError):
        DocDrift(doc_path="README.md", doc_line=1, snippet="f()", reason="r", evidence=[])


def _modules(base_source: str, head_source: str):
    base = {"pkg/api.py": parse_module("pkg/api.py", base_source.encode())}
    head = {"pkg/api.py": parse_module("pkg/api.py", head_source.encode())}
    return base, head


def _empty_diff() -> FileDiff:
    return FileDiff(path="pkg/api.py", old_path=None, status="modified", hunks=())


def test_route_removal_is_breaking():
    base_source = (
        'from .web import router\n\n@router.get("/users")\ndef list_users():\n    return []\n'
    )
    head_source = 'from .web import router\n\ndef list_users():\n    return []\n'
    base, head = _modules(base_source, head_source)
    changes = detect_changes(
        base_modules=base,
        head_modules=head,
        base_graph=build_graph("base", base),
        head_graph=build_graph("head", head),
        diffs=[_empty_diff()],
        base_rev="base",
        head_rev="head",
    )
    route_changes = [change for change in changes if change.kind is ChangeKind.ROUTE_REMOVED]
    assert len(route_changes) == 1
    assert route_changes[0].classification is Classification.BREAKING
    assert route_changes[0].route_before.route_id == "GET /users"


def test_status_code_change_needs_human():
    base_source = (
        'from .web import router\n\n@router.get("/users", status_code=200)\n'
        "def list_users():\n    return []\n"
    )
    head_source = (
        'from .web import router\n\n@router.get("/users", status_code=202)\n'
        "def list_users():\n    return []\n"
    )
    base, head = _modules(base_source, head_source)
    changes = detect_changes(
        base_modules=base,
        head_modules=head,
        base_graph=build_graph("base", base),
        head_graph=build_graph("head", head),
        diffs=[_empty_diff()],
        base_rev="base",
        head_rev="head",
    )
    status = [change for change in changes if change.kind is ChangeKind.STATUS_CHANGED]
    assert len(status) == 1
    assert status[0].classification is Classification.NEEDS_HUMAN


def test_detect_changes_traces_broken_consumers():
    base_source = "def create_user(name, email):\n    return name\n"
    head_source = "def create_user(name, email, tenant):\n    return name\n"
    consumer = (
        "from .services import create_user\n\n"
        "def owner(name, email):\n    return create_user(name, email)\n"
    )
    base = {
        "pkg/services.py": parse_module("pkg/services.py", base_source.encode()),
        "pkg/clients.py": parse_module("pkg/clients.py", consumer.encode()),
    }
    head = {
        "pkg/services.py": parse_module("pkg/services.py", head_source.encode()),
        "pkg/clients.py": parse_module("pkg/clients.py", consumer.encode()),
    }
    diffs = [
        FileDiff(
            path="pkg/services.py",
            old_path=None,
            status="modified",
            hunks=(),
        )
    ]
    changes = detect_changes(
        base_modules=base,
        head_modules=head,
        base_graph=build_graph("base", base),
        head_graph=build_graph("head", head),
        diffs=diffs,
        base_rev="base",
        head_rev="head",
    )
    signature_change = next(
        change for change in changes if change.kind is ChangeKind.SIGNATURE_CHANGED
    )
    assert signature_change.classification is Classification.BREAKING
    assert [consumer.impact for consumer in signature_change.consumers] == ["broken"]
    assert signature_change.consumers[0].line == 4


def test_doc_code_block_drift():
    head = {
        "pkg/services.py": parse_module(
            "pkg/services.py",
            (
                "def calculate_discount(amount_cents: int, percent: int) -> int:\n"
                "    return amount_cents\n"
            ).encode(),
        )
    }
    change = Change(
        kind=ChangeKind.SIGNATURE_CHANGED,
        symbol="pkg/services.py::calculate_discount",
        summary="s",
        classification=Classification.BREAKING,
        confidence=Confidence.OBSERVED,
        evidence=[Evidence("source", "pkg/services.py", "head", 1, 2)],
        file="pkg/services.py",
    )
    docs = {
        "README.md": (
            "# Usage\n\n```python\nfrom pkg.services import calculate_discount\n"
            "total = calculate_discount(amount=2000, percent=15)\n```\n"
        )
    }
    drifts = detect_doc_drift(
        head_modules=head,
        changes=[change],
        docs=docs,
        base_routes=[],
        head_routes=[],
        base_rev="base",
        head_rev="head",
    )
    assert len(drifts) == 1
    assert drifts[0].doc_line == 5
    assert "amount_cents" in drifts[0].reason
    assert {item.kind for item in drifts[0].evidence} == {"docs", "source"}


def test_doc_endpoint_drift():
    base_route = RouteInfo(
        method="GET",
        path="/discounts/{code}",
        handler="pkg/api.py::get_discount",
        status_code=200,
        line=3,
        file="pkg/api.py",
    )
    docs = {"README.md": "# API\n\n- `GET /discounts/{code}` looks up a discount.\n"}
    drifts = detect_doc_drift(
        head_modules={},
        changes=[],
        docs=docs,
        base_routes=[base_route],
        head_routes=[],
        base_rev="base",
        head_rev="head",
    )
    assert len(drifts) == 1
    assert "gone at head" in drifts[0].reason
    assert drifts[0].doc_line == 3
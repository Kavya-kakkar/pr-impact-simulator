"""Documentation drift detection.

Only contradictions that can be demonstrated are reported: a README snippet
that no longer matches the head signature, or an API reference for a route that
no longer exists at head. Prose that merely looks stale is ignored.
"""

from __future__ import annotations

import re

from ..model import (
    Change,
    ChangeKind,
    DocDrift,
    Evidence,
    FunctionInfo,
    ModuleInfo,
    RouteInfo,
)
from ..parsing import calls_in_source, markdown_code_blocks
from .api import call_mismatch

_CODE_LANGUAGES = {"python": "python", "py": "python", "python3": "python"}
_ENDPOINT_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[A-Za-z0-9_{}./:-]*)",
    re.IGNORECASE,
)


def detect_doc_drift(
    *,
    head_modules: dict[str, ModuleInfo],
    changes: list[Change],
    docs: dict[str, str],
    base_routes: list[RouteInfo],
    head_routes: list[RouteInfo],
    base_rev: str,
    head_rev: str,
) -> list[DocDrift]:
    changed = {
        change.symbol.split("::", 1)[-1].rsplit(".", 1)[-1]: change
        for change in changes
        if change.kind in (ChangeKind.SIGNATURE_CHANGED, ChangeKind.FUNCTION_REMOVED)
    }
    head_functions = _head_functions_by_name(head_modules)

    drifts: list[DocDrift] = []
    for doc_path, text in docs.items():
        drifts.extend(
            _code_block_drift(
                doc_path=doc_path,
                text=text,
                changed=changed,
                head_functions=head_functions,
                head_rev=head_rev,
            )
        )
        drifts.extend(
            _endpoint_drift(
                doc_path=doc_path,
                text=text,
                base_routes=base_routes,
                head_routes=head_routes,
                base_rev=base_rev,
                head_rev=head_rev,
            )
        )
    return _dedupe(drifts)


def _code_block_drift(
    *,
    doc_path: str,
    text: str,
    changed: dict[str, Change],
    head_functions: dict[str, FunctionInfo],
    head_rev: str,
) -> list[DocDrift]:
    drifts: list[DocDrift] = []
    for language, first_line, code in markdown_code_blocks(text):
        grammar = _CODE_LANGUAGES.get(language)
        if grammar is None or not code.strip():
            continue
        try:
            calls = calls_in_source(code.encode("utf-8"), grammar)
        except Exception:
            continue
        for call in calls:
            if call.receiver is not None:
                continue
            change = changed.get(call.name)
            if change is None:
                continue
            doc_line = first_line + call.line - 1
            target = head_functions.get(call.name)
            doc_evidence = Evidence(
                "docs",
                doc_path,
                head_rev,
                doc_line,
                doc_line,
                detail=f"example calls `{call.callee}`",
            )
            if target is None:
                drifts.append(
                    DocDrift(
                        doc_path=doc_path,
                        doc_line=doc_line,
                        snippet=call.callee,
                        reason=(
                            f"README example calls `{call.name}`, which is removed "
                            "at head"
                        ),
                        evidence=[doc_evidence, *change.evidence],
                    )
                )
                continue
            incompatible, explanation = call_mismatch(target.signature, call)
            if not incompatible:
                continue
            drifts.append(
                DocDrift(
                    doc_path=doc_path,
                    doc_line=doc_line,
                    snippet=call.callee,
                    reason=(
                        f"README example does not match the head signature of "
                        f"`{call.name}`: {explanation}"
                    ),
                    evidence=[
                        doc_evidence,
                        Evidence(
                            "source",
                            target.path,
                            head_rev,
                            target.start_line,
                            target.end_line,
                            detail="head signature",
                        ),
                    ],
                )
            )
    return drifts


def _endpoint_drift(
    *,
    doc_path: str,
    text: str,
    base_routes: list[RouteInfo],
    head_routes: list[RouteInfo],
    base_rev: str,
    head_rev: str,
) -> list[DocDrift]:
    head_ids = {route.route_id for route in head_routes}
    base_by_id = {route.route_id: route for route in base_routes}
    fenced: set[int] = set()
    lines = text.splitlines()
    for _language, first_line, code in markdown_code_blocks(text):
        for offset in range(len(code.splitlines()) + 2):
            fenced.add(first_line + offset)

    drifts: list[DocDrift] = []
    for index, line in enumerate(lines, start=1):
        if index in fenced:
            continue
        for match in _ENDPOINT_RE.finditer(line):
            method = match.group(1).upper()
            path = match.group(2).rstrip(".,;:)`'\"")
            route_id = f"{method} {path}"
            if route_id in head_ids or route_id not in base_by_id:
                continue
            base_route = base_by_id[route_id]
            drifts.append(
                DocDrift(
                    doc_path=doc_path,
                    doc_line=index,
                    snippet=match.group(0).strip(),
                    reason=(
                        f"documented endpoint {route_id} existed at base but is "
                        "gone at head"
                    ),
                    evidence=[
                        Evidence(
                            "docs",
                            doc_path,
                            head_rev,
                            index,
                            index,
                            detail="documented endpoint",
                        ),
                        Evidence(
                            "route",
                            base_route.file,
                            base_rev,
                            base_route.line,
                            base_route.line,
                            detail="route removed by the PR",
                        ),
                    ],
                )
            )
    return drifts


def _head_functions_by_name(modules: dict[str, ModuleInfo]) -> dict[str, FunctionInfo]:
    """Map bare function name -> head definition (only unambiguous names)."""

    found: dict[str, FunctionInfo] = {}
    ambiguous: set[str] = set()
    for module in modules.values():
        for function in module.functions.values():
            if not function.is_public:
                continue
            if function.name in found and found[function.name].symbol != function.symbol:
                ambiguous.add(function.name)
            found[function.name] = function
    return {name: fn for name, fn in found.items() if name not in ambiguous}


def _dedupe(drifts: list[DocDrift]) -> list[DocDrift]:
    seen: set[tuple[str, int, str]] = set()
    unique: list[DocDrift] = []
    for drift in drifts:
        key = (drift.doc_path, drift.doc_line, drift.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(drift)
    return sorted(unique, key=lambda item: (item.doc_path, item.doc_line))
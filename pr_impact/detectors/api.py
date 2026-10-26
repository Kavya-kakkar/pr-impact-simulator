"""API-surface detectors.

Compares public function signatures and declared HTTP routes between the base
and head revisions. Every emitted change points at the base lines, the head
lines and the diff that caused it.
"""

from __future__ import annotations

from dataclasses import replace

from ..model import (
    Change,
    ChangeKind,
    Classification,
    Confidence,
    ConsumerHit,
    Evidence,
    FileDiff,
    FunctionInfo,
    ModuleInfo,
    RouteInfo,
    Signature,
)
from ..graph import RepoGraph

_POSITIONAL_KINDS = ("positional", "posonly")


def detect_changes(
    *,
    base_modules: dict[str, ModuleInfo],
    head_modules: dict[str, ModuleInfo],
    base_graph: RepoGraph,
    head_graph: RepoGraph,
    diffs: list[FileDiff],
    base_rev: str,
    head_rev: str,
    max_depth: int = 3,
) -> list[Change]:
    diff_by_head = {diff.path: diff for diff in diffs}
    changes: list[Change] = []

    for head_path, file_diff in diff_by_head.items():
        base_path = file_diff.old_path if file_diff.status == "renamed" else head_path
        base_mod = base_modules.get(base_path) if base_path else None
        head_mod = head_modules.get(head_path)
        if base_mod is None and head_mod is None:
            continue
        if (base_mod or head_mod).language not in ("python", "javascript", "typescript", "tsx"):
            continue

        base_funcs = _public(base_mod.functions) if base_mod else {}
        head_funcs = _public(head_mod.functions) if head_mod else {}

        for qualname, head_func in head_funcs.items():
            base_func = base_funcs.get(qualname)
            if base_func is None:
                changes.append(
                    Change(
                        kind=ChangeKind.FUNCTION_ADDED,
                        symbol=head_func.symbol,
                        summary=f"New public symbol `{qualname}` in `{head_path}`",
                        classification=Classification.ADDITIVE,
                        confidence=Confidence.OBSERVED,
                        evidence=_source_evidence(
                            head_func, head_rev, file_diff, head_path
                        ),
                        file=head_path,
                        head_signature=head_func.signature,
                        reasons=["new public API surface"],
                    )
                )
                continue
            verdict = compare_signatures(base_func.signature, head_func.signature)
            if not verdict.has_difference:
                continue
            classification = (
                Classification.BREAKING
                if verdict.breaking
                else (
                    Classification.NEEDS_HUMAN
                    if verdict.needs_human
                    else Classification.ADDITIVE
                )
            )
            consumers = _annotate_consumers(
                head_graph.consumers(head_func.symbol, max_depth=max_depth),
                head_graph,
                head_func.signature,
                target_name=head_func.name,
            )
            changes.append(
                Change(
                    kind=ChangeKind.SIGNATURE_CHANGED,
                    symbol=head_func.symbol,
                    summary=(
                        f"Signature of `{qualname}` changed: "
                        f"{base_func.signature.render(qualname)} -> "
                        f"{head_func.signature.render(qualname)}"
                    ),
                    classification=classification,
                    confidence=(
                        Confidence.NEEDS_HUMAN
                        if classification is Classification.NEEDS_HUMAN
                        else Confidence.OBSERVED
                    ),
                    evidence=_pair_evidence(
                        base_func, base_rev, head_func, head_rev, file_diff, head_path
                    ),
                    file=head_path,
                    base_signature=base_func.signature,
                    head_signature=head_func.signature,
                    consumers=consumers,
                    reasons=[
                        *verdict.breaking,
                        *verdict.needs_human,
                        *verdict.additive,
                    ],
                )
            )

        for qualname, base_func in base_funcs.items():
            if qualname in head_funcs:
                continue
            consumers = _annotate_consumers(
                base_graph.consumers(base_func.symbol, max_depth=max_depth),
                base_graph,
                base_func.signature,
                target_name=base_func.name,
                removed=True,
            )
            changes.append(
                Change(
                    kind=ChangeKind.FUNCTION_REMOVED,
                    symbol=base_func.symbol,
                    summary=f"Public symbol `{qualname}` removed from `{head_path}`",
                    classification=Classification.BREAKING,
                    confidence=Confidence.OBSERVED,
                    evidence=_removed_evidence(
                        base_func, base_rev, file_diff, head_path, head_rev
                    ),
                    file=head_path,
                    base_signature=base_func.signature,
                    consumers=consumers,
                    reasons=["symbol no longer exists at head"],
                )
            )

        changes.extend(
            _route_changes(
                base_mod=base_mod,
                head_mod=head_mod,
                file_diff=file_diff,
                base_rev=base_rev,
                head_rev=head_rev,
                head_path=head_path,
            )
        )

    return _dedupe(_sort_changes(changes))


def compare_signatures(base: Signature, head: Signature):
    return _SignatureVerdict.from_signatures(base, head)


class _SignatureVerdict:
    def __init__(self) -> None:
        self.breaking: list[str] = []
        self.needs_human: list[str] = []
        self.additive: list[str] = []

    @property
    def has_difference(self) -> bool:
        return bool(self.breaking or self.needs_human or self.additive)

    @classmethod
    def from_signatures(cls, base: Signature, head: Signature) -> "_SignatureVerdict":
        verdict = cls()
        base_params = {param.name: param for param in base.params}
        head_params = {param.name: param for param in head.params}

        for name, param in base_params.items():
            if name not in head_params:
                verdict.breaking.append(f"parameter `{name}` was removed or renamed")

        for name, param in head_params.items():
            if name in base_params:
                continue
            if param.required:
                verdict.breaking.append(
                    f"required parameter `{name}` was added (existing callers omit it)"
                )
            else:
                verdict.additive.append(f"optional parameter `{name}` was added")

        common_base = [
            p.name for p in base.params if p.kind in _POSITIONAL_KINDS and p.name in head_params
        ]
        common_head = [
            p.name for p in head.params if p.kind in _POSITIONAL_KINDS and p.name in base_params
        ]
        if common_base != common_head:
            verdict.breaking.append(
                "positional parameter order changed: "
                f"{common_base} -> {common_head}"
            )

        for name in base_params:
            if name not in head_params:
                continue
            before, after = base_params[name], head_params[name]
            if before.kind != after.kind:
                verdict.breaking.append(
                    f"parameter `{name}` changed kind {before.kind} -> {after.kind}"
                )
            if before.default != after.default:
                verdict.needs_human.append(
                    f"default for `{name}` changed: {before.default!r} -> {after.default!r}"
                )
            if before.annotation != after.annotation:
                verdict.needs_human.append(
                    f"annotation for `{name}` changed: {before.annotation!r} -> "
                    f"{after.annotation!r}"
                )

        if base.returns != head.returns:
            verdict.needs_human.append(
                f"return annotation changed: {base.returns!r} -> {head.returns!r}"
            )
        if base.is_async != head.is_async:
            verdict.needs_human.append(
                f"async-ness changed: {base.is_async} -> {head.is_async}"
            )
        # Deduplicate while preserving order.
        verdict.breaking = list(dict.fromkeys(verdict.breaking))
        verdict.needs_human = list(dict.fromkeys(verdict.needs_human))
        verdict.additive = list(dict.fromkeys(verdict.additive))
        return verdict


def call_mismatch(signature: Signature, call) -> tuple[bool, str]:
    """Return (incompatible, explanation) for a call site against a signature."""

    if call.has_star_args or call.arg_count is None:
        return False, "call uses argument unpacking; arity not statically known"
    problems: list[str] = []
    positional_params = [
        param for param in signature.params if param.kind in _POSITIONAL_KINDS
    ]
    if not signature.accepts_positional and call.arg_count > len(positional_params):
        problems.append(
            f"passes {call.arg_count} positional args but head accepts at most "
            f"{len(positional_params)}"
        )
    filled_by_position = {
        param.name for param in positional_params[: call.arg_count]
    }
    provided = filled_by_position | set(call.kwargs)
    missing = [
        param.name
        for param in signature.params
        if param.required and param.name not in provided
    ]
    if missing:
        problems.append(
            "missing required argument(s) at head: " + ", ".join(sorted(missing))
        )
    accepted = signature.accepted_keywords()
    if accepted is not None:
        unexpected = [name for name in call.kwargs if name not in accepted]
        if unexpected:
            problems.append(
                "passes keyword(s) not accepted at head: "
                + ", ".join(sorted(unexpected))
            )
    if problems:
        return True, "; ".join(problems)
    return False, "call shape is compatible with the head signature"


def _annotate_consumers(
    hits: list[ConsumerHit],
    graph: RepoGraph,
    signature: Signature,
    *,
    target_name: str,
    removed: bool = False,
) -> list[ConsumerHit]:
    annotated: list[ConsumerHit] = []
    for hit in hits:
        module = graph.modules.get(hit.path)
        candidates = []
        if module is not None:
            candidates = [
                candidate
                for candidate in module.calls
                if candidate.line == hit.line and candidate.in_symbol == hit.symbol
            ]
        call = next(
            (candidate for candidate in candidates if candidate.name == target_name),
            candidates[0] if candidates else None,
        )
        if removed:
            if hit.depth == 1:
                annotated.append(
                    replace(
                        hit,
                        impact="broken",
                        detail="call site still exists at the analysed revision",
                    )
                )
            else:
                annotated.append(hit)
            continue
        if hit.depth > 1 or call is None:
            annotated.append(hit)
            continue
        if call.has_star_args or call.arg_count is None:
            annotated.append(
                replace(
                    hit,
                    impact="unknown",
                    detail="call uses argument unpacking; arity not statically known",
                )
            )
            continue
        incompatible, explanation = call_mismatch(signature, call)
        annotated.append(
            replace(hit, impact="broken" if incompatible else "compatible", detail=explanation)
        )
    return annotated


def _route_changes(
    *,
    base_mod: ModuleInfo | None,
    head_mod: ModuleInfo | None,
    file_diff: FileDiff,
    base_rev: str,
    head_rev: str,
    head_path: str,
) -> list[Change]:
    base_by_handler: dict[str, list[RouteInfo]] = {}
    head_by_handler: dict[str, list[RouteInfo]] = {}
    for route in base_mod.routes if base_mod else []:
        base_by_handler.setdefault(route.handler.split("::", 1)[-1], []).append(route)
    for route in head_mod.routes if head_mod else []:
        head_by_handler.setdefault(route.handler.split("::", 1)[-1], []).append(route)

    changes: list[Change] = []
    for qualname in sorted(set(base_by_handler) | set(head_by_handler)):
        before_routes = base_by_handler.get(qualname, [])
        after_routes = head_by_handler.get(qualname, [])
        before = {route.route_id: route for route in before_routes}
        after = {route.route_id: route for route in after_routes}
        only_before = [route for route_id, route in before.items() if route_id not in after]
        only_after = [route for route_id, route in after.items() if route_id not in before]
        symbol = (after_routes or before_routes)[0].handler

        if only_before and only_after:
            for old_route, new_route in zip(only_before, only_after):
                changes.append(
                    Change(
                        kind=ChangeKind.ROUTE_CHANGED,
                        symbol=symbol,
                        summary=(
                            f"Route changed for `{qualname}`: "
                            f"{old_route.route_id} -> {new_route.route_id}"
                        ),
                        classification=Classification.BREAKING,
                        confidence=Confidence.OBSERVED,
                        evidence=[
                            Evidence(
                                "route", old_route.file, base_rev,
                                old_route.line, old_route.line,
                            ),
                            Evidence(
                                "route", new_route.file, head_rev,
                                new_route.line, new_route.line,
                            ),
                            *_optional_evidence(
                                file_diff, head_path,
                                base_rev if file_diff.status == "deleted" else head_rev,
                            ),
                        ],
                        file=head_path,
                        route_before=old_route,
                        route_after=new_route,
                        reasons=["HTTP method or path changed"],
                    )
                )
            for extra in only_before[len(only_after):]:
                changes.append(_removed_route(extra, base_rev, head_rev, file_diff, head_path))
            for extra in only_after[len(only_before):]:
                changes.append(_added_route(extra, head_rev, file_diff, head_path))
        else:
            for route in only_before:
                changes.append(_removed_route(route, base_rev, head_rev, file_diff, head_path))
            for route in only_after:
                changes.append(_added_route(route, head_rev, file_diff, head_path))

        for route_id, old_route in before.items():
            new_route = after.get(route_id)
            if new_route is None or old_route.status_code == new_route.status_code:
                continue
            changes.append(
                Change(
                    kind=ChangeKind.STATUS_CHANGED,
                    symbol=symbol,
                    summary=(
                        f"Response status for {route_id} changed: "
                        f"{old_route.status_code} -> {new_route.status_code}"
                    ),
                    classification=Classification.NEEDS_HUMAN,
                    confidence=Confidence.NEEDS_HUMAN,
                    evidence=[
                        Evidence(
                            "route", old_route.file, base_rev,
                            old_route.line, old_route.line,
                        ),
                        Evidence(
                            "route", new_route.file, head_rev,
                            new_route.line, new_route.line,
                        ),
                    ],
                    file=head_path,
                    route_before=old_route,
                    route_after=new_route,
                    reasons=[
                        "static analysis cannot prove whether clients handle the new status"
                    ],
                )
            )
    return changes


def _removed_route(
    route: RouteInfo,
    base_rev: str,
    head_rev: str,
    file_diff: FileDiff,
    head_path: str,
) -> Change:
    return Change(
        kind=ChangeKind.ROUTE_REMOVED,
        symbol=route.handler,
        summary=f"Route removed: {route.route_id}",
        classification=Classification.BREAKING,
        confidence=Confidence.OBSERVED,
        evidence=[
            Evidence("route", route.file, base_rev, route.line, route.line),
            *_optional_evidence(
                file_diff,
                head_path,
                base_rev if file_diff.status == "deleted" else head_rev,
            ),
        ],
        file=head_path,
        route_before=route,
        reasons=["external HTTP clients calling this route will fail"],
    )


def _added_route(
    route: RouteInfo, head_rev: str, file_diff: FileDiff, head_path: str
) -> Change:
    return Change(
        kind=ChangeKind.ROUTE_ADDED,
        symbol=route.handler,
        summary=f"Route added: {route.route_id}",
        classification=Classification.ADDITIVE,
        confidence=Confidence.OBSERVED,
        evidence=[
            Evidence("route", route.file, head_rev, route.line, route.line),
            *_optional_evidence(file_diff, head_path, head_rev),
        ],
        file=head_path,
        route_after=route,
        reasons=["new route, no existing caller can be broken"],
    )


def _source_evidence(
    function: FunctionInfo, revision: str, file_diff: FileDiff, head_path: str
) -> list[Evidence]:
    return [
        Evidence(
            "source",
            function.path,
            revision,
            function.start_line,
            function.end_line,
            detail="definition at head",
        ),
        *_optional_evidence(file_diff, head_path, revision),
    ]


def _pair_evidence(
    base_func: FunctionInfo,
    base_rev: str,
    head_func: FunctionInfo,
    head_rev: str,
    file_diff: FileDiff,
    head_path: str,
) -> list[Evidence]:
    return [
        Evidence(
            "source",
            base_func.path,
            base_rev,
            base_func.start_line,
            base_func.end_line,
            detail="definition at base",
        ),
        Evidence(
            "source",
            head_func.path,
            head_rev,
            head_func.start_line,
            head_func.end_line,
            detail="definition at head",
        ),
        *_optional_evidence(file_diff, head_path, head_rev),
    ]


def _removed_evidence(
    base_func: FunctionInfo,
    base_rev: str,
    file_diff: FileDiff,
    head_path: str,
    head_rev: str,
) -> list[Evidence]:
    return [
        Evidence(
            "source",
            base_func.path,
            base_rev,
            base_func.start_line,
            base_func.end_line,
            detail="definition at base; absent at head",
        ),
        *_optional_evidence(file_diff, head_path, head_rev),
    ]


def _optional_evidence(
    file_diff: FileDiff, path: str, revision: str
) -> list[Evidence]:
    if not file_diff.hunks:
        return []
    starts = [hunk.new_start for hunk in file_diff.hunks]
    ends = [hunk.new_start + max(hunk.new_len, 1) - 1 for hunk in file_diff.hunks]
    return [
        Evidence(
            "diff",
            path,
            revision,
            min(starts),
            max(ends),
            detail="diff hunks that produced this finding",
        )
    ]


def _public(functions: dict[str, FunctionInfo]) -> dict[str, FunctionInfo]:
    return {name: fn for name, fn in functions.items() if fn.is_public}


def _sort_changes(changes: list[Change]) -> list[Change]:
    order = {
        Classification.BREAKING: 0,
        Classification.NEEDS_HUMAN: 1,
        Classification.ADDITIVE: 2,
    }
    return sorted(
        changes,
        key=lambda change: (order[change.classification], change.file, change.symbol, change.kind.value),
    )


def _dedupe(changes: list[Change]) -> list[Change]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[Change] = []
    for change in changes:
        key = (change.file, change.symbol, change.kind.value, change.summary)
        if key in seen:
            continue
        seen.add(key)
        unique.append(change)
    return unique
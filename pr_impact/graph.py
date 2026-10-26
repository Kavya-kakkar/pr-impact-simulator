"""Static call/dependency graph.

Every edge carries its resolution state. Ambiguous and unresolved edges are
kept in the graph (and surfaced in the dossier's limitations) rather than being
silently discarded, so "zero consumers" is never confused with "not traced".
"""

from __future__ import annotations

import posixpath
from collections import deque
from dataclasses import dataclass, field

from .model import (
    ConsumerHit,
    FunctionInfo,
    GraphEdge,
    ImportRef,
    ModuleInfo,
    Resolution,
)
from .parsing import is_package_file, module_name

_JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Names that are not API surface and cannot be resolved in-repo.
_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "Exception", "float",
    "getattr", "hasattr", "int", "isinstance", "len", "list", "map", "max",
    "min", "open", "print", "range", "repr", "reversed", "round", "set",
    "setattr", "sorted", "str", "sum", "super", "tuple", "type", "zip",
    "ValueError", "TypeError", "KeyError", "RuntimeError", "AttributeError",
    "NotImplementedError", "AssertionError", "ZeroDivisionError", "OSError",
    "IOError", "ImportError", "StopIteration", "object", "property",
}


@dataclass
class RepoGraph:
    revision: str
    modules: dict[str, ModuleInfo]
    nodes: dict[str, FunctionInfo] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    module_paths: dict[str, str] = field(default_factory=dict)
    reverse: dict[str, list[GraphEdge]] = field(default_factory=dict)

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)
        if edge.target:
            self.reverse.setdefault(edge.target, []).append(edge)

    def consumers(self, symbol: str, max_depth: int = 3) -> list[ConsumerHit]:
        """Walk reverse edges from ``symbol`` up to ``max_depth`` hops."""

        hits: dict[tuple[str, int], ConsumerHit] = {}
        seen = {symbol}
        queue: deque[tuple[str, int]] = deque([(symbol, 0)])
        while queue:
            target, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self.reverse.get(target, []):
                if edge.source == target:
                    continue
                key = (edge.source, edge.line)
                if key not in hits:
                    hits[key] = ConsumerHit(
                        symbol=edge.source,
                        path=edge.path,
                        line=edge.line,
                        depth=depth + 1,
                        resolution=edge.resolution,
                        detail=edge.detail,
                        revision=self.revision,
                    )
                if edge.source not in seen:
                    seen.add(edge.source)
                    queue.append((edge.source, depth + 1))
        return sorted(hits.values(), key=lambda hit: (hit.depth, hit.path, hit.line))

    def unresolved_edges(self) -> list[GraphEdge]:
        return [edge for edge in self.edges if edge.resolution is not Resolution.RESOLVED]

def build_graph(revision: str, modules: dict[str, ModuleInfo]) -> RepoGraph:
    graph = RepoGraph(revision=revision, modules=dict(modules))
    for info in modules.values():
        for function in info.functions.values():
            graph.nodes[function.symbol] = function
    for path in modules:
        graph.module_paths.setdefault(module_name(path), path)

    for info in modules.values():
        for call in info.calls:
            if call.in_symbol == "<module>":
                continue
            target, resolution, detail = _resolve(
                graph, info, call.receiver, call.name, call.in_symbol
            )
            graph.add_edge(
                GraphEdge(
                    source=call.in_symbol,
                    target=target,
                    resolution=resolution,
                    path=info.path,
                    line=call.line,
                    detail=detail,
                )
            )
    return graph


def _resolve(
    graph: RepoGraph,
    module: ModuleInfo,
    receiver: str | None,
    name: str,
    caller_symbol: str,
) -> tuple[str | None, Resolution, str]:
    if not name:
        return None, Resolution.UNRESOLVED, "empty callee name"

    if receiver is None:
        if name in _BUILTINS:
            return None, Resolution.EXTERNAL, f"`{name}` is a Python builtin"
        ref = module.imports.get(name)
        if ref is not None:
            target = _target_from_import(graph, module, ref, name)
            if target is not None:
                return target, Resolution.RESOLVED, f"imported from {ref.module}"
            if _imported_is_class(graph, module, ref):
                return (
                    None,
                    Resolution.EXTERNAL,
                    f"`{name}` is a class instantiation, not a tracked function",
                )
            if _import_is_external(graph, module, ref):
                return (
                    None,
                    Resolution.EXTERNAL,
                    f"`{ref.module}` is outside the analysed repository",
                )
            return (
                None,
                Resolution.UNRESOLVED,
                f"`{name}` is imported from `{ref.module}` but no matching "
                "function exists in that module at this revision",
            )
        local = graph.nodes.get(f"{module.path}::{name}")
        if local is not None:
            return local.symbol, Resolution.RESOLVED, "local module function"
        if name in module.classes:
            return None, Resolution.EXTERNAL, f"`{name}` is a class defined in this module"
        return (
            None,
            Resolution.UNRESOLVED,
            f"no local definition or import for `{name}` in {module.path}",
        )

    ref = module.imports.get(receiver)
    if ref is not None:
        target = _target_from_import(graph, module, ref, name, receiver_is_module=True)
        if target is not None:
            return target, Resolution.RESOLVED, f"module import `{receiver}`"
        if _imported_is_class(graph, module, ref):
            return (
                None,
                Resolution.EXTERNAL,
                f"`{receiver}.{name}` is an attribute of a class, not a function",
            )
        if _import_is_external(graph, module, ref):
            return (
                None,
                Resolution.EXTERNAL,
                f"`{ref.module}` is outside the analysed repository",
            )
        return (
            None,
            Resolution.UNRESOLVED,
            f"`{receiver}.{name}` could not be mapped to a function in the "
            "imported module",
        )

    if receiver.strip() in ("self", "cls") and "." in caller_symbol.split("::", 1)[1]:
        class_name = caller_symbol.split("::", 1)[1].split(".")[0]
        method = graph.nodes.get(f"{module.path}::{class_name}.{name}")
        if method is not None:
            return method.symbol, Resolution.RESOLVED, f"same-class method via {receiver}"
        return (
            None,
            Resolution.UNRESOLVED,
            f"`{receiver}.{name}` is not defined on class {class_name}",
        )

    if receiver.split(".")[-1] in module.imports:
        return (
            None,
            Resolution.AMBIGUOUS,
            f"receiver `{receiver}` is an imported symbol; attribute resolution "
            "requires type inference",
        )

    return (
        None,
        Resolution.AMBIGUOUS,
        f"receiver type for `{receiver}.{name}` is not statically known",
    )


def _target_from_import(
    graph: RepoGraph,
    module: ModuleInfo,
    ref: ImportRef,
    name: str,
    receiver_is_module: bool = False,
) -> str | None:
    if graph.modules and module.language != "python":
        return _target_from_js_import(graph, module, ref, name)

    if ref.imported is None:
        module_path = _python_module_path(graph, module, ref)
        if module_path is None:
            return None
        return _function_in(graph, module_path, name)

    module_name_resolved = _python_module_name(module.path, ref)
    direct_path = graph.module_paths.get(module_name_resolved)
    if direct_path is not None and not receiver_is_module:
        target = _function_in(graph, direct_path, ref.imported)
        if target is not None:
            return target

    # `from package import submodule` style: the name may be a module.
    nested = graph.module_paths.get(f"{module_name_resolved}.{ref.imported}")
    if nested is not None:
        return _function_in(graph, nested, name)

    if direct_path is not None and receiver_is_module:
        return _function_in(graph, direct_path, name)
    return None


def _import_is_external(graph: RepoGraph, module: ModuleInfo, ref: ImportRef) -> bool:
    if module.language != "python":
        return not ref.module.startswith(".")
    name = _python_module_name(module.path, ref)
    return not ref.relative and name not in graph.module_paths


def _imported_is_class(graph: RepoGraph, module: ModuleInfo, ref: ImportRef) -> bool:
    if not ref.imported or module.language != "python":
        return False
    module_name_resolved = _python_module_name(module.path, ref)
    for candidate in (
        module_name_resolved,
        f"{module_name_resolved}.{ref.imported}",
    ):
        path = graph.module_paths.get(candidate)
        if path and ref.imported in graph.modules[path].classes:
            return True
    return False


def _function_in(graph: RepoGraph, path: str, qualname: str) -> str | None:
    function = graph.nodes.get(f"{path}::{qualname}")
    return function.symbol if function is not None else None


def _python_module_name(caller_path: str, ref: ImportRef) -> str:
    raw = ref.module
    if not ref.relative:
        return raw
    dots = len(raw) - len(raw.lstrip("."))
    rest = raw.lstrip(".")
    caller_module = module_name(caller_path)
    parts = caller_module.split(".") if caller_module else []
    if not is_package_file(caller_path):
        parts = parts[:-1]
    drop = max(0, dots - 1)
    if drop:
        parts = parts[:-drop] if drop <= len(parts) else []
    if rest:
        parts = [*parts, rest]
    return ".".join(part for part in parts if part)


def _python_module_path(graph: RepoGraph, module: ModuleInfo, ref: ImportRef) -> str | None:
    return graph.module_paths.get(_python_module_name(module.path, ref))


def _target_from_js_import(
    graph: RepoGraph, module: ModuleInfo, ref: ImportRef, name: str
) -> str | None:
    path = _js_module_path(graph, module.path, ref.module)
    if path is None:
        return None
    if ref.imported in (None, "default"):
        return _function_in(graph, path, name)
    return _function_in(graph, path, ref.imported)


def _js_module_path(graph: RepoGraph, caller_path: str, raw: str) -> str | None:
    if not raw.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(caller_path), raw))
    for candidate in (
        base,
        *(base + suffix for suffix in _JS_SUFFIXES),
        *(posixpath.join(base, "index" + suffix) for suffix in _JS_SUFFIXES),
    ):
        if candidate in graph.modules:
            return candidate
    return None
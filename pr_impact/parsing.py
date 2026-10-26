"""Tree-sitter parsing and API-surface extraction.

Facts extracted here are purely syntactic: signatures, decorators, imports,
call sites and route declarations. Nothing in this module infers behaviour.

Python is the supported language. TypeScript/JavaScript support is explicitly
best-effort: top-level functions, arrows, class methods, imports and call
expressions are recognised; decorators, overloads and dynamic patterns are not.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Language, Node, Parser

from .model import (
    CallSite,
    FunctionInfo,
    ImportRef,
    ModuleInfo,
    Param,
    RouteInfo,
    Signature,
)

try:  # Python grammar is required.
    import tree_sitter_python
except ImportError as exc:  # pragma: no cover - packaging failure
    raise RuntimeError("tree-sitter-python is required") from exc

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

_ROUTE_RE = re.compile(
    r"^@?(?P<receiver>[A-Za-z_][\w.]*)\.(?P<method>get|post|put|patch|delete|head|options)"
    r"\(\s*(?P<quote>[\"'])(?P<path>/[^\"']*)(?P=quote)",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(r"status_code\s*=\s*(\d{3})")
_DOC_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_+-]*)")

_languages: dict[str, Language] = {"python": Language(tree_sitter_python.language())}
for _lang_name, _module_name in (
    ("javascript", "tree_sitter_javascript"),
    ("typescript", "tree_sitter_typescript"),
    ("tsx", "tree_sitter_typescript"),
):
    try:
        module = __import__(_module_name)
    except ImportError:  # pragma: no cover - optional dependency
        continue
    if _lang_name == "tsx":  # pragma: no cover - optional
        _languages[_lang_name] = Language(module.language_tsx())
    elif _lang_name == "typescript":  # pragma: no cover - optional
        _languages[_lang_name] = Language(module.language_typescript())
    else:  # pragma: no cover - optional
        _languages[_lang_name] = Language(module.language())

_parsers = {name: Parser(language) for name, language in _languages.items()}

ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def detect_language(path: str) -> str | None:
    return LANGUAGE_BY_EXT.get(Path(path).suffix.lower())


def module_name(path: str) -> str:
    """Return the dotted module name for a supported source path."""

    suffix = Path(path).suffix
    stem = path[: -len(suffix)] if suffix else path
    parts = stem.replace("\\", "/").split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(part for part in parts if part)


def is_package_file(path: str) -> bool:
    return Path(path).name == "__init__.py"


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", "replace")


def _walk(node: Node):
    """Depth-first walk that yields named nodes (avoids recursion limits)."""

    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def parse_source(source: bytes, language: str):
    parser = _parsers.get(language)
    if parser is None:
        raise ValueError(f"unsupported language: {language}")
    return parser.parse(source)


def parse_module(path: str, source: bytes) -> ModuleInfo:
    language = detect_language(path)
    if language is None:
        raise ValueError(f"unsupported file for static analysis: {path}")

    info = ModuleInfo(path=path, language=language)
    try:
        tree = parse_source(source, language)
    except Exception as exc:  # tree-sitter can fail on pathological input
        info.parse_error = f"{type(exc).__name__}: {exc}"
        return info

    root = tree.root_node
    if root.has_error:
        info.parse_error = "tree-sitter reported syntax errors; extraction is partial"

    if language == "python":
        _collect_python(path, root, info)
    else:
        _collect_js(path, root, info)
    return info


# --------------------------------------------------------------------------
# Python extraction
# --------------------------------------------------------------------------


def _collect_python(path: str, root: Node, info: ModuleInfo) -> None:
    body = root.named_children
    for node in body:
        _collect_python_node(path, node, info, class_name=None, decorators=())
    _collect_python_imports(root, info)
    for function in list(info.functions.values()):
        body_node = _python_function_body(root, function)
        if body_node is not None:
            _collect_calls(body_node, function.symbol, info)
    _collect_python_routes(root, info)


def _collect_python_node(
    path: str,
    node: Node,
    info: ModuleInfo,
    class_name: str | None,
    decorators: tuple[str, ...],
) -> None:
    if node.type == "decorated_definition":
        decorators = tuple(
            _text(child)[1:] if _text(child).startswith("@") else _text(child)
            for child in node.named_children
            if child.type == "decorator"
        )
        definition = node.child_by_field_name("definition")
        if definition is not None:
            _collect_python_node(path, definition, info, class_name, decorators)
        return
    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        name = _text(name_node)
        if not name:
            return
        qualname = f"{class_name}.{name}" if class_name else name
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        info.functions[qualname] = FunctionInfo(
            path=path,
            qualname=qualname,
            name=name,
            signature=_python_signature(node, decorators),
            start_line=start,
            end_line=end,
            language="python",
            class_name=class_name,
            is_public=not name.startswith("_"),
        )
        return
    if node.type == "class_definition":
        class_node = node.child_by_field_name("name")
        class_name = _text(class_node) or None
        if class_name:
            info.classes.add(class_name)
        body_node = node.child_by_field_name("body")
        if body_node is None:
            return
        for child in body_node.named_children:
            _collect_python_node(path, child, info, class_name, ())
        return


def _python_function_body(root: Node, function: FunctionInfo) -> Node | None:
    """Find the body node for a registered function by walking definitions."""

    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            name = _text(node.child_by_field_name("name"))
            qualname = function.qualname.split(".")[-1]
            if name == qualname and node.start_point[0] + 1 == function.start_line:
                return node.child_by_field_name("body")
        stack.extend(reversed(node.named_children))
    return None


def _python_signature(node: Node, decorators: tuple[str, ...]) -> Signature:
    params_node = node.child_by_field_name("parameters")
    params = _python_params(params_node) if params_node is not None else ()
    return_node = node.child_by_field_name("return_type")
    is_async = any(_text(child) == "async" for child in node.children)
    return Signature(
        params=params,
        returns=_text(return_node) or None,
        is_async=is_async,
        decorators=decorators,
    )


def _python_params(params_node: Node) -> tuple[Param, ...]:
    # Separator positions matter: params before "/" are positional-only, params
    # after "*" (or after *args) are keyword-only.
    slash_index = None
    star_index = None
    named: list[tuple[int, Node]] = []
    for index, child in enumerate(params_node.children):
        token = _text(child)
        if child.type == "keyword_separator" or (not child.is_named and token == "*"):
            star_index = index
            continue
        if child.type == "positional_separator" or (not child.is_named and token == "/"):
            slash_index = index
            continue
        if not child.is_named or child.type == "comment":
            continue
        named.append((index, child))

    params: list[Param] = []
    for index, child in named:
        token = _text(child)
        positional_only = slash_index is not None and index < slash_index
        keyword_only = star_index is not None and index > star_index
        kind = (
            "keyword_only"
            if keyword_only
            else ("posonly" if positional_only else "positional")
        )
        if child.type == "list_splat_pattern":
            params.append(Param(name=token.lstrip("*").strip(), kind="var_positional"))
            continue
        if child.type == "dictionary_splat_pattern":
            params.append(Param(name=token.lstrip("*").strip(), kind="var_keyword"))
            continue

        name_node = child.child_by_field_name("name")
        if name_node is None and child.named_children:
            name_node = child.named_children[0]
        name = _text(name_node) or (token if child.type == "identifier" else "")
        annotation = None
        type_node = child.child_by_field_name("type")
        if type_node is not None:
            annotation = _text(type_node)
        elif child.type == "typed_parameter" and len(child.named_children) > 1:
            annotation = _text(child.named_children[1])
        default = None
        value_node = child.child_by_field_name("value")
        if value_node is not None:
            default = _text(value_node)
        if not name:
            continue
        params.append(
            Param(name=name, kind=kind, annotation=annotation, default=default)
        )
    return tuple(params)


def _collect_python_imports(root: Node, info: ModuleInfo) -> None:
    for node in root.named_children:
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "aliased_import":
                    module = _text(child.child_by_field_name("name"))
                    local = _text(child.child_by_field_name("alias")) or module.split(".")[0]
                else:
                    module = _text(child)
                    local = module.split(".")[0]
                if module:
                    info.imports[local] = ImportRef(
                        local=local,
                        module=module,
                        imported=None,
                        line=node.start_point[0] + 1,
                    )
            continue
        if node.type != "import_from_statement":
            continue
        children = node.named_children
        if not children:
            continue
        module_node = children[0]
        module = _text(module_node)
        relative = module_node.type == "relative_import"
        rest = children[1:]
        if not rest:
            continue
        for child in rest:
            if child.type == "aliased_import":
                imported = _text(child.child_by_field_name("name"))
                local = _text(child.child_by_field_name("alias")) or imported
            else:
                imported = _text(child)
                local = imported
            if not imported:
                continue
            info.imports[local] = ImportRef(
                local=local,
                module=module,
                imported=imported,
                line=node.start_point[0] + 1,
                relative=relative,
            )


def _collect_calls(body: Node, symbol: str, info: ModuleInfo) -> None:
    for node in _walk(body):
        site = _python_call_site(node, symbol)
        if site is not None:
            info.calls.append(site)


def _python_call_site(node: Node, symbol: str) -> CallSite | None:
    if node.type != "call":
        return None
    function_node = node.child_by_field_name("function")
    if function_node is None:
        return None
    if function_node.type == "identifier":
        callee = _text(function_node)
        receiver = None
        name = callee
    elif function_node.type == "attribute":
        obj = function_node.child_by_field_name("object")
        attr = function_node.child_by_field_name("attribute")
        receiver = _text(obj)
        name = _text(attr)
        callee = _text(function_node)
    else:
        return None
    arguments = node.child_by_field_name("arguments")
    positional = 0
    kwargs: list[str] = []
    has_star = False
    if arguments is not None:
        for arg in arguments.named_children:
            if arg.type == "comment":
                continue
            if arg.type == "keyword_argument":
                keyword = _text(arg.child_by_field_name("name"))
                if keyword:
                    kwargs.append(keyword)
                continue
            text = _text(arg)
            if text.startswith("*"):
                has_star = True
                continue
            positional += 1
    return CallSite(
        callee=callee,
        name=name,
        receiver=receiver,
        line=node.start_point[0] + 1,
        in_symbol=symbol,
        arg_count=positional,
        kwargs=tuple(kwargs),
        has_star_args=has_star,
    )


def _collect_python_routes(root: Node, info: ModuleInfo) -> None:
    by_start = {function.start_line: function for function in info.functions.values()}
    for node in _walk(root):
        if node.type != "decorated_definition":
            continue
        definition = node.child_by_field_name("definition")
        if definition is None or definition.type != "function_definition":
            continue
        function = by_start.get(definition.start_point[0] + 1)
        if function is None:
            continue
        for child in node.named_children:
            if child.type != "decorator":
                continue
            text = _text(child).lstrip("@").strip()
            match = _ROUTE_RE.match(text)
            if not match:
                continue
            method = match.group("method").lower()
            if method not in ROUTE_METHODS:
                continue
            status_match = _STATUS_RE.search(text)
            info.routes.append(
                RouteInfo(
                    method=method.upper(),
                    path=match.group("path"),
                    handler=function.symbol,
                    status_code=int(status_match.group(1)) if status_match else None,
                    line=child.start_point[0] + 1,
                    file=info.path,
                )
            )


# --------------------------------------------------------------------------
# TypeScript / JavaScript extraction (best effort)
# --------------------------------------------------------------------------


def _collect_js(path: str, root: Node, info: ModuleInfo) -> None:
    for node in root.named_children:
        _collect_js_node(path, node, info, exported=False)
    _collect_js_imports(root, info)
    for function in list(info.functions.values()):
        body = _js_function_body(root, function)
        if body is not None:
            _collect_js_calls(body, function.symbol, info)


def _collect_js_node(
    path: str, node: Node, info: ModuleInfo, exported: bool, class_name: str | None = None
) -> None:
    if node.type == "export_statement":
        for child in node.named_children:
            _collect_js_node(path, child, info, exported=True, class_name=class_name)
        return
    if node.type == "function_declaration":
        _register_js_function(path, node, info, exported, class_name)
        return
    if node.type == "lexical_declaration":
        for child in node.named_children:
            if child.type == "variable_declarator":
                value = child.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression"):
                    _register_js_function(path, child, info, exported, class_name, value_node=value)
        return
    if node.type == "class_declaration":
        name = _text(node.child_by_field_name("name"))
        if name:
            info.classes.add(name)
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                if child.type == "method_definition":
                    _register_js_function(path, child, info, exported, name)
        return


def _register_js_function(
    path: str,
    node: Node,
    info: ModuleInfo,
    exported: bool,
    class_name: str | None,
    value_node: Node | None = None,
) -> None:
    if node.type == "method_definition":
        name_node = node.child_by_field_name("name")
        params_node = node.child_by_field_name("parameters")
        body = node.child_by_field_name("body")
    elif node.type == "variable_declarator":
        name_node = node.child_by_field_name("name")
        params_node = value_node.child_by_field_name("parameters") if value_node else None
        body = value_node.child_by_field_name("body") if value_node else None
        if body is None and value_node is not None and value_node.type == "arrow_function":
            body = value_node.child_by_field_name("body")
    else:
        name_node = node.child_by_field_name("name")
        params_node = node.child_by_field_name("parameters")
        body = node.child_by_field_name("body")
    name = _text(name_node)
    if not name or body is None:
        return
    qualname = f"{class_name}.{name}" if class_name else name
    signature = Signature(
        params=_js_params(params_node) if params_node is not None else (),
        returns=_text(node.child_by_field_name("return_type")) or None,
        is_async=any(_text(child) == "async" for child in node.children),
        decorators=(),
    )
    info.functions[qualname] = FunctionInfo(
        path=path,
        qualname=qualname,
        name=name,
        signature=signature,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        language=info.language,
        class_name=class_name,
        is_public=exported if class_name is None else (not name.startswith(("#", "_"))),
    )


def _js_params(params_node: Node) -> tuple[Param, ...]:
    params: list[Param] = []
    for child in params_node.named_children:
        if child.type in ("required_parameter", "optional_parameter"):
            pattern = child.child_by_field_name("pattern")
            name = _text(pattern)
            annotation = _text(child.child_by_field_name("type")) or None
            default = _text(child.child_by_field_name("value")) or None
            if child.type == "optional_parameter" and default is None:
                default = "?"
            params.append(Param(name=name, kind="positional", annotation=annotation, default=default))
        elif child.type == "rest_pattern":
            params.append(Param(name=_text(child).lstrip("."), kind="var_positional"))
        elif child.type == "identifier":
            params.append(Param(name=_text(child), kind="positional"))
        elif child.type == "assignment_pattern":
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            params.append(
                Param(name=_text(left), kind="positional", default=_text(right) or None)
            )
    return tuple(params)


def _collect_js_imports(root: Node, info: ModuleInfo) -> None:
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        source = None
        for child in node.named_children:
            if child.type == "string":
                source = _text(child).strip("'\"")
                break
        if not source:
            continue
        for child in node.named_children:
            if child.type != "import_clause":
                continue
            for part in child.named_children:
                if part.type == "identifier":
                    info.imports[_text(part)] = ImportRef(
                        local=_text(part), module=source, imported="default",
                        line=node.start_point[0] + 1,
                    )
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        if spec.type != "import_specifier":
                            continue
                        imported = _text(spec.child_by_field_name("name"))
                        alias = _text(spec.child_by_field_name("alias"))
                        local = alias or imported
                        if imported:
                            info.imports[local] = ImportRef(
                                local=local, module=source, imported=imported,
                                line=node.start_point[0] + 1,
                            )


def _js_function_body(root: Node, function: FunctionInfo) -> Node | None:
    stack = [root]
    name = function.qualname.split(".")[-1]
    while stack:
        node = stack.pop()
        candidate_name = _text(node.child_by_field_name("name")) if node.type != "variable_declarator" else _text(node.child_by_field_name("name"))
        if candidate_name == name:
            body = node.child_by_field_name("body")
            if body is not None:
                return body
            value = node.child_by_field_name("value")
            if value is not None:
                return value.child_by_field_name("body")
        for child in reversed(node.named_children):
            stack.append(child)
    return None


def _collect_js_calls(body: Node, symbol: str, info: ModuleInfo) -> None:
    for node in _walk(body):
        site = _js_call_site(node, symbol)
        if site is not None:
            info.calls.append(site)


def _js_call_site(node: Node, symbol: str) -> CallSite | None:
    if node.type != "call_expression":
        return None
    function_node = node.child_by_field_name("function")
    if function_node is None:
        return None
    if function_node.type == "identifier":
        name = _text(function_node)
        receiver = None
    elif function_node.type == "member_expression":
        name = _text(function_node.child_by_field_name("property"))
        receiver = _text(function_node.child_by_field_name("object"))
    else:
        return None
    arguments = node.child_by_field_name("arguments")
    positional = 0
    has_star = False
    if arguments is not None:
        for arg in arguments.named_children:
            if arg.type == "comment":
                continue
            if arg.type == "spread_element":
                has_star = True
                continue
            positional += 1
    return CallSite(
        callee=_text(function_node),
        name=name,
        receiver=receiver,
        line=node.start_point[0] + 1,
        in_symbol=symbol,
        arg_count=positional,
        has_star_args=has_star,
    )


def calls_in_source(source: bytes, language: str, symbol: str = "<module>") -> list[CallSite]:
    """Extract every call in a snippet (used for documentation code blocks)."""

    tree = parse_source(source, language)
    calls: list[CallSite] = []
    for node in _walk(tree.root_node):
        if language == "python":
            site = _python_call_site(node, symbol)
        elif language in ("javascript", "typescript", "tsx"):
            site = _js_call_site(node, symbol)
        else:
            site = None
        if site is not None:
            calls.append(site)
    return calls


# --------------------------------------------------------------------------
# Markdown helpers
# --------------------------------------------------------------------------


def markdown_code_blocks(text: str) -> list[tuple[str, int, str]]:
    """Return (language, first_content_line, code) for fenced blocks."""

    blocks: list[tuple[str, int, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = _DOC_FENCE_RE.match(lines[index])
        if not match:
            index += 1
            continue
        fence, language = match.group(1), match.group(2).lower()
        start = index + 1
        body: list[str] = []
        index += 1
        while index < len(lines) and not lines[index].strip().startswith(fence[:3]):
            body.append(lines[index])
            index += 1
        blocks.append((language, start + 1, "\n".join(body)))
        index += 1
    return blocks
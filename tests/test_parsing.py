from __future__ import annotations

import pytest

from pr_impact.parsing import (
    calls_in_source,
    detect_language,
    markdown_code_blocks,
    module_name,
    parse_module,
)

PY_SOURCE = b'''from .services import create_user as make_user
from . import helpers


@router.get("/users/{uid}", status_code=201)
def get_user(uid: str, *, verbose: bool = False) -> dict:
    data = helpers.load(uid)
    return make_user(uid, "x@y.z", verbose=True)


async def stream(prefix, /, count: int, *args, **kwargs):
    return await collect(prefix, count)


class Service:
    def __init__(self, name: str) -> None:
        self.name = name

    def run(self):
        return self.helper()

    def helper(self):
        return 1
'''


def test_module_helpers():
    assert detect_language("a/b.py") == "python"
    assert detect_language("a/b.tsx") == "tsx"
    assert detect_language("a/README.md") is None
    assert module_name("sample_app/services.py") == "sample_app.services"
    assert module_name("sample_app/__init__.py") == "sample_app"


def test_signature_extraction():
    info = parse_module("pkg/mod.py", PY_SOURCE)
    assert info.parse_error is None
    get_user = info.functions["get_user"]
    signature = get_user.signature
    assert signature.returns == "dict"
    assert [param.name for param in signature.params] == ["uid", "verbose"]
    assert [param.kind for param in signature.params] == ["positional", "keyword_only"]
    assert signature.params[0].annotation == "str"
    assert signature.params[0].required
    assert not signature.params[1].required
    assert signature.params[1].default == "False"
    assert "def get_user(uid: str, verbose: bool = False) -> dict" in signature.render(
        "get_user"
    )


def test_positional_only_and_variadic_extraction():
    info = parse_module("pkg/mod.py", PY_SOURCE)
    stream = info.functions["stream"].signature
    assert stream.is_async
    assert stream.params[0].kind == "posonly"
    assert stream.params[1].kind == "positional"
    assert stream.params[2].kind == "var_positional"
    assert stream.params[3].kind == "var_keyword"
    assert stream.required_positional() == 2
    assert stream.max_positional() is None
    assert stream.accepted_keywords() is None


def test_imports_and_routes():
    info = parse_module("pkg/mod.py", PY_SOURCE)
    imported = info.imports["make_user"]
    assert imported.imported == "create_user"
    assert imported.module == ".services"
    assert imported.relative
    assert info.imports["helpers"].imported == "helpers"
    assert [route.route_id for route in info.routes] == ["GET /users/{uid}"]
    route = info.routes[0]
    assert route.status_code == 201
    assert route.line == 5  # points at the decorator, not the def
    assert route.handler == "pkg/mod.py::get_user"


def test_calls_are_attributed_to_enclosing_function():
    info = parse_module("pkg/mod.py", PY_SOURCE)
    calls = {(call.in_symbol, call.name) for call in info.calls}
    assert ("pkg/mod.py::get_user", "load") in calls
    assert ("pkg/mod.py::get_user", "make_user") in calls
    user_call = next(
        call for call in info.calls if call.in_symbol == "pkg/mod.py::get_user" and call.name == "make_user"
    )
    assert user_call.arg_count == 2
    assert user_call.kwargs == ("verbose",)
    assert ("pkg/mod.py::Service.run", "helper") in calls
    assert info.functions["Service.run"].class_name == "Service"


def test_parse_error_is_reported_without_crashing():
    info = parse_module("pkg/broken.py", b"def broken(:\n    pass\n")
    assert info.parse_error
    assert all(function.signature.params == () for function in info.functions.values())


def test_doc_calls_include_module_level_calls():
    calls = calls_in_source(
        b'from pkg import f\nresult = f(1, 2, mode="fast")\n', "python"
    )
    call = next(item for item in calls if item.name == "f")
    assert call.arg_count == 2
    assert call.kwargs == ("mode",)


def test_markdown_code_blocks():
    text = "# Title\n\n```python\nprint(1)\nprint(2)\n```\n\ntext\n"
    assert markdown_code_blocks(text) == [("python", 4, "print(1)\nprint(2)")]


def test_typescript_functions_if_available():
    pytest.importorskip("tree_sitter_javascript")
    source = b'''
import { createUser } from "./services";
export function handler(name: string) {
  return createUser(name, "x@y.z");
}
export const add = (a: number, b: number = 2) => a + b;
class Api {
  getUser(id: string) { return fetch(id); }
}
'''
    info = parse_module("web/api.ts", source)
    assert set(info.functions) == {"handler", "add", "Api.getUser"}
    assert info.functions["handler"].is_public
    assert [param.name for param in info.functions["handler"].signature.params] == ["name"]
    assert info.functions["add"].signature.params[1].default == "2"
    assert info.imports["createUser"].module == "./services"
    assert any(call.name == "createUser" for call in info.calls)
    assert info.functions["Api.getUser"].is_public
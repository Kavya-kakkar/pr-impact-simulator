from __future__ import annotations

from pr_impact.graph import build_graph
from pr_impact.model import Resolution
from pr_impact.parsing import parse_module


def _graph(sources: dict[str, str]):
    modules = {
        path: parse_module(path, source.encode("utf-8"))
        for path, source in sources.items()
    }
    return build_graph("head", modules)


def test_cross_module_import_is_resolved():
    graph = _graph(
        {
            "pkg/services.py": "def create_user(name, email):\n    return name\n",
            "pkg/clients.py": (
                "from .services import create_user\n\n"
                "def create_owner(name, email):\n"
                "    return create_user(name, email)\n"
            ),
        }
    )
    edge = next(edge for edge in graph.edges if edge.source == "pkg/clients.py::create_owner")
    assert edge.target == "pkg/services.py::create_user"
    assert edge.resolution is Resolution.RESOLVED
    assert edge.line == 4


def test_same_module_and_self_method_calls_resolve():
    graph = _graph(
        {
            "pkg/mod.py": (
                "def helper():\n    return 1\n\n"
                "def caller():\n    return helper()\n\n"
                "class Service:\n"
                "    def run(self):\n        return self.helper()\n\n"
                "    def helper(self):\n        return 2\n"
            )
        }
    )
    assert any(
        edge.source == "pkg/mod.py::caller" and edge.target == "pkg/mod.py::helper"
        for edge in graph.edges
    )
    assert any(
        edge.source == "pkg/mod.py::Service.run"
        and edge.target == "pkg/mod.py::Service.helper"
        for edge in graph.edges
    )


def test_ambiguous_and_unresolved_edges_are_recorded():
    graph = _graph(
        {
            "pkg/mod.py": (
                "def caller(obj):\n"
                "    obj.save()\n"
                "    missing()\n"
            )
        }
    )
    resolutions = {edge.resolution for edge in graph.edges}
    assert Resolution.AMBIGUOUS in resolutions
    assert Resolution.UNRESOLVED in resolutions
    assert all(edge.target is None for edge in graph.edges)


def test_builtins_classes_and_third_party_are_external():
    graph = _graph(
        {
            "pkg/models.py": "class User:\n    pass\n",
            "pkg/mod.py": (
                "import pytest\n"
                "from .models import User\n\n"
                "def build():\n"
                "    user = User()\n"
                "    pytest.raises(ValueError)\n"
                "    return len([user])\n"
            ),
        }
    )
    assert graph.edges
    assert all(edge.resolution is Resolution.EXTERNAL for edge in graph.edges)


def test_consumers_are_traced_transitively():
    graph = _graph(
        {
            "pkg/services.py": "def create_user(name):\n    return name\n",
            "pkg/clients.py": (
                "from .services import create_user\n\n"
                "def create_owner(name):\n"
                "    return create_user(name)\n"
            ),
            "pkg/api.py": (
                "from .clients import create_owner\n\n"
                "def handle(name):\n"
                "    return create_owner(name)\n"
            ),
        }
    )
    hits = graph.consumers("pkg/services.py::create_user", max_depth=3)
    assert [(hit.symbol, hit.depth) for hit in hits] == [
        ("pkg/clients.py::create_owner", 1),
        ("pkg/api.py::handle", 2),
    ]


def test_depth_limit_stops_the_walk():
    graph = _graph(
        {
            "pkg/a.py": "def target():\n    return 1\n",
            "pkg/b.py": (
                "from .a import target\n\ndef caller():\n    return target()\n"
            ),
            "pkg/c.py": (
                "from .b import caller\n\ndef outer():\n    return caller()\n"
            ),
        }
    )
    assert [hit.symbol for hit in graph.consumers("pkg/a.py::target", max_depth=1)] == [
        "pkg/b.py::caller"
    ]
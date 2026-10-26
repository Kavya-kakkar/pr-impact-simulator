"""Typed records shared by every stage of the pipeline.

The model deliberately keeps facts and inferences apart: a :class:`Change` or
:class:`DocDrift` cannot be constructed without evidence, and anything the
static layer could not establish is carried as a limitation instead of being
silently dropped or guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Confidence(str, Enum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    NEEDS_HUMAN = "needs-human"


class ChangeKind(str, Enum):
    FUNCTION_ADDED = "function-added"
    FUNCTION_REMOVED = "function-removed"
    SIGNATURE_CHANGED = "signature-changed"
    ROUTE_ADDED = "route-added"
    ROUTE_REMOVED = "route-removed"
    ROUTE_CHANGED = "route-changed"
    STATUS_CHANGED = "route-status-changed"


class Classification(str, Enum):
    BREAKING = "breaking"
    ADDITIVE = "additive"
    NEEDS_HUMAN = "needs-human"


class Resolution(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    EXTERNAL = "external"


@dataclass(frozen=True)
class Evidence:
    """A pointer a human can open and check.

    ``revision`` is the git rev the pointer was read from, which matters when
    the same path exists in both the base and head trees.
    """

    kind: str
    path: str
    revision: str
    line_start: int | None = None
    line_end: int | None = None
    detail: str = ""

    def loc(self) -> str:
        loc = self.path
        if self.line_start is not None:
            loc += f":{self.line_start}"
            if self.line_end and self.line_end != self.line_start:
                loc += f"-{self.line_end}"
        return f"{loc}@{self.revision}"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": self.path,
            "revision": self.revision,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "detail": self.detail,
        }


def require_evidence(evidence: list[Evidence], what: str) -> None:
    """Reject unevidenced factual claims at construction time."""

    if not evidence:
        raise ValueError(f"{what} must carry at least one evidence pointer")


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "+", "-" or " "
    old_line: int | None
    new_line: int | None
    text: str


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True)
class FileDiff:
    path: str
    old_path: str | None
    status: str  # added | deleted | modified | renamed
    hunks: tuple[Hunk, ...]

    def added_lines(self) -> set[int]:
        return {
            line.new_line
            for hunk in self.hunks
            for line in hunk.lines
            if line.kind == "+" and line.new_line is not None
        }

    def deleted_lines(self) -> set[int]:
        return {
            line.old_line
            for hunk in self.hunks
            for line in hunk.lines
            if line.kind == "-" and line.old_line is not None
        }

    def new_lines(self) -> set[int]:
        return {
            line.new_line
            for hunk in self.hunks
            for line in hunk.lines
            if line.new_line is not None
        }

    def old_lines(self) -> set[int]:
        return {
            line.old_line
            for hunk in self.hunks
            for line in hunk.lines
            if line.old_line is not None
        }


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # positional | keyword_only | var_positional | var_keyword
    annotation: str | None = None
    default: str | None = None

    @property
    def required(self) -> bool:
        return self.default is None and self.kind in ("positional", "posonly", "keyword_only")


@dataclass(frozen=True)
class Signature:
    params: tuple[Param, ...]
    returns: str | None = None
    is_async: bool = False
    decorators: tuple[str, ...] = ()

    @property
    def accepts_keywords(self) -> bool:
        return any(p.kind == "var_keyword" for p in self.params)

    @property
    def accepts_positional(self) -> bool:
        return any(p.kind == "var_positional" for p in self.params)

    def required_positional(self) -> int:
        return sum(1 for p in self.params if p.required and p.kind in ("positional", "posonly"))

    def max_positional(self) -> int | None:
        if self.accepts_positional:
            return None
        return sum(1 for p in self.params if p.kind in ("positional", "posonly"))

    def accepted_keywords(self) -> set[str] | None:
        """Keyword names accepted by the signature; ``None`` means ``**kwargs``."""

        if self.accepts_keywords:
            return None
        return {
            p.name
            for p in self.params
            if p.kind in ("positional", "keyword_only", "posonly") and p.name
        }

    def render(self, name: str | None = None) -> str:
        parts = []
        for param in self.params:
            text = param.name
            if param.kind == "var_positional":
                text = "*" + text
            elif param.kind == "var_keyword":
                text = "**" + text
            if param.annotation:
                text += f": {param.annotation}"
            if param.default is not None:
                text += f" = {param.default}"
            parts.append(text)
        prefix = "async " if self.is_async else ""
        returns = f" -> {self.returns}" if self.returns else ""
        label = f" {name}" if name else ""
        return f"{prefix}def{label}({', '.join(parts)}){returns}"

    def as_dict(self) -> dict[str, object]:
        return {
            "params": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "annotation": p.annotation,
                    "default": p.default,
                }
                for p in self.params
            ],
            "returns": self.returns,
            "is_async": self.is_async,
            "decorators": list(self.decorators),
        }


@dataclass(frozen=True)
class FunctionInfo:
    path: str
    qualname: str
    name: str
    signature: Signature
    start_line: int
    end_line: int
    language: str
    class_name: str | None = None
    is_public: bool = True

    @property
    def symbol(self) -> str:
        return f"{self.path}::{self.qualname}"


@dataclass(frozen=True)
class RouteInfo:
    method: str
    path: str
    handler: str  # module::qualname
    status_code: int | None
    line: int
    file: str

    @property
    def route_id(self) -> str:
        return f"{self.method.upper()} {self.path}"


@dataclass(frozen=True)
class ImportRef:
    local: str
    module: str
    imported: str | None
    line: int
    relative: bool = False


@dataclass(frozen=True)
class CallSite:
    callee: str
    name: str
    receiver: str | None
    line: int
    in_symbol: str
    arg_count: int | None
    kwargs: tuple[str, ...] = ()
    has_star_args: bool = False


@dataclass
class ModuleInfo:
    path: str
    language: str
    functions: dict[str, FunctionInfo] = field(default_factory=dict)
    routes: list[RouteInfo] = field(default_factory=list)
    imports: dict[str, ImportRef] = field(default_factory=dict)
    calls: list[CallSite] = field(default_factory=list)
    classes: set[str] = field(default_factory=set)
    parse_error: str | None = None


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str | None
    resolution: Resolution
    path: str
    line: int
    detail: str


@dataclass(frozen=True)
class ConsumerHit:
    symbol: str
    path: str
    line: int
    depth: int
    resolution: Resolution
    revision: str
    impact: str = "unknown"  # broken | compatible | unknown
    detail: str = ""


@dataclass
class Change:
    kind: ChangeKind
    symbol: str
    summary: str
    classification: Classification
    confidence: Confidence
    evidence: list[Evidence]
    file: str
    base_signature: Signature | None = None
    head_signature: Signature | None = None
    route_before: RouteInfo | None = None
    route_after: RouteInfo | None = None
    consumers: list[ConsumerHit] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    rationale: str = ""

    def __post_init__(self) -> None:
        require_evidence(self.evidence, f"change {self.kind.value} {self.symbol}")

    @property
    def downstream_count(self) -> int:
        return len({c.symbol for c in self.consumers})


@dataclass
class DocDrift:
    doc_path: str
    doc_line: int
    snippet: str
    reason: str
    evidence: list[Evidence]

    def __post_init__(self) -> None:
        require_evidence(self.evidence, f"doc drift in {self.doc_path}:{self.doc_line}")


@dataclass
class CoverageFile:
    path: str
    added_lines: tuple[int, ...]
    uncovered_added: tuple[int, ...]
    covered_added: int
    missing_lines: tuple[int, ...] = ()
    newly_uncovered: tuple[int, ...] = ()


@dataclass
class CoverageReport:
    available: bool
    base_rev: str
    head_rev: str
    files: list[CoverageFile] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    command: str = ""
    tests_passed: bool | None = None
    base_percent: float | None = None
    head_percent: float | None = None
    base_tests_passed: bool | None = None

    @property
    def uncovered_added_lines(self) -> int:
        return sum(len(f.uncovered_added) for f in self.files)

    @property
    def newly_uncovered_lines(self) -> int:
        return sum(len(f.newly_uncovered) for f in self.files)


@dataclass(frozen=True)
class RiskFactor:
    name: str
    raw: int
    cap: int
    weight: float
    contribution: float
    explanation: str

    @property
    def capped(self) -> int:
        return min(self.raw, self.cap)


@dataclass
class RiskScore:
    total: int
    raw_total: float
    band: str
    factors: list[RiskFactor]
    formula: str


@dataclass
class TestSkeleton:
    path: str
    content: str
    target_symbol: str
    evidence: list[Evidence]


@dataclass
class Dossier:
    repo: str
    base_rev: str
    head_rev: str
    base_sha: str
    head_sha: str
    changes: list[Change]
    coverage: CoverageReport
    doc_drift: list[DocDrift]
    risk: RiskScore
    skeletons: list[TestSkeleton]
    rollback_steps: list[str]
    limitations: list[str]
    files_changed: int
    supported_files_changed: int
    total_consumers: int

    @property
    def breaking_changes(self) -> list[Change]:
        return [c for c in self.changes if c.classification is Classification.BREAKING]
# PR Impact Simulator

Analyzes a git repository and produces an **impact dossier** for a pull request
before merge: what breaks, who calls it, what the tests miss, and what the docs
still claim. It is not a style reviewer and it does not guess.

Every factual claim carries an evidence pointer (`path:line@revision`). If the
analyser cannot establish a claim, the claim is omitted and the reason appears
under **Limitations** in the dossier.

## What it does

- Detects public API changes: function signatures (added/removed/renamed
  parameters, parameter order, kinds, defaults, annotations, return type) and
  FastAPI-style route declarations (method, path, `status_code`).
- Builds a static call graph and traces consumers of each changed symbol to a
  configurable depth, tagging each call site `broken`, `compatible` or
  `unknown` by checking its argument shape against the head signature.
- Runs coverage on both base and head in throwaway `git worktree` checkouts with
  the same interpreter and command, then correlates **only the lines the PR
  added** with executed lines.
- Detects documentation drift: README code examples that no longer match the
  head signature, and documented endpoints that disappeared.
- Computes a factor-based risk score with every factor, cap, weight and
  contribution printed.
- Emits `dossier.md`, `rollback.md`, `dossier.json`, and incomplete pytest
  skeletons for uncovered changed surfaces.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"     # adds pytest, coverage, mypy, ruff
pip install tree-sitter-javascript tree-sitter-typescript   # optional, for TS/JS
```

## Run the demo

```bash
python demo/run_demo.py            # builds fixtures, analyses, prints dossiers
python demo/run_demo.py --summary  # only the pass/fail summary
```

The demo builds `examples/.build/target_repo` from scratch, with a `main`
branch and three seeded PR branches, then analyses each one:

| Branch | Seeded defect | Measured result |
| --- | --- | --- |
| `pr/signature-break` | `create_user` gains a required `tenant_id` | 1 breaking change, 3 broken consumers, risk 29 MODERATE |
| `pr/untested-endpoint` | new refund route and service with no tests | 9 uncovered added lines, base 95.31% → head 90.28%, risk 9 LOW, 2 skeletons |
| `pr/docs-drift` | `amount` renamed to `amount_cents`, route moved | 2 breaking changes, 2 docs drift items, risk 46 HIGH |

The seeded expectations are asserted by the demo, so it exits non-zero if the
analyser regresses.

## CLI

```bash
pr-impact . --base main --head HEAD            # artifacts in .pr-impact/
pr-impact . --head feature/x --print-comment   # print the would-be PR comment
pr-impact . --fail-on-breaking                 # CI gate; exit 1 on breaking surface
python -m pr_impact . --no-coverage --out /tmp/report
```

| Flag | Purpose |
| --- | --- |
| `--base`, `--head` | revisions to compare (branches, tags or SHAs) |
| `--out` | artifact directory |
| `--depth` | consumer trace depth (default 3) |
| `--no-coverage` | skip coverage runs |
| `--no-llm` | disable LLM enrichment |
| `--print-comment` | write the markdown dossier to stdout |
| `--fail-on-breaking` | non-zero exit when breaking changes exist |

## Architecture

```
pr_impact/
  gitio.py      git plumbing + unified-diff parsing (typed hunks)
  parsing.py    tree-sitter extraction of signatures, routes, imports, calls
  graph.py      call graph with resolved / ambiguous / unresolved / external edges
  detectors/    api.py (signatures, routes, call-shape checks), docs.py (drift)
  cov.py        worktree coverage runs + diff correlation
  risk.py       transparent factor scoring
  llm.py        optional escalation-only semantic layer
  emit/         dossier.md, rollback.md, JSON, pytest skeletons
  pipeline.py   orchestration; CLI is a thin wrapper
tests/          72 tests
examples/       fixture builder for the seeded repository
```

The layers share typed records from `model.py`. `Change` and `DocDrift` refuse
to construct without evidence, so an unevidenced claim cannot reach the output.

### Edge resolution

Every call edge records how it was resolved:

| State | Meaning | Effect on counts |
| --- | --- | --- |
| `resolved` | import chain or local definition proves the target | counted |
| `ambiguous` | receiver type unknown (e.g. `obj.save()`) | not counted, listed |
| `unresolved` | import exists but no matching function at that revision | not counted, listed |
| `external` | builtin, third-party module, or class instantiation | out of scope |

Ambiguous and unresolved edges are reported as limitations with example
locations, so "zero consumers" is never confused with "not traced".

## Risk score

```
risk = min(100,
    10 x min(breaking_changes, 5) +
     3 x min(broken_consumers, 20) +
     1 x min(uncovered_added_lines, 40) +
     3 x min(docs_drift, 5) +
     5 x min(migration_points, 5))
```

`migration_points` counts breaking changes by kind: removed function/route 3,
changed route/signature 2, changed status code 1. Bands: `<20` LOW, `<40`
MODERATE, `<65` HIGH, else CRITICAL. Every factor is printed with its raw value,
cap, weight, contribution and a one-line explanation, followed by the
substituted arithmetic, so the number can be recomputed by hand.

## Optional LLM layer

Disabled unless `PR_IMPACT_LLM_URL` is set (OpenAI-compatible
`/chat/completions`; optional `PR_IMPACT_LLM_MODEL`, `PR_IMPACT_LLM_KEY`). The
LLM receives only facts the static layer already established, and may:

- **escalate** a change to `needs-human` with a stated uncertainty, or
- **agree** and supply prose.

It may not soften a breaking verdict: a contradicting verdict is ignored and
recorded as a limitation. Its text never introduces paths, line numbers, call
relationships or test results. The deterministic demo runs with it off.

## Implementation trade-offs

- **Static first, LLM last.** Facts come from tree-sitter; the LLM can only
  annotate or escalate. This keeps the dossier reproducible and auditable, at
  the cost of missing semantics the parser cannot see (dynamic dispatch,
  framework magic).
- **Diff-scoped change detection.** Only public functions in files touched by
  the PR are compared, so an unrelated refactor elsewhere does not flood the
  report. Renames are reported as removal + addition, which is noisier but never
  silently wrong.
- **Coverage correlation is line-based.** Only lines added by the PR are
  attributed. A PR that deletes a covered line and shifts an uncovered one is
  not credited with a regression, because line numbers move between revisions.
- **Consumers are bounded by depth.** The default depth of 3 catches
  service → client → route chains; deeper chains are cut and reported only by
  the depth limit, not invented.
- **Worktree isolation.** Coverage runs in a temporary detached worktree with
  `COVERAGE_FILE` redirected and `PYTHONPATH` cleared, so the analysed repo's
  index, branches and coverage files are untouched.
- **Artifacts are never written into the analysed repository unless you ask.**
  The default output is `.pr-impact/` in the current directory; the analyser
  does not push commits or comment on PRs in local mode.

## Known failure modes and unsupported cases

- **Python is the supported language.** TypeScript/JavaScript get top-level
  functions, arrows, class methods, imports and calls; decorators, overloads,
  generics and dynamic dispatch are not modelled, and the dossier says so when
  TS/JS files change.
- **No type inference.** `obj.method()` is `ambiguous` unless the receiver is an
  imported module, `self`, or `cls`. Interface-typed receivers stay unresolved.
- **FastAPI detection is decorator-shape based.** `@router.get(...)`-style
  decorators are recognised, including `status_code`; APIRouter composition,
  `include_router` prefixes and dependency injection are not resolved, so paths
  are reported exactly as written at the decorator.
- **Re-exports and `__all__` are not tracked.** A symbol exported through a
  package `__init__` is traced only if the consumer imports from the defining
  module.
- **Coverage requires a green-or-runnable pytest suite.** Missing pytest, a
  collection error, or a timeout produces an explicit limitation and no
  uncovered-line count. Outcomes that differ between base and head are flagged
  as "not under equivalent conditions".
- **Partial parses are possible.** Files that tree-sitter cannot fully parse are
  still analysed, and the dossier states that their facts are incomplete.
- **No GitHub write path in local mode.** Local analysis is the source of truth;
  the CLI never comments on a PR.

## GitHub integration

`.github/workflows/pr-impact.yml` runs the analyser on every pull request and
always uploads `.pr-impact/` plus `comment.md` as an artifact, and writes a
short summary to the job summary.

Posting the dossier as a PR comment is **opt-in**:

1. set the repository variable `PR_IMPACT_COMMENT` to `true`;
2. the commenting job then requests `pull-requests: write`, which is granted
   only to that job and only when the variable is set.

The write permission is therefore never present in the default path, and the
analyser itself performs no network calls - the workflow shells out to `gh`
with the token it was given. Contributors are not notified automatically unless
the repository owner enables the variable.

## Tests

```bash
python -m pytest -q
```

`mypy pr_impact` is clean and `ruff check` passes. 72 tests cover diff parsing (including new/deleted/renamed files and hunk line
numbering), signature extraction, route detection, graph resolution states,
transitive consumer tracing, call-shape mismatch, docs drift, coverage
correlation, risk arithmetic and caps, LLM escalation rules, artifact
rendering, the CLI contract, and end-to-end determinism.

## Evidence model

| Evidence kind | Meaning |
| --- | --- |
| `source` | definition at a revision, with line range |
| `route` | decorator line declaring the route and status |
| `diff` | hunks that produced the finding |
| `docs` | line in a markdown document |

A pointer renders as `path:line@revision`, for example
`sample_app/services.py:8-9@main`. Revisions are the git refs passed on the
command line, so a reviewer can open the same content with `git show`.

## License

No license file is included; add one before distributing.

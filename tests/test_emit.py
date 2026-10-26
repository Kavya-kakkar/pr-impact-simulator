from __future__ import annotations

import json
from dataclasses import replace

from pr_impact.emit.dossier import dossier_to_dict, render_dossier
from pr_impact.emit.rollback import rollback_steps
from pr_impact.model import TestSkeleton as Skeleton
from pr_impact.pipeline import write_artifacts


def test_dossier_is_ascii_markdown_with_evidence(dossier_signature_break):
    text = render_dossier(dossier_signature_break)
    text.encode("ascii")  # must not require a special console code page
    assert "# PR Impact Dossier" in text
    assert "Verdict: MODERATE - 29/100" in text
    assert "| breaking_changes |" in text
    assert "risk = min(100," in text
    assert "sample_app/services.py:8-9@main" in text
    assert "| `sample_app/clients.py::create_owner`" in text
    assert "missing required argument(s) at head: tenant_id" in text
    assert "No claim is included without a file:line pointer." in text


def test_json_export_is_plain_data(dossier_signature_break):
    payload = dossier_to_dict(dossier_signature_break)
    encoded = json.dumps(payload)
    reloaded = json.loads(encoded)
    assert reloaded["risk"]["total"] == 29
    assert reloaded["changes"][0]["classification"] == "breaking"
    assert reloaded["changes"][0]["evidence"][0]["revision"] == "main"
    assert isinstance(reloaded["coverage"]["available"], bool)


def test_write_artifacts_includes_dossier_rollback_json(tmp_path, dossier_signature_break):
    paths = write_artifacts(dossier_signature_break, tmp_path)
    assert paths["dossier"].exists()
    assert paths["rollback"].exists()
    assert paths["json"].exists()
    rollback = paths["rollback"].read_text(encoding="utf-8")
    assert "# Rollback checklist" in rollback
    assert "create_user" in rollback
    assert "sample_app/services.py:8-9@main" in rollback


def test_write_artifacts_writes_skeletons(tmp_path, dossier_signature_break):
    skeleton = Skeleton(
        path=".pr-impact/tests/test_demo_contract.py",
        content='"""skeleton"""\n',
        target_symbol="pkg/mod.py::demo",
        evidence=[],
    )
    dossier = replace(dossier_signature_break, skeletons=[skeleton])
    paths = write_artifacts(dossier, tmp_path)
    destination = tmp_path / "tests" / "test_demo_contract.py"
    assert destination.exists()
    assert paths[skeleton.path] == destination


def test_rollback_steps_cover_routes_and_coverage(dossier_docs_drift):
    steps = dossier_docs_drift.rollback_steps
    text = "\n".join(steps)
    assert "Restore route `GET /discounts/{code}`" in text
    assert "calculate_discount" in text
    assert "documentation" in text
    assert "re-run this analyser" in text


def test_rollback_without_breaking_changes(dossier_untested):
    text = "\n".join(dossier_untested.rollback_steps)
    assert "No breaking surface was detected" in text
    assert "Coverage was unavailable" in text


def test_rollback_function_is_pure(dossier_untested):
    steps = rollback_steps(
        dossier_untested.changes,
        dossier_untested.coverage,
        dossier_untested.doc_drift,
        dossier_untested.head_rev,
    )
    assert steps == dossier_untested.rollback_steps
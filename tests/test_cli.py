from __future__ import annotations

from pathlib import Path

from pr_impact.cli import main


def test_cli_writes_artifacts(fixture_repo, tmp_path: Path, capsys):
    out = tmp_path / "artifacts"
    code = main(
        [
            str(fixture_repo.repo),
            "--base",
            "main",
            "--head",
            "pr/signature-break",
            "--no-coverage",
            "--no-llm",
            "--out",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "MODERATE 29/100" in captured.out
    assert "dossier:" in captured.out
    assert (out / "dossier.md").exists()
    assert (out / "rollback.md").exists()
    assert (out / "dossier.json").exists()


def test_cli_print_comment(fixture_repo, tmp_path: Path, capsys):
    code = main(
        [
            str(fixture_repo.repo),
            "--head",
            "pr/docs-drift",
            "--no-coverage",
            "--no-llm",
            "--out",
            str(tmp_path / "artifacts"),
            "--print-comment",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "# PR Impact Dossier" in captured.out
    assert "## Documentation drift" in captured.out
    assert "README.md:10" in captured.out


def test_cli_fail_on_breaking(fixture_repo, tmp_path: Path):
    code = main(
        [
            str(fixture_repo.repo),
            "--head",
            "pr/signature-break",
            "--no-coverage",
            "--no-llm",
            "--out",
            str(tmp_path / "artifacts"),
            "--fail-on-breaking",
        ]
    )
    assert code == 1


def test_cli_non_breaking_exits_zero(fixture_repo, tmp_path: Path):
    code = main(
        [
            str(fixture_repo.repo),
            "--head",
            "pr/untested-endpoint",
            "--no-coverage",
            "--no-llm",
            "--out",
            str(tmp_path / "artifacts"),
            "--fail-on-breaking",
        ]
    )
    assert code == 0


def test_cli_bad_repository(tmp_path: Path, capsys):
    code = main([str(tmp_path / "nope"), "--no-coverage"])
    captured = capsys.readouterr()
    assert code == 2
    assert "error:" in captured.err
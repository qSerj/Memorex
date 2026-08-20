from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from memorex.cli import app

runner = CliRunner()


def test_cli_init_add_and_show_json(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    source = tmp_path / "source.md"
    source.write_text("# Note\n\nA durable fact.", encoding="utf-8")

    initialized = runner.invoke(app, ["--data-dir", str(data_dir), "init", "--json"])
    added = runner.invoke(app, ["--data-dir", str(data_dir), "add", str(source), "--json"])
    shown = runner.invoke(app, ["--data-dir", str(data_dir), "source", "show", "1", "--json"])

    assert initialized.exit_code == 0
    assert json.loads(initialized.stdout)["fts5"] is True
    assert added.exit_code == 0
    assert json.loads(added.stdout)["status"] == "added"
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["versions"][0]["segment_count"] == 1


def test_cli_requires_explicit_init(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("A fact.", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--data-dir", str(tmp_path / "missing"), "add", str(source)],
    )

    assert result.exit_code == 1
    assert "run 'memorex init'" in result.stderr

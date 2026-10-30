"""The one-off migration CLI: dry-run, failures, and the successful switch."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from app.core import db

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "migrate_data_dir.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("migrate_data_dir_tool", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_home(tmp_path: Path) -> Path:
    from tests.test_datamove import _make_home as make_home

    return make_home(tmp_path)


def _run(monkeypatch, *args: str) -> int:
    tool = _load_tool()
    monkeypatch.setattr(sys, "argv", ["migrate_data_dir.py", *args])
    return tool.main()


def test_dry_run_reports_without_writing(tmp_path, monkeypatch, capsys) -> None:
    home = _make_home(tmp_path)
    before = (home / "config.json").read_bytes()
    target = tmp_path / "InvoiceDeskData"

    code = _run(
        monkeypatch,
        "--dry-run",
        "--source-home",
        str(home),
        "--target",
        str(target),
    )

    assert code == 0
    assert not target.exists()
    assert (home / "config.json").read_bytes() == before
    output = capsys.readouterr().out
    assert "dry run" in output
    assert '"invoices": 2' in output


def test_missing_source_returns_an_error(tmp_path, monkeypatch, capsys) -> None:
    target = tmp_path / "InvoiceDeskData"
    code = _run(
        monkeypatch,
        "--source-home",
        str(tmp_path / "missing"),
        "--target",
        str(target),
    )
    assert code == 1
    assert not target.exists()
    assert "error:" in capsys.readouterr().out


def test_migration_updates_the_pointer_and_keeps_the_flag(
    tmp_path, monkeypatch, capsys
) -> None:
    home = _make_home(tmp_path)
    target = tmp_path / "InvoiceDeskData"

    code = _run(
        monkeypatch,
        "--source-home",
        str(home),
        "--target",
        str(target),
    )

    assert code == 0
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert Path(config["data_dir"]) == target.resolve()
    assert config["welcome_done"] is True
    assert (target / "invoicedesk.db").is_file()
    assert "moved:" in capsys.readouterr().out


def test_different_target_database_fails_without_touching_config(
    tmp_path, monkeypatch, capsys
) -> None:
    home = _make_home(tmp_path)
    before = (home / "config.json").read_bytes()
    target = tmp_path / "InvoiceDeskData"
    target.mkdir()
    db.connect(target / "invoicedesk.db").close()

    code = _run(
        monkeypatch,
        "--source-home",
        str(home),
        "--target",
        str(target),
    )

    assert code == 1
    assert (home / "config.json").read_bytes() == before
    assert "不同的发票库" in capsys.readouterr().out

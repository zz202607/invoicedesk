from __future__ import annotations

from pathlib import Path

import pytest

from app.cli import _is_unsafe_repo_output, main
from tests.pdf_factory import full_digital_lines, make_pdf

ROOT = Path(__file__).resolve().parents[1]


def write_invoice(folder: Path, name: str = "a.pdf") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return make_pdf(folder, full_digital_lines(), name=name)


def test_import_then_stats(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "2026年7月"
    write_invoice(folder)
    database = tmp_path / "invoices.db"

    assert main(["import", str(folder), "--db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "批次 #1「2026年7月」已导入" in output
    assert "发票 1" in output
    assert "冲突 0" in output
    assert "合计 1,060.00 元" in output
    assert database.exists()

    assert main(["stats", "--db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "批次 #1「2026年7月」" in output
    assert "发票 1" in output
    assert "合计: 批次 1" in output


def test_reimport_reports_no_new_files(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "july"
    write_invoice(folder)
    database = tmp_path / "invoices.db"

    assert main(["import", str(folder), "--db", str(database)]) == 0
    capsys.readouterr()
    assert main(["import", str(folder), "--db", str(database)]) == 0
    assert "没有新文件" in capsys.readouterr().out


def test_db_inside_repo_is_refused(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "july"
    write_invoice(folder)
    database = ROOT / "_test_refused.db"
    database.unlink(missing_ok=True)

    try:
        assert main(["import", str(folder), "--db", str(database)]) == 2
        assert not database.exists()
        assert "数据库路径在仓库内" in capsys.readouterr().err
    finally:
        database.unlink(missing_ok=True)


def test_db_guard_allows_ignored_directories(tmp_path: Path) -> None:
    assert _is_unsafe_repo_output(ROOT / "data" / "invoicedesk.db") is False
    assert _is_unsafe_repo_output(ROOT / "out" / "invoices.db") is False
    assert _is_unsafe_repo_output(ROOT / "invoices.db") is True
    assert _is_unsafe_repo_output(tmp_path / "invoices.db") is False


def test_import_empty_folder_returns_1(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    database = tmp_path / "invoices.db"

    assert main(["import", str(folder), "--db", str(database)]) == 1
    assert "没有找到 PDF" in capsys.readouterr().out


def test_import_missing_path_returns_2(tmp_path: Path, capsys) -> None:
    database = tmp_path / "invoices.db"
    assert main(["import", str(tmp_path / "missing"), "--db", str(database)]) == 2
    assert "路径不存在" in capsys.readouterr().err


def test_stats_missing_database_returns_1(tmp_path: Path, capsys) -> None:
    database = tmp_path / "missing.db"
    assert main(["stats", "--db", str(database)]) == 1
    assert "数据库不存在" in capsys.readouterr().err
    assert not database.exists()


def test_damaged_pdf_is_imported_for_review(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "july"
    write_invoice(folder)
    (folder / "bad.pdf").write_bytes(b"not a real pdf")
    database = tmp_path / "invoices.db"

    assert main(["import", str(folder), "--db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "发票 2" in output
    assert "待确认 1" in output

    assert main(["stats", "--db", str(database)]) == 0
    assert "发票 2" in capsys.readouterr().out


def test_unexpected_import_error_is_reported_cleanly(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    folder = tmp_path / "july"
    write_invoice(folder)
    database = tmp_path / "invoices.db"

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.cli.import_folder", boom)
    assert main(["import", str(folder), "--db", str(database)]) == 2
    error = capsys.readouterr().err
    assert "导入失败（RuntimeError）" in error
    assert "Traceback" not in error
    assert "boom" not in error


def test_os_error_does_not_echo_paths(tmp_path: Path, capsys, monkeypatch) -> None:
    folder = tmp_path / "july"
    write_invoice(folder)
    database = tmp_path / "invoices.db"

    def boom(*args, **kwargs):
        raise OSError(r"C:\secret\invoice.pdf")

    monkeypatch.setattr("app.cli.import_folder", boom)
    assert main(["import", str(folder), "--db", str(database)]) == 2
    error = capsys.readouterr().err
    assert "导入失败" in error
    assert "secret" not in error


def test_unexpected_import_error_reraises_with_debug(
    tmp_path: Path, monkeypatch
) -> None:
    folder = tmp_path / "july"
    write_invoice(folder)
    database = tmp_path / "invoices.db"

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.cli.import_folder", boom)
    with pytest.raises(RuntimeError):
        main(["import", str(folder), "--db", str(database), "--debug"])


def test_import_label_override(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "july"
    write_invoice(folder)
    database = tmp_path / "invoices.db"

    assert main(["import", str(folder), "--db", str(database), "--label", "7月示例公司"]) == 0
    assert "7月示例公司" in capsys.readouterr().out

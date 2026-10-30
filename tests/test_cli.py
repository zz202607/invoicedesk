from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest

from app.cli import _is_unsafe_repo_output, main, parse_folder, print_report, scan_pdfs
from app.core.models import InvoiceType, ParsedInvoice
from tests.pdf_factory import full_digital_lines, make_pdf

ROOT = Path(__file__).resolve().parents[1]


def blank_pdf(path: Path) -> None:
    document = fitz.open()
    document.new_page(width=300, height=200)
    document.save(path)
    document.close()


def test_scan_pdfs_skips_output_dirs_and_accepts_uppercase(tmp_path: Path) -> None:
    blank_pdf(tmp_path / "a.PDF")
    (tmp_path / "sub").mkdir()
    blank_pdf(tmp_path / "sub" / "b.pdf")
    (tmp_path / "分类合并结果").mkdir()
    blank_pdf(tmp_path / "分类合并结果" / "c.pdf")
    (tmp_path / "out").mkdir()
    blank_pdf(tmp_path / "out" / "d.pdf")
    (tmp_path / "InvoiceDeskData" / "backups").mkdir(parents=True)
    blank_pdf(tmp_path / "InvoiceDeskData" / "backups" / "e.pdf")
    (tmp_path / "报销发票整理").mkdir()
    blank_pdf(tmp_path / "报销发票整理" / "f.pdf")
    (tmp_path / "note.txt").write_text("not a pdf", encoding="utf-8")

    names = [path.name for path in scan_pdfs(tmp_path)]
    assert names == ["a.PDF", "b.pdf"]


def test_scan_pdfs_does_not_skip_parent_named_out(tmp_path: Path) -> None:
    root = tmp_path / "out" / "july"
    root.mkdir(parents=True)
    blank_pdf(root / "a.pdf")
    assert [path.name for path in scan_pdfs(root)] == ["a.pdf"]


def test_parse_folder_limit(tmp_path: Path) -> None:
    blank_pdf(tmp_path / "a.pdf")
    blank_pdf(tmp_path / "b.pdf")
    assert len(parse_folder(tmp_path, limit=1)) == 1
    assert len(parse_folder(tmp_path)) == 2


def test_main_missing_path_returns_2(tmp_path: Path, capsys) -> None:
    assert main(["parse", str(tmp_path / "missing")]) == 2
    assert "路径不存在" in capsys.readouterr().err


def test_main_empty_dir_returns_1(tmp_path: Path, capsys) -> None:
    assert main(["parse", str(tmp_path)]) == 1
    assert "没有找到 PDF" in capsys.readouterr().out


def test_main_writes_json(tmp_path: Path, capsys) -> None:
    pdf = tmp_path / "scan.pdf"
    blank_pdf(pdf)
    output = tmp_path / "out.json"
    assert main(["parse", str(pdf), "--json", str(output)]) == 0
    records = json.loads(output.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["invoice_type"] == "scanned"
    assert records[0]["needs_review"] is True
    assert "confidence" in records[0]
    assert records[0]["source_name"] == "scan.pdf"
    assert "source_path" not in records[0]
    output = capsys.readouterr().out
    assert "共 1 条" in output
    assert "待确认 1 条" in output


def test_main_absolute_paths_flag(tmp_path: Path, capsys) -> None:
    pdf = tmp_path / "scan.pdf"
    blank_pdf(pdf)
    output = tmp_path / "out.json"
    assert main(["parse", str(pdf), "--json", str(output), "--absolute-paths"]) == 0
    records = json.loads(output.read_text(encoding="utf-8"))
    assert "source_path" in records[0]
    assert str(pdf) in records[0]["source_path"]
    assert "警告" in capsys.readouterr().err


def test_unsafe_repo_output_detection(tmp_path: Path) -> None:
    assert _is_unsafe_repo_output(ROOT / "out.json") is True
    assert _is_unsafe_repo_output(ROOT / "out" / "parsed.json") is False
    assert _is_unsafe_repo_output(ROOT / "tests" / "local" / "parsed.json") is False
    assert _is_unsafe_repo_output(tmp_path / "out.json") is False


def test_json_inside_repo_is_refused(tmp_path: Path, capsys) -> None:
    pdf = tmp_path / "scan.pdf"
    blank_pdf(pdf)
    output = ROOT / "_test_refused.json"
    output.unlink(missing_ok=True)
    assert main(["parse", str(pdf), "--json", str(output)]) == 2
    assert not output.exists()
    assert "请写到 out/" in capsys.readouterr().err


def test_force_allows_repo_output(tmp_path: Path) -> None:
    pdf = tmp_path / "scan.pdf"
    blank_pdf(pdf)
    output = ROOT / "_test_force.json"
    try:
        assert main(["parse", str(pdf), "--json", str(output), "--force"]) == 0
        assert output.exists()
    finally:
        output.unlink(missing_ok=True)


def test_print_report_sanitizes_untrusted_filename(capsys) -> None:
    invoice = ParsedInvoice(
        source_path="bad\x1b[2J.pdf",
        invoice_type=InvoiceType.SCANNED,
        warnings=["bad\x1b[2J warning"],
    )
    print_report([invoice], show_detail=True)
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "bad[2J.pdf" in output


def test_main_limit_applies_to_json(tmp_path: Path) -> None:
    blank_pdf(tmp_path / "a.pdf")
    blank_pdf(tmp_path / "b.pdf")
    output = tmp_path / "out.json"
    assert main(["parse", str(tmp_path), "--limit", "1", "--json", str(output)]) == 0
    assert len(json.loads(output.read_text(encoding="utf-8"))) == 1


def test_limit_zero_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["parse", str(tmp_path), "--limit", "0"])
    assert excinfo.value.code == 2


def test_limit_larger_than_file_count(tmp_path: Path) -> None:
    blank_pdf(tmp_path / "a.pdf")
    blank_pdf(tmp_path / "b.pdf")
    output = tmp_path / "out.json"
    assert main(["parse", str(tmp_path), "--limit", "10", "--json", str(output)]) == 0
    assert len(json.loads(output.read_text(encoding="utf-8"))) == 2


def test_json_write_failure_returns_2(tmp_path: Path, capsys) -> None:
    blank_pdf(tmp_path / "a.pdf")
    output = tmp_path / "missing" / "out.json"
    assert main(["parse", str(tmp_path), "--json", str(output)]) == 2
    assert "无法写入" in capsys.readouterr().err


def test_verbose_shows_detail(tmp_path: Path, capsys) -> None:
    pdf = tmp_path / "scan.pdf"
    blank_pdf(pdf)
    assert main(["parse", str(pdf), "--verbose"]) == 0
    captured = capsys.readouterr()
    assert "scan.pdf" in captured.out
    assert "待确认:" in captured.out
    assert "警告: 明细输出" in captured.err


def test_cli_masks_tax_ids_by_default(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path, full_digital_lines(), name="invoice.pdf")
    default_json = tmp_path / "default.json"
    assert main(["parse", str(pdf), "--json", str(default_json)]) == 0
    record = json.loads(default_json.read_text(encoding="utf-8"))[0]
    assert record["seller_tax_id"] != "92500000TEST00002X"
    assert record["seller_tax_id"].startswith("9250")
    assert record["seller_tax_id"].endswith("002X")

    full_json = tmp_path / "full.json"
    assert main(["parse", str(pdf), "--json", str(full_json), "--include-tax-ids"]) == 0
    full_record = json.loads(full_json.read_text(encoding="utf-8"))[0]
    assert full_record["seller_tax_id"] == "92500000TEST00002X"

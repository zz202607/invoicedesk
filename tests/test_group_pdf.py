"""Clean merged PDFs grouped by buyer company and category."""

from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest

from app.core.export import ExportError, ExportRow
from app.core.group_pdf import (
    A4_HEIGHT,
    A4_WIDTH,
    MANIFEST_NAME,
    UNKNOWN_CATEGORY,
    UNKNOWN_COMPANY,
    write_group_pdfs,
)
from app.core.models import InvoiceType
from tests.pdf_factory import make_pdf, make_pdf_pages


def make_row(source: Path, **overrides) -> ExportRow:
    values = {
        "id": 1,
        "batch_id": 1,
        "source_path": str(source),
        "page_index": 0,
        "page_count": 1,
        "invoice_type": InvoiceType.FULL_DIGITAL_NORMAL.value,
        "invoice_number": "26502000000000000001",
        "invoice_date": "2026-10-10",
        "seller_name": "示例五金店",
        "buyer_name": "示例甲公司",
        "amount_net_cents": 100,
        "tax_amount_cents": 6,
        "amount_total_cents": 106,
        "item_summary": "五金配件",
        "category": "其他费用",
        "confidence": {},
        "warnings": (),
        "needs_review": False,
    }
    values.update(overrides)
    return ExportRow(**values)


def page_texts(path: Path) -> list[str]:
    with fitz.open(path) as document:
        return [page.get_text().strip() for page in document]


def test_groups_by_company_and_category_without_labels(tmp_path: Path) -> None:
    first = make_pdf(tmp_path, [(40, 40, "甲票内容")], name="a.pdf")
    second = make_pdf(tmp_path, [(40, 40, "乙票内容")], name="b.pdf")
    third = make_pdf(tmp_path, [(40, 40, "丙票内容")], name="c.pdf")
    rows = [
        make_row(first, id=1, category="差旅费"),
        make_row(second, id=2, category="差旅费"),
        make_row(
            third,
            id=3,
            buyer_name="示例乙公司",
            category="餐饮费",
        ),
    ]

    result = write_group_pdfs(tmp_path / "out", rows)

    assert len(result.groups) == 2
    by_key = {(group.company, group.category): group for group in result.groups}
    example_a = by_key[("示例甲公司", "差旅费")]
    assert example_a.count == 2
    assert example_a.source_pages == 2
    assert example_a.pages == 1
    company_dir = Path(example_a.path).parent
    assert company_dir.name == "示例甲公司"
    assert Path(example_a.path).name == "差旅费.pdf"
    # Two invoice pages share one A4 sheet; no label, sequence, or watermark
    # text is added anywhere.
    assert page_texts(Path(example_a.path)) == ["甲票内容\n乙票内容"]
    assert not (tmp_path / "out" / "发票二合一.pdf").exists()


def test_group_pdf_uses_a4_sheets_with_two_pages_each(tmp_path: Path) -> None:
    first = make_pdf(tmp_path, [(40, 40, "票一")], name="a.pdf")
    second = make_pdf(tmp_path, [(40, 40, "票二")], name="b.pdf")
    third = make_pdf(tmp_path, [(40, 40, "票三")], name="c.pdf")
    rows = [
        make_row(first, id=1),
        make_row(second, id=2),
        make_row(third, id=3),
    ]

    result = write_group_pdfs(tmp_path / "out", rows)

    group = result.groups[0]
    assert group.source_pages == 3
    assert group.pages == 2
    with fitz.open(group.path) as document:
        assert document.page_count == 2
        for page in document:
            assert page.rect.width == pytest.approx(A4_WIDTH)
            assert page.rect.height == pytest.approx(A4_HEIGHT)
        top = document[0].search_for("票一")
        bottom = document[0].search_for("票二")
        assert top and bottom
        assert top[0].y1 <= A4_HEIGHT / 2
        assert bottom[0].y0 >= A4_HEIGHT / 2
        assert "票三" in document[1].get_text()
        assert "票一" not in document[1].get_text()


def test_shared_page_is_written_once_per_group(tmp_path: Path) -> None:
    source = make_pdf(tmp_path, [(40, 40, "同页")], name="same.pdf")
    rows = [
        make_row(source, id=1),
        make_row(source, id=2),
    ]

    result = write_group_pdfs(tmp_path / "out", rows)

    assert len(result.groups) == 1
    assert result.groups[0].count == 2
    assert result.groups[0].source_pages == 1
    assert result.groups[0].pages == 1
    assert page_texts(Path(result.groups[0].path)) == ["同页"]


def test_multi_page_source_keeps_only_requested_page(tmp_path: Path) -> None:
    source = make_pdf_pages(
        tmp_path,
        [[(40, 40, "第一页")], [(40, 40, "第二页")]],
        name="two.pdf",
    )
    rows = [make_row(source, page_index=1, page_count=2)]

    result = write_group_pdfs(tmp_path / "out", rows)

    assert result.groups[0].source_pages == 1
    assert result.groups[0].pages == 1
    assert page_texts(Path(result.groups[0].path)) == ["第二页"]


def test_unassigned_invoice_uses_unambiguous_report_company(tmp_path: Path) -> None:
    known = make_pdf(tmp_path, [(40, 40, "已知公司")], name="known.pdf")
    unassigned = make_pdf(tmp_path, [(40, 40, "铁路票")], name="train.pdf")
    rows = [
        make_row(
            known,
            id=1,
            buyer_name="示例甲公司",
            category="差旅费",
        ),
        make_row(unassigned, id=2, buyer_name="", category="差旅费"),
    ]

    result = write_group_pdfs(tmp_path / "out", rows, fallback_company="示例甲")

    assert {group.company for group in result.groups} == {
        "示例甲公司"
    }
    assert result.groups[0].category == "差旅费"
    assert result.groups[0].count == 2


def test_single_company_export_claims_unassigned_invoices(tmp_path: Path) -> None:
    known = make_pdf(tmp_path, [(40, 40, "已知公司")], name="known.pdf")
    unassigned = make_pdf(tmp_path, [(40, 40, "铁路票")], name="train.pdf")
    rows = [
        make_row(known, id=1, buyer_name="示例甲公司"),
        make_row(unassigned, id=2, buyer_name=""),
    ]

    result = write_group_pdfs(tmp_path / "out", rows)

    assert {group.company for group in result.groups} == {
        "示例甲公司"
    }


def test_ambiguous_report_company_keeps_unassigned_separate(tmp_path: Path) -> None:
    a = make_pdf(tmp_path, [(40, 40, "甲")], name="a.pdf")
    b = make_pdf(tmp_path, [(40, 40, "乙")], name="b.pdf")
    c = make_pdf(tmp_path, [(40, 40, "丙")], name="c.pdf")
    rows = [
        make_row(a, id=1, buyer_name="示例甲公司"),
        make_row(b, id=2, buyer_name="示例乙公司"),
        make_row(c, id=3, buyer_name=""),
    ]

    result = write_group_pdfs(tmp_path / "out", rows, fallback_company="示例")

    assert UNKNOWN_COMPANY in {group.company for group in result.groups}


def test_empty_buyer_and_category_get_explicit_names(tmp_path: Path) -> None:
    source = make_pdf(tmp_path, [(40, 40, "空字段")], name="empty.pdf")
    rows = [make_row(source, buyer_name="", category="")]

    result = write_group_pdfs(tmp_path / "out", rows)

    assert result.groups[0].company == UNKNOWN_COMPANY
    assert result.groups[0].category == UNKNOWN_CATEGORY
    assert Path(result.groups[0].path).name == f"{UNKNOWN_CATEGORY}.pdf"


def test_missing_and_invalid_sources_are_skipped(tmp_path: Path) -> None:
    good = make_pdf(tmp_path, [(40, 40, "正常")], name="good.pdf")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    rows = [
        make_row(good, id=1),
        make_row(tmp_path / "missing.pdf", id=2),
        make_row(broken, id=3),
        make_row(tmp_path / "only-missing.pdf", id=4, category="餐饮费"),
    ]

    result = write_group_pdfs(tmp_path / "out", rows)

    assert len(result.groups) == 1
    assert result.groups[0].category == "其他费用"
    assert result.groups[0].count == 3
    assert result.groups[0].source_pages == 1
    assert result.groups[0].pages == 1
    assert any("missing.pdf" in item for item in result.skipped_missing)
    assert any("broken.pdf" in item for item in result.skipped_invalid)


def test_rexport_removes_stale_generated_pdfs_but_keeps_user_files(
    tmp_path: Path,
) -> None:
    old_source = make_pdf(tmp_path, [(40, 40, "旧")], name="old.pdf")
    new_source = make_pdf(tmp_path, [(40, 40, "新")], name="new.pdf")
    output = tmp_path / "out"
    write_group_pdfs(
        output,
        [make_row(old_source, buyer_name="旧公司", category="差旅费")],
    )
    stale = output / "旧公司" / "差旅费.pdf"
    assert stale.is_file()
    user_file = output / "我的说明.pdf"
    user_file.write_text("keep me", encoding="utf-8")

    write_group_pdfs(
        output,
        [make_row(new_source, buyer_name="新公司", category="餐饮费")],
    )

    assert not stale.exists()
    assert not (output / "旧公司").exists()
    assert user_file.is_file()
    manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["files"] == ["新公司/餐饮费.pdf"]


def test_unsafe_and_reserved_names_are_sanitized(tmp_path: Path) -> None:
    source = make_pdf(tmp_path, [(40, 40, "脏名字")], name="dirty.pdf")
    rows = [
        make_row(source, buyer_name='CON', category='差旅/费:?*"<>|'),
    ]

    result = write_group_pdfs(tmp_path / "out", rows)

    path = Path(result.groups[0].path)
    assert path.parent.name == "_CON"
    assert path.name == "差旅_费_______.pdf"


def test_symlink_output_directory_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    source = make_pdf(tmp_path, [(40, 40, "x")], name="x.pdf")

    with pytest.raises(ExportError):
        write_group_pdfs(link, [make_row(source)])

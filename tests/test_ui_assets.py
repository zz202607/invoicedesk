"""Guard the local UI assets: pin the vendored Vue build and forbid XSS sinks."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app" / "ui"


def test_required_ui_assets_exist() -> None:
    for name in (
        "index.html",
        "app.js",
        "theme.js",
        "styles.css",
        "vendor/vue.global.prod.js",
    ):
        assert (UI / name).is_file(), name


def test_vendored_vue_hash_matches_manifest() -> None:
    bundle = UI / "vendor" / "vue.global.prod.js"
    # Hash the upstream LF form even if the checkout converted line endings.
    digest = hashlib.sha256(bundle.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    manifest = (UI / "vendor" / "SOURCE.txt").read_text(encoding="utf-8")
    assert "Vue 3.5.44" in manifest
    assert digest in manifest
    assert (UI / "vendor" / "LICENSE.vue").is_file()


def test_page_has_restrictive_csp_and_no_remote_assets() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'http-equiv="Content-Security-Policy"' in html
    assert "connect-src 'none'" in html
    # Vue's in-DOM template compiler needs eval; everything else is local.
    assert "'unsafe-eval'" in html
    for name in ("index.html", "app.js", "theme.js", "styles.css"):
        text = (UI / name).read_text(encoding="utf-8")
        assert "http://" not in text
        assert "https://" not in text


def test_invoice_corrections_are_wired_and_auditable() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "保存修正" in html
    assert "确认无误" in html
    assert "修正记录" in html
    assert "规则分类" in html
    # Rule names come from user input; the toast must interpolate, not render HTML.
    assert "{{ toast.message }}" in html
    assert "update_invoice" in script
    assert "get_invoice" in script
    assert "confirm_invoice" in script


def test_pool_shows_company_and_quick_category_editing() -> None:
    """Imported rows must expose buyer company and a one-click category fix."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "row.buyer_name" in html
    assert "购买方（发票抬头公司）" in html
    assert "startCategoryEdit" in html
    assert "startCategoryEdit" in script
    assert "commitCategory" in script
    assert "categoryChoices" in script
    assert "update_invoice" in script
    # A successful import switches the pool to the new batch and reopens it.
    assert "String(data.batch_id)" in script
    assert "this.poolVisible = true" in script


def test_rule_management_is_wired_to_the_bridge() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "分类规则" in html
    assert "应用到历史" in html
    assert "openRulesModal" in script
    assert "list_rules" in script
    assert "set_rule_enabled" in script
    assert "preview_rule_backfill" in script
    assert "apply_rule_backfill" in script
    assert "delete_rule" in script
    # The backend enforces preview-before-apply via a token; the UI must pass
    # that token through rather than calling apply directly.
    assert "preview.token" in script
    # Seller names are untrusted PDF text; confirmation dialogs use the
    # sanitizer instead of interpolating raw values.
    assert "displayText" in script
    assert "displayText(rule.pattern" in script
    # Unicode categories cover bidi marks and format characters beyond the
    # ranges an explicit list would catch.
    assert "\\p{Cf}" in script


def test_export_result_is_wired_to_the_bridge() -> None:
    """Exports take a long time; the result must stay open with the paths."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "exportResult" in html
    assert "打开文件夹" in html
    assert "openExportFolder" in html
    assert "open_output_folder" in script
    assert "this.exportResult = {" in script
    assert "openExportFolder()" in script


def test_invoice_text_is_never_rendered_as_html() -> None:
    """Parsed PDF text is untrusted; the UI must only interpolate it."""
    for name in ("index.html", "app.js"):
        text = (UI / name).read_text(encoding="utf-8")
        for sink in ("v-html", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in text, f"{name} uses {sink}"


def test_folder_picker_is_wired_to_the_bridge() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "浏览…" in html
    assert "pickFolder" in html
    assert "pick_folder" in script
    assert "pickingFolder" in script


def test_theme_switch_is_local_and_applied_before_paint() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    theme = (UI / "theme.js").read_text(encoding="utf-8")
    css = (UI / "styles.css").read_text(encoding="utf-8")
    # theme.js must run before the stylesheet so there is no light flash.
    assert html.index('src="theme.js"') < html.index('href="styles.css"')
    assert "invoicedesk.theme" in theme
    assert "localStorage" in theme
    assert "matchMedia" in theme
    assert "setAttribute('data-theme'" in theme
    assert "cycle" in theme
    # The data-theme attribute is the single source; no duplicate media block.
    assert ':root[data-theme="dark"]' in css
    assert "prefers-color-scheme" not in css
    assert "cycleTheme" in script
    assert "Ctrl+Shift+D" in html


def test_dashboard_is_wired_to_the_bridge() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "汇总看板" in html
    assert "分类分布" in html
    assert "月度分布" in html
    assert "¥{{ dashboard.total }}" in html
    assert "openDashboard" in script
    assert "dashboardLoading" in script
    assert "await this.call('dashboard'" in script
    assert "barPercent" in script
    assert "categoryMax" in script
    assert "monthMax" in script


def test_keyboard_shortcuts_are_wired() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "快捷键" in html
    assert "Ctrl</kbd> + <kbd>K" in html
    assert "Ctrl</kbd> + <kbd>D" in html
    assert "Ctrl</kbd> + <kbd>E" in html
    assert "F1" in html
    assert "onKeydown" in script
    assert "event.ctrlKey || event.metaKey" in script
    assert "focusSearch" in script
    assert "ref=\"searchInput\"" in html
    # Escape must close the newest overlays before the older ones.
    assert script.index("showShortcutsModal) this.showShortcutsModal = false") < script.index(
        "showDashboardModal) this.showDashboardModal = false"
    )


def test_import_and_export_are_prominent_and_wired() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    css = (UI / "styles.css").read_text(encoding="utf-8")
    # Both are primary quick actions in the top bar, with the export choices
    # reachable from one visible menu.
    assert 'class="btn action import"' in html
    assert 'class="btn action export export-main"' in html
    assert "导出报销单" in html
    assert "导出证据包" in html
    assert "exportReportFromMenu" in script
    assert "exportEvidenceFromMenu" in script
    assert ".btn.action.import" in css
    assert ".btn.action.export" in css


def test_import_modal_offers_recent_folders() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "最近用过的文件夹" in html
    assert "recent_import_paths" in script
    assert "folderName" in script
    assert "openImportModal" in script


def test_report_header_fields_offer_remembered_values() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert 'list="company-options"' in html
    assert 'list="department-options"' in html
    assert 'list="reporter-options"' in html
    assert "hasReportChoices" in script
    assert "boot.report_choices.company" in html
    assert "reportForm.company = name" in html


def test_reparse_action_is_wired_to_the_bridge() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    assert "重新识别未识别" in html
    assert "reparseUnrecognized" in script
    assert "reparse_unrecognized" in script
    # Old scanned records are recoverable with one click from a visible banner,
    # and the pool can be filtered down to rows still awaiting OCR.
    assert "一键重新识别" in html
    assert "仅未识别" in html
    assert "unrecognized_only" in script


def test_one_click_export_and_arrow_navigation_are_wired() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    css = (UI / "styles.css").read_text(encoding="utf-8")
    # The main export button runs the evidence package directly; the caret
    # still opens the menu for the report-only export.
    assert 'class="btn action export export-main"' in html
    assert '@click="exportEvidence"' in html
    assert 'class="btn action export export-caret"' in html
    assert "group_dir" in script
    assert "exportResult.groups" in html
    assert "按公司 / 分类拆分的 PDF" in html
    # Preview navigation with the arrow keys, excluding text fields.
    assert "ArrowDown" in script
    assert "ArrowUp" in script
    assert "stepPreview" in script
    assert "isContentEditable" in script
    assert "↑/↓ 切换" in html
    assert ".export-groups" in css
    assert ".notice-bar" in css

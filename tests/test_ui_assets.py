"""Guard the local UI assets: pin the vendored Vue build and forbid XSS sinks."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app" / "ui"


def test_required_ui_assets_exist() -> None:
    for name in ("index.html", "app.js", "styles.css", "vendor/vue.global.prod.js"):
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
    for name in ("index.html", "app.js", "styles.css"):
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

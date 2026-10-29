"""Desktop entry point: open the local workbench in a pywebview window.

The window loads only local files; the JS bridge talks to :mod:`app.api`,
which reads and writes the local SQLite database. No network access is
required or used.

A packaged build has no console, so startup failures (missing WebView2, a
broken bundle) show a dialog and are written to the rotating local log.
``--smoke`` runs the startup checks without opening a window; the build script
uses that exit code to verify a frozen bundle.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

from app import __version__
from app.api import Api
from app.core import logs, runtime
from app.core.export import redact_paths
from app.core.reports import ReportError

UI_INDEX = runtime.resource_path("app/ui/index.html")
WINDOW_TITLE = "发票管家 InvoiceDesk"
_TRUE_FLAGS = {"1", "true", "yes", "on"}
FOLDER_DIALOG_TIMEOUT = 600  # seconds; a picker left open this long is broken
WEBVIEW2_HELP = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
SMOKE_ASSETS = (
    "index.html",
    "app.js",
    "styles.css",
    "theme.js",
    "vendor/vue.global.prod.js",
)


def _env_flag(name: str) -> bool:
    """Parse a boolean environment flag; ``0``/``false`` mean off."""
    return os.environ.get(name, "").strip().lower() in _TRUE_FLAGS


def _show_error(title: str, message: str) -> None:
    """Surface a fatal startup error without depending on a console."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to stderr
            print(f"消息框不可用：{exc}", file=sys.stderr)
    print(f"{title}: {message}", file=sys.stderr)


def _gui_action(callback):
    """Wrap a Python callable as a .NET delegate (Windows desktop only)."""
    try:
        import clr  # noqa: F401 - pythonnet must be loaded before System imports
        from System import Action
    except ImportError as exc:  # pragma: no cover - the desktop runtime provides it
        raise ReportError("当前环境不支持文件夹选择，请手动粘贴路径。") from exc
    return Action(callback)


def _run_on_gui_thread(window, callback):
    """Run ``callback`` on the window's WinForms thread and return its value.

    pywebview delivers bridge calls on worker threads, and WinForms dialogs may
    only be created on the thread that owns the window.
    """
    invoke = getattr(getattr(window, "native", None), "Invoke", None)
    if invoke is None:
        raise ReportError("窗口尚未就绪，请稍后重试。")

    outcome: dict[str, object] = {}
    done = threading.Event()

    def runner() -> None:
        try:
            outcome["value"] = callback()
        except Exception as exc:  # noqa: BLE001 - reported as a readable error
            outcome["error"] = exc
        finally:
            done.set()

    try:
        invoke(_gui_action(runner))
    except ReportError:
        raise
    except Exception as exc:
        raise ReportError("无法打开文件夹选择器，请稍后重试。") from exc
    if not done.wait(FOLDER_DIALOG_TIMEOUT):
        raise ReportError("选择文件夹超时，请重试。")
    if "error" in outcome:
        raise ReportError("无法打开文件夹选择器，请稍后重试。")
    return outcome.get("value")


def make_folder_dialog(webview_module):
    """Build the GUI-thread-safe folder picker injected into :class:`Api`."""

    def pick_folder(current: str = "") -> str | None:
        windows = getattr(webview_module, "windows", None) or []
        if not windows:
            raise ReportError("窗口尚未就绪，请稍后重试。")
        initial = current if current and os.path.isdir(current) else str(Path.home())

        window = windows[0]
        picked = _run_on_gui_thread(
            window,
            lambda: window.create_file_dialog(
                webview_module.FileDialog.FOLDER, initial
            ),
        )
        if not picked:
            return None
        return str(picked[0])

    return pick_folder


def _smoke_requested() -> bool:
    return "--smoke" in sys.argv[1:] or _env_flag("INVOICEDESK_SMOKE")


def _write_smoke_report(report: dict[str, object]) -> None:
    target = os.environ.get("INVOICEDESK_SMOKE_REPORT", "").strip()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if target:
        Path(target).write_text(text, encoding="utf-8")
    else:
        print(text)


def run_smoke() -> int:
    """Check bundle resources, OCR, WebView2, and a throwaway database; no window."""
    ui_dir = runtime.resource_path("app/ui")
    checks: dict[str, bool] = {
        "ui_assets": all((ui_dir / name).is_file() for name in SMOKE_ASSETS),
        "webview2": sys.platform != "win32" or runtime.webview2_available(),
    }
    detail: dict[str, object] = {}
    try:
        # A frozen bundle is only useful if the OCR models were packaged too.
        from app.core import ocr

        ocr.self_check()
        checks["ocr"] = True
    except Exception as exc:  # noqa: BLE001 - reported in the smoke report
        checks["ocr"] = False
        detail["ocr_error"] = redact_paths(str(exc))
    try:
        with tempfile.TemporaryDirectory(prefix="invoicedesk-smoke-") as tmp:
            api = Api(db_path=Path(tmp) / "smoke.db", out_dir=Path(tmp) / "out")
            response = api.bootstrap()
            checks["database"] = bool(response.get("ok"))
            data = response.get("data") or {}
            checks["version"] = data.get("version") == __version__
            detail["db_name"] = data.get("db_name")
    except Exception as exc:  # noqa: BLE001 - reported in the smoke report
        checks["database"] = False
        detail["error"] = redact_paths(str(exc))

    report = {
        "ok": all(checks.values()),
        "frozen": runtime.is_frozen(),
        "version": __version__,
        "checks": checks,
        "detail": detail,
    }
    try:
        _write_smoke_report(report)
    except OSError as exc:
        print(f"无法写入冒烟测试报告：{redact_paths(str(exc))}", file=sys.stderr)
        return 1
    return 0 if report["ok"] else 1


def main(webview_module=None, *, message_box=None) -> int:
    """Open the workbench window and block until it closes.

    ``webview_module`` is injectable so tests can exercise the wiring without
    starting a real WebView2 window. ``message_box`` is injectable for the same
    reason; production uses a Win32 message box.
    """
    box = message_box or _show_error
    if _smoke_requested():
        return run_smoke()

    if webview_module is None:
        log_target = logs.setup_logging(runtime.log_file())
        logger = logging.getLogger(logs.LOGGER_NAME)
        logger.info("starting InvoiceDesk %s (frozen=%s)", __version__, runtime.is_frozen())
        logs.install_exception_hooks(log_target, box)

        if sys.platform == "win32" and not runtime.webview2_available():
            logger.error("WebView2 Evergreen Runtime not detected")
            box(
                WINDOW_TITLE,
                "未检测到 Microsoft Edge WebView2 运行时，工作台无法启动。\n\n"
                "请安装 WebView2 Runtime 后重试：\n"
                f"{WEBVIEW2_HELP}\n\n"
                "安装完成后不需要重启电脑。",
            )
            return 2

        try:
            import webview as webview_module
        except ImportError:
            logger.exception("pywebview is not installed")
            box(
                WINDOW_TITLE,
                "缺少 pywebview，无法打开工作台。请先运行：\n"
                "  python -m pip install -r requirements.txt",
            )
            return 1

    db_path = os.environ.get("INVOICEDESK_DB") or None
    out_dir = os.environ.get("INVOICEDESK_OUT") or None
    # DevTools can execute any bridge call, so keep it out of packaged builds
    # and make an accidentally enabled debug session visible in the title.
    debug = _env_flag("INVOICEDESK_DEBUG") and not getattr(sys, "frozen", False)
    title = WINDOW_TITLE + (" [调试]" if debug else "")

    try:
        api = Api(
            db_path=db_path,
            out_dir=out_dir,
            folder_dialog=make_folder_dialog(webview_module),
        )
        webview_module.create_window(
            title,
            url=UI_INDEX.as_uri(),
            js_api=api,
            width=1200,
            height=800,
            min_size=(1024, 640),
            # A fixed 1440×900 window gets clamped by Windows on 1280×720 screens
            # while WebView2 keeps the larger viewport, clipping the right toolbar.
            maximized=True,
            text_select=True,
            confirm_close=False,
        )
        webview_module.start(debug=debug, private_mode=True)
    except Exception as exc:  # last line of defence before exit
        logging.getLogger(logs.LOGGER_NAME).exception("workbench failed to start")
        box(
            WINDOW_TITLE,
            f"工作台启动失败：{redact_paths(str(exc))}\n"
            f"日志：{runtime.log_file()}",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Desktop entry point: open the local workbench in a pywebview window.

The window loads only local files; the JS bridge talks to :mod:`app.api`,
which reads and writes the local SQLite database. No network access is
required or used.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from app.api import Api
from app.core.reports import ReportError

UI_INDEX = Path(__file__).resolve().parent / "ui" / "index.html"
WINDOW_TITLE = "发票管家 InvoiceDesk"
_TRUE_FLAGS = {"1", "true", "yes", "on"}
FOLDER_DIALOG_TIMEOUT = 600  # seconds; a picker left open this long is broken


def _env_flag(name: str) -> bool:
    """Parse a boolean environment flag; ``0``/``false`` mean off."""
    return os.environ.get(name, "").strip().lower() in _TRUE_FLAGS


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


def main(webview_module=None) -> int:
    """Open the workbench window and block until it closes.

    ``webview_module`` is injectable so tests can exercise the wiring without
    starting a real WebView2 window.
    """
    if webview_module is None:
        try:
            import webview as webview_module
        except ImportError:
            print(
                "缺少 pywebview，无法打开工作台。请先运行：\n"
                "  python -m pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 1

    db_path = os.environ.get("INVOICEDESK_DB") or None
    out_dir = os.environ.get("INVOICEDESK_OUT") or None
    # DevTools can execute any bridge call, so keep it out of packaged builds
    # and make an accidentally enabled debug session visible in the title.
    debug = _env_flag("INVOICEDESK_DEBUG") and not getattr(sys, "frozen", False)
    title = WINDOW_TITLE + (" [调试]" if debug else "")
    api = Api(
        db_path=db_path,
        out_dir=out_dir,
        folder_dialog=make_folder_dialog(webview_module),
    )
    webview_module.create_window(
        title,
        url=UI_INDEX.as_uri(),
        js_api=api,
        width=1440,
        height=900,
        min_size=(1080, 700),
        text_select=True,
        confirm_close=False,
    )
    webview_module.start(debug=debug, private_mode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

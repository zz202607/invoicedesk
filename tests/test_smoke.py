from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from app import __version__
from app.api import Api
from app.main import main


class FakeNative:
    def __init__(self) -> None:
        self.invoked = 0

    def Invoke(self, action) -> None:
        self.invoked += 1
        action()


class SilentNative:
    def Invoke(self, action) -> None:
        """Simulate a GUI thread that never runs the delegate."""


class FakeWindow:
    def __init__(self, picked: tuple[str, ...] | None = None) -> None:
        self.picked = picked
        self.native = FakeNative()
        self.dialog_calls: list[tuple] = []

    def create_file_dialog(self, dialog_type, directory):
        self.dialog_calls.append((dialog_type, directory))
        return self.picked


class FakeWebview:
    FileDialog = SimpleNamespace(FOLDER=20)

    def __init__(self) -> None:
        self.window_kwargs: dict = {}
        self.start_kwargs: dict = {}
        self.windows: list[FakeWindow] = []

    def create_window(self, title, **kwargs):
        self.window_kwargs = {"title": title, **kwargs}
        return FakeWindow()

    def start(self, **kwargs) -> None:
        self.start_kwargs = kwargs


def test_version_is_present() -> None:
    assert __version__ == "0.1.0"


def test_main_wires_the_window_without_starting_a_real_gui() -> None:
    fake = FakeWebview()
    assert main(fake) == 0

    assert fake.window_kwargs["title"] == "发票管家 InvoiceDesk"
    url = fake.window_kwargs["url"]
    assert url.endswith("/ui/index.html")
    assert Path(url.removeprefix("file://")).name == "index.html"
    assert isinstance(fake.window_kwargs["js_api"], Api)
    assert fake.window_kwargs["min_size"] == (1024, 640)
    assert fake.window_kwargs["maximized"] is True
    assert fake.start_kwargs["private_mode"] is True
    assert fake.start_kwargs["debug"] is False


def test_debug_flag_parsing(monkeypatch) -> None:
    for value, expected in (
        ("1", True),
        ("true", True),
        ("YES", True),
        (" on ", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("2", False),
        ("random", False),
        ("", False),
    ):
        monkeypatch.setenv("INVOICEDESK_DEBUG", value)
        fake = FakeWebview()
        assert main(fake) == 0
        assert fake.start_kwargs["debug"] is expected
        expected_title = "发票管家 InvoiceDesk" + (" [调试]" if expected else "")
        assert fake.window_kwargs["title"] == expected_title


def test_packaged_build_ignores_debug_flag(monkeypatch) -> None:
    monkeypatch.setenv("INVOICEDESK_DEBUG", "1")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake = FakeWebview()
    assert main(fake) == 0
    assert fake.start_kwargs["debug"] is False
    assert "[调试]" not in fake.window_kwargs["title"]


def _gui_fake(monkeypatch, picked):
    # The .NET delegate wrapper only exists inside a real desktop runtime.
    monkeypatch.setattr("app.main._gui_action", lambda callback: callback)
    fake = FakeWebview()
    window = FakeWindow(picked=picked)
    fake.windows.append(window)
    return fake, window


def test_folder_picker_uses_the_native_dialog_on_the_gui_thread(monkeypatch, tmp_path) -> None:
    fake, window = _gui_fake(monkeypatch, (str(tmp_path),))
    assert main(fake) == 0

    api = fake.window_kwargs["js_api"]
    response = api.pick_folder(str(tmp_path))
    assert response == {"ok": True, "data": {"path": str(tmp_path)}}
    assert window.dialog_calls == [(20, str(tmp_path))]
    assert window.native.invoked == 1


def test_folder_picker_cancel_and_initial_directory_fallback(monkeypatch, tmp_path) -> None:
    fake, window = _gui_fake(monkeypatch, None)
    assert main(fake) == 0

    api = fake.window_kwargs["js_api"]
    assert api.pick_folder(str(tmp_path)) == {"ok": True, "data": {"path": None}}
    api.pick_folder(str(tmp_path / "missing"))
    assert window.dialog_calls[-1] == (20, str(Path.home()))


def test_folder_picker_reports_a_missing_window(monkeypatch) -> None:
    monkeypatch.setattr("app.main._gui_action", lambda callback: callback)
    fake = FakeWebview()
    assert main(fake) == 0

    response = fake.window_kwargs["js_api"].pick_folder("")
    assert response["ok"] is False
    assert "窗口尚未就绪" in response["error"]


def test_folder_picker_timeout_and_dialog_errors(monkeypatch) -> None:
    monkeypatch.setattr("app.main._gui_action", lambda callback: callback)
    fake = FakeWebview()
    window = FakeWindow(picked=None)
    window.native = SilentNative()
    fake.windows.append(window)
    assert main(fake) == 0

    api = fake.window_kwargs["js_api"]
    monkeypatch.setattr("app.main.FOLDER_DIALOG_TIMEOUT", 0.001)
    response = api.pick_folder("")
    assert response["ok"] is False
    assert "超时" in response["error"]

    def boom(dialog_type, directory):
        raise RuntimeError("dialog exploded")

    window.native = FakeNative()
    window.create_file_dialog = boom
    response = api.pick_folder("")
    assert response["ok"] is False
    assert "无法打开文件夹选择器" in response["error"]

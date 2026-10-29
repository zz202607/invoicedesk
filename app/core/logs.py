"""Local file logging for the packaged desktop app.

A windowed PyInstaller build has no console: ``sys.stdout`` and ``sys.stderr``
are ``None``, so the bridge's diagnostic prints disappear. Logging goes to a
rotating file under the user data directory instead. Logs stay on this machine;
they may contain local paths, so they are never uploaded.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "invoicedesk"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

MessageBox = Callable[[str, str], None]


class _LogStream:
    """Minimal text stream that forwards writes to the file logger."""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level

    def write(self, text: str) -> int:
        message = text.rstrip()
        if message:
            self._logger.log(self._level, "%s", message)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def setup_logging(
    path: str | Path,
    *,
    level: int = logging.INFO,
    force: bool = False,
) -> Path | None:
    """Attach a rotating file handler; return its path or ``None`` on failure.

    Repeated calls are no-ops unless ``force`` is set, so startup wiring cannot
    duplicate handlers or open the same file twice.
    """
    log_path = Path(path)
    logger = logging.getLogger(LOGGER_NAME)
    if getattr(logger, "_invoicedesk_configured", False) and not force:
        return log_path

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    active: logging.Handler | None = None
    target: Path | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        active = RotatingFileHandler(
            log_path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        target = log_path
    except OSError:
        if sys.stderr is not None:
            active = logging.StreamHandler(sys.stderr)
    if active is None:
        active = logging.NullHandler()
    active.setFormatter(formatter)
    logger.addHandler(active)
    logger._invoicedesk_configured = True  # type: ignore[attr-defined]

    # Windowed builds lose the streams the API layer prints errors to.
    if getattr(sys, "frozen", False):
        if sys.stderr is None:
            sys.stderr = _LogStream(logger, logging.WARNING)  # type: ignore[assignment]
        if sys.stdout is None:
            sys.stdout = _LogStream(logger, logging.INFO)  # type: ignore[assignment]
    return target


def install_exception_hooks(target: Path | None, message_box: MessageBox | None = None) -> None:
    """Log uncaught exceptions and, for the main thread, show a dialog."""
    logger = logging.getLogger(LOGGER_NAME)

    def _main_hook(exc_type, exc_value, traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, traceback)
            return
        logger.error("uncaught exception", exc_info=(exc_type, exc_value, traceback))
        if message_box is not None:
            location = f"\n日志：{target}" if target is not None else ""
            message_box("发票管家 InvoiceDesk", f"程序遇到意外错误，已记录日志。{location}")

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.error(
            "uncaught thread exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _main_hook
    threading.excepthook = _thread_hook

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from watchdog_app.logging_utils import configure_logging


def test_configure_logging_closes_replaced_rotating_file_handlers(monkeypatch, tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    try:
        first_log_path = configure_logging(tmp_path / "first")
        old_handler = next(
            handler
            for handler in root_logger.handlers
            if isinstance(handler, RotatingFileHandler)
            and Path(getattr(handler, "baseFilename", "")) == first_log_path
        )

        close_calls = {"count": 0}
        original_close = old_handler.close

        def _tracked_close() -> None:
            close_calls["count"] += 1
            original_close()

        monkeypatch.setattr(old_handler, "close", _tracked_close)

        second_log_path = configure_logging(tmp_path / "second")

        assert close_calls["count"] == 1
        assert old_handler not in root_logger.handlers
        assert any(
            isinstance(handler, RotatingFileHandler)
            and Path(getattr(handler, "baseFilename", "")) == second_log_path
            for handler in root_logger.handlers
        )
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(original_level)
        for handler in original_handlers:
            root_logger.addHandler(handler)


def test_configure_logging_adds_single_console_handler(monkeypatch, tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    try:
        configure_logging(tmp_path / "logs")
        configure_logging(tmp_path / "logs")

        file_handlers = [
            handler for handler in root_logger.handlers if isinstance(handler, RotatingFileHandler)
        ]
        console_handlers = [
            handler
            for handler in root_logger.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, RotatingFileHandler)
        ]

        assert len(file_handlers) == 1
        assert len(console_handlers) == 1
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(original_level)
        for handler in original_handlers:
            root_logger.addHandler(handler)

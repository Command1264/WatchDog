from __future__ import annotations

from datetime import datetime
from pathlib import Path
import logging

from watchdog_app.logging_utils import (
    WatchDogFileHandler,
    WatchDogFormatter,
    configure_logging,
)
from watchdog_app.storage import log_output_root


def _restore_root_logger(original_handlers: list[logging.Handler], original_level: int) -> None:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.setLevel(original_level)
    for handler in original_handlers:
        root_logger.addHandler(handler)


def _record(logger_name: str, level: int, message: str, created_at: datetime) -> logging.LogRecord:
    record = logging.LogRecord(logger_name, level, __file__, 0, message, (), None)
    record.created = created_at.timestamp()
    return record


def test_formatter_uses_millisecond_precision_and_right_aligned_level_name() -> None:
    formatter = WatchDogFormatter()
    record = _record(
        "watchdog_app.test",
        logging.WARNING,
        "hello",
        datetime(2026, 4, 17, 9, 10, 11, 345000),
    )

    assert formatter.format(record) == (
        "2026-04-17 09:10:11.345 [ Warn] watchdog_app.test: hello"
    )


def test_formatter_maps_critical_to_error_label() -> None:
    formatter = WatchDogFormatter()
    record = _record(
        "watchdog_app.test",
        logging.CRITICAL,
        "boom",
        datetime(2026, 4, 17, 9, 10, 11, 987000),
    )

    assert formatter.format(record) == (
        "2026-04-17 09:10:11.987 [Error] watchdog_app.test: boom"
    )


def test_configure_logging_creates_expected_log_path_and_header(tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    app_started_at = datetime(2026, 4, 17, 8, 0, 0, 456000)
    current_time = datetime(2026, 4, 17, 9, 10, 11, 123000)

    try:
        log_path = configure_logging(
            tmp_path,
            app_started_at=app_started_at,
            now_provider=lambda: current_time,
        )

        assert log_path == (
            tmp_path
            / "WatchDogLogs"
            / "2026-04-17"
            / "WatchDog_2026-04-17-09-10-11.log"
        )

        logging.getLogger("watchdog_app.test").info("hello")

        content = log_path.read_text(encoding="utf-8")
        assert (
            "2026-04-17 09:10:11.123 [ Info] watchdog_app.logging: "
            "Application started at: 2026-04-17 08:00:00.456"
        ) in content
        assert (
            "2026-04-17 09:10:11.123 [ Info] watchdog_app.logging: "
            "Log created at: 2026-04-17 09:10:11.123"
        ) in content
        assert "[ Info] watchdog_app.test: hello" in content
    finally:
        _restore_root_logger(original_handlers, original_level)


def test_configure_logging_closes_replaced_watchdog_file_handlers(monkeypatch, tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    try:
        first_log_path = configure_logging(
            tmp_path / "first",
            now_provider=lambda: datetime(2026, 4, 17, 9, 0, 0, 100000),
        )
        old_handler = next(
            handler
            for handler in root_logger.handlers
            if isinstance(handler, WatchDogFileHandler)
            and handler.current_log_path == first_log_path
        )

        close_calls = {"count": 0}
        original_close = old_handler.close

        def _tracked_close() -> None:
            close_calls["count"] += 1
            original_close()

        monkeypatch.setattr(old_handler, "close", _tracked_close)

        second_log_path = configure_logging(
            tmp_path / "second",
            now_provider=lambda: datetime(2026, 4, 17, 9, 1, 0, 200000),
        )

        assert close_calls["count"] == 1
        assert old_handler not in root_logger.handlers
        assert any(
            isinstance(handler, WatchDogFileHandler)
            and handler.current_log_path == second_log_path
            for handler in root_logger.handlers
        )
    finally:
        _restore_root_logger(original_handlers, original_level)


def test_configure_logging_adds_single_console_handler(tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    try:
        configure_logging(
            tmp_path / "logs",
            now_provider=lambda: datetime(2026, 4, 17, 9, 0, 0, 100000),
        )
        configure_logging(
            tmp_path / "logs",
            now_provider=lambda: datetime(2026, 4, 17, 9, 0, 1, 100000),
        )

        file_handlers = [
            handler for handler in root_logger.handlers if isinstance(handler, WatchDogFileHandler)
        ]
        console_handlers = [
            handler
            for handler in root_logger.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, WatchDogFileHandler)
        ]

        assert len(file_handlers) == 1
        assert len(console_handlers) == 1
    finally:
        _restore_root_logger(original_handlers, original_level)


def test_watchdog_file_handler_rolls_over_on_size_and_links_next_relative_path(tmp_path: Path) -> None:
    times = iter(
        [
            datetime(2026, 4, 17, 9, 0, 1, 200000),
            datetime(2026, 4, 17, 9, 0, 1, 300000),
        ]
    )
    handler = WatchDogFileHandler(
        tmp_path,
        app_started_at=datetime(2026, 4, 17, 8, 0, 0, 0),
        now_provider=lambda: next(times),
        max_bytes=300,
    )
    handler.setFormatter(WatchDogFormatter())

    logger = logging.getLogger("watchdog_app.test.size")
    logger.setLevel(logging.DEBUG)
    logger.handlers = [handler]
    logger.propagate = False

    try:
        first_record = _record(
            "watchdog_app.test.size",
            logging.INFO,
            "X" * 220,
            datetime(2026, 4, 17, 9, 0, 0, 900000),
        )
        handler.emit(first_record)

        first_path = tmp_path / "WatchDogLogs" / "2026-04-17" / "WatchDog_2026-04-17-09-00-00.log"
        second_path = tmp_path / "WatchDogLogs" / "2026-04-17" / "WatchDog_2026-04-17-09-00-01.log"

        assert handler.current_log_path == second_path
        first_content = first_path.read_text(encoding="utf-8")
        second_content = second_path.read_text(encoding="utf-8")

        assert (
            "2026-04-17 09:00:01.200 [ Info] watchdog_app.logging: "
            "Log ended at: 2026-04-17 09:00:01.200"
        ) in first_content
        assert (
            "2026-04-17 09:00:01.200 [ Info] watchdog_app.logging: "
            "Next log: WatchDog_2026-04-17-09-00-01.log"
        ) in first_content
        assert (
            "2026-04-17 09:00:01.200 [ Info] watchdog_app.logging: "
            "Log created at: 2026-04-17 09:00:01.200"
        ) in second_content
    finally:
        handler.close()
        logger.handlers = []


def test_watchdog_file_handler_rolls_over_when_day_changes(tmp_path: Path) -> None:
    times = iter(
        [
            datetime(2026, 4, 18, 0, 0, 0, 200000),
        ]
    )
    handler = WatchDogFileHandler(
        tmp_path,
        app_started_at=datetime(2026, 4, 17, 20, 0, 0, 0),
        now_provider=lambda: next(times),
        max_bytes=5 * 1024 * 1024,
    )
    handler.setFormatter(WatchDogFormatter())

    try:
        handler.emit(
            _record(
                "watchdog_app.test.day",
                logging.INFO,
                "day-one",
                datetime(2026, 4, 17, 23, 59, 59, 700000),
            )
        )
        handler.emit(
            _record(
                "watchdog_app.test.day",
                logging.INFO,
                "day-two",
                datetime(2026, 4, 18, 0, 0, 0, 100000),
            )
        )

        day_one_path = tmp_path / "WatchDogLogs" / "2026-04-17" / "WatchDog_2026-04-17-23-59-59.log"
        day_two_path = tmp_path / "WatchDogLogs" / "2026-04-18" / "WatchDog_2026-04-18-00-00-00.log"

        assert handler.current_log_path == day_two_path
        first_content = day_one_path.read_text(encoding="utf-8")
        second_content = day_two_path.read_text(encoding="utf-8")

        assert "Next log: ../2026-04-18/WatchDog_2026-04-18-00-00-00.log" in first_content
        assert "[ Info] watchdog_app.test.day: day-two" in second_content
    finally:
        handler.close()


def test_watchdog_file_handler_close_writes_end_footer(tmp_path: Path) -> None:
    times = iter(
        [
            datetime(2026, 4, 17, 9, 0, 0, 100000),
            datetime(2026, 4, 17, 9, 0, 2, 300000),
        ]
    )
    handler = WatchDogFileHandler(
        tmp_path,
        app_started_at=datetime(2026, 4, 17, 8, 0, 0, 0),
        now_provider=lambda: next(times),
    )
    handler.setFormatter(WatchDogFormatter())

    path = handler.ensure_active_log()
    handler.close()

    content = path.read_text(encoding="utf-8")
    assert (
        "2026-04-17 09:00:02.300 [ Info] watchdog_app.logging: "
        "Next log: End"
    ) in content


def test_log_output_root_helper(tmp_path: Path) -> None:
    assert log_output_root(tmp_path / "base") == tmp_path / "base" / "WatchDogLogs"

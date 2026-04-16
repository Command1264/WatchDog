from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, TextIO
import logging
import os

from .storage import log_output_root


APP_LOGGER_NAME = "watchdog_app.logging"
MAX_LOG_FILE_BYTES = 5 * 1024 * 1024
APP_STARTED_AT = datetime.now()


class WatchDogFormatter(logging.Formatter):
    _LEVEL_NAMES = {
        logging.DEBUG: "Debug",
        logging.INFO: "Info",
        logging.WARNING: "Warn",
        logging.ERROR: "Error",
        logging.CRITICAL: "Error",
    }

    @classmethod
    def format_datetime(cls, value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @classmethod
    def format_level_name(cls, levelno: int) -> str:
        level_name = cls._LEVEL_NAMES.get(levelno, "Info")
        return f"{level_name:>5}"

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        del datefmt
        return self.format_datetime(datetime.fromtimestamp(record.created))

    def format_line(
        self,
        *,
        timestamp: datetime,
        levelno: int,
        application: str,
        message: str,
    ) -> str:
        return (
            f"{self.format_datetime(timestamp)} "
            f"[{self.format_level_name(levelno)}] "
            f"{application}: {message}"
        )

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        output = self.format_line(
            timestamp=datetime.fromtimestamp(record.created),
            levelno=record.levelno,
            application=record.name,
            message=message,
        )
        if record.exc_info:
            output = f"{output}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            output = f"{output}\n{self.formatStack(record.stack_info)}"
        return output


class WatchDogFileHandler(logging.Handler):
    terminator = "\n"

    def __init__(
        self,
        log_directory: Path,
        *,
        app_started_at: datetime | None = None,
        now_provider: Callable[[], datetime] | None = None,
        max_bytes: int = MAX_LOG_FILE_BYTES,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self._log_directory = Path(log_directory)
        self._logs_root = log_output_root(self._log_directory)
        self._app_started_at = app_started_at or APP_STARTED_AT
        self._now = now_provider or datetime.now
        self._max_bytes = max_bytes
        self._encoding = encoding
        self._stream: TextIO | None = None
        self._current_path: Path | None = None
        self._current_created_at: datetime | None = None
        self._current_size = 0

    @property
    def base_log_directory(self) -> Path:
        return self._log_directory

    @property
    def current_log_path(self) -> Path | None:
        return self._current_path

    def ensure_active_log(self, timestamp: datetime | None = None) -> Path:
        self.acquire()
        try:
            if self._stream is None or self._current_path is None:
                self._open_new_log(timestamp or self._now())
            return self._current_path
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = self.format(record)
            record_dt = datetime.fromtimestamp(record.created)
            self.acquire()
            try:
                self.ensure_active_log(record_dt)
                if self._current_created_at is not None and self._current_created_at.date() != record_dt.date():
                    self._rollover(record_dt)
                self._write_entry(entry)
                if self._current_size >= self._max_bytes:
                    self._rollover(self._now())
            finally:
                self.release()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                self._close_current_log(ended_at=self._now(), next_path=None)
                self._stream = None
                self._current_path = None
                self._current_created_at = None
                self._current_size = 0
        finally:
            self.release()
        super().close()

    def _formatter(self) -> WatchDogFormatter:
        formatter = self.formatter
        if isinstance(formatter, WatchDogFormatter):
            return formatter
        fallback = WatchDogFormatter()
        self.setFormatter(fallback)
        return fallback

    def _candidate_path(self, timestamp: datetime) -> Path:
        return (
            self._logs_root
            / timestamp.strftime("%Y-%m-%d")
            / f"WatchDog_{timestamp.strftime('%Y-%m-%d-%H-%M-%S')}.log"
        )

    def _build_log_path(self, timestamp: datetime) -> Path:
        candidate_time = timestamp.replace(microsecond=0)
        for _ in range(86_400):
            candidate = self._candidate_path(candidate_time)
            if candidate != self._current_path and not candidate.exists():
                return candidate
            candidate_time += timedelta(seconds=1)
        raise RuntimeError("Unable to allocate a unique log file path.")

    def _open_new_log(self, created_at: datetime, *, path: Path | None = None) -> None:
        file_path = path or self._build_log_path(created_at)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = file_path.open("w", encoding=self._encoding, newline="\n")
        self._current_path = file_path
        self._current_created_at = created_at
        self._current_size = 0
        self._write_internal_line(
            timestamp=created_at,
            message=f"Application started at: {self._formatter().format_datetime(self._app_started_at)}",
        )
        self._write_internal_line(
            timestamp=created_at,
            message=f"Log created at: {self._formatter().format_datetime(created_at)}",
        )

    def _write_entry(self, entry: str) -> None:
        if self._stream is None:
            raise RuntimeError("Log stream is not open.")
        payload = f"{entry}{self.terminator}"
        self._stream.write(payload)
        self._stream.flush()
        self._current_size += len(payload.encode(self._encoding))

    def _write_internal_line(self, *, timestamp: datetime, message: str) -> None:
        self._write_entry(
            self._formatter().format_line(
                timestamp=timestamp,
                levelno=logging.INFO,
                application=APP_LOGGER_NAME,
                message=message,
            )
        )

    def _relative_next_path(self, next_path: Path | None) -> str:
        if next_path is None or self._current_path is None:
            return "End"
        return Path(os.path.relpath(next_path, self._current_path.parent)).as_posix()

    def _close_current_log(self, *, ended_at: datetime, next_path: Path | None) -> None:
        if self._stream is None:
            return
        self._write_internal_line(
            timestamp=ended_at,
            message=f"Log ended at: {self._formatter().format_datetime(ended_at)}",
        )
        self._write_internal_line(
            timestamp=ended_at,
            message=f"Next log: {self._relative_next_path(next_path)}",
        )
        self._stream.close()

    def _rollover(self, next_created_at: datetime) -> None:
        if self._stream is None:
            self._open_new_log(next_created_at)
            return
        next_path = self._build_log_path(next_created_at)
        self._close_current_log(ended_at=next_created_at, next_path=next_path)
        self._stream = None
        self._current_path = None
        self._current_created_at = None
        self._current_size = 0
        self._open_new_log(next_created_at, path=next_path)


def configure_logging(
    log_directory: Path,
    *,
    app_started_at: datetime | None = None,
    now_provider: Callable[[], datetime] | None = None,
    max_bytes: int = MAX_LOG_FILE_BYTES,
) -> Path:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    target_directory = Path(log_directory)
    existing = [
        handler
        for handler in root_logger.handlers
        if isinstance(handler, WatchDogFileHandler) and handler.base_log_directory == target_directory
    ]
    if existing:
        return existing[0].ensure_active_log()

    removed_handlers: list[WatchDogFileHandler] = []
    for handler in list(root_logger.handlers):
        if isinstance(handler, WatchDogFileHandler):
            root_logger.removeHandler(handler)
            removed_handlers.append(handler)

    for handler in removed_handlers:
        handler.close()

    has_console_handler = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, WatchDogFileHandler)
        for handler in root_logger.handlers
    )

    formatter = WatchDogFormatter()
    file_handler = WatchDogFileHandler(
        target_directory,
        app_started_at=app_started_at,
        now_provider=now_provider,
        max_bytes=max_bytes,
    )
    file_handler.setFormatter(formatter)
    active_path = file_handler.ensure_active_log()
    root_logger.addHandler(file_handler)

    if not has_console_handler:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root_logger.addHandler(console)

    return active_path

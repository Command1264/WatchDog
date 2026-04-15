from __future__ import annotations

from logging.handlers import RotatingFileHandler
from pathlib import Path
import logging

from .storage import log_file_path


def configure_logging(log_directory: Path) -> Path:
    log_directory.mkdir(parents=True, exist_ok=True)
    path = log_file_path(log_directory)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    existing = [
        handler
        for handler in root_logger.handlers
        if isinstance(handler, RotatingFileHandler)
        and Path(getattr(handler, "baseFilename", "")) == path
    ]
    if existing:
        return path

    for handler in list(root_logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        path,
        maxBytes=1_048_576,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    if not any(isinstance(handler, logging.StreamHandler) for handler in root_logger.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root_logger.addHandler(console)

    return path

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys

from .models import (
    APP_NAME,
    BOOTSTRAP_FILE_NAME,
    CONFIG_FILE_NAME,
    ExitReason,
    LOGS_DIRECTORY_NAME,
    normalize_path_text,
    normalize_separators,
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_path() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve()
    return Path(sys.argv[0]).resolve()


def runtime_base_dir() -> Path:
    if is_frozen():
        return executable_path().parent
    return Path(__file__).resolve().parents[2]


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def asset_path(*parts: str) -> Path:
    return package_dir().joinpath("assets", *parts)


def app_icon_path() -> Path:
    return asset_path("icons", "WatchDog.ico")


def ready_icon_path() -> Path:
    return asset_path("icons", "WatchDog-Ready.ico")


def not_ready_icon_path() -> Path:
    return asset_path("icons", "WatchDog-NotReady.ico")


def appdata_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME


def local_appdata_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME


def bootstrap_path() -> Path:
    return local_appdata_dir() / BOOTSTRAP_FILE_NAME


def default_config_path() -> Path:
    return appdata_dir() / CONFIG_FILE_NAME


def default_log_path() -> Path:
    return local_appdata_dir() / LOGS_DIRECTORY_NAME


def child_command() -> list[str]:
    if is_frozen():
        return [normalize_path_text(executable_path()), "--child-app"]
    return [normalize_path_text(sys.executable), "-m", "watchdog_app.main", "--child-app"]


def startup_command() -> list[str]:
    if is_frozen():
        return [normalize_path_text(executable_path())]
    return [normalize_path_text(sys.executable), "-m", "watchdog_app.main"]


def startup_command_line() -> str:
    return normalize_separators(subprocess.list2cmdline(startup_command()))


def exit_code(reason: ExitReason) -> int:
    return reason.value

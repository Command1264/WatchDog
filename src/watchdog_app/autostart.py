from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import winreg

from .models import APP_NAME, AutoStartProvider, AutoStartScope
from .runtime import startup_command_line


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME = f"{APP_NAME} AutoStart"


@dataclass(slots=True)
class AutoStartStatus:
    scope: AutoStartScope
    provider: AutoStartProvider | None
    enabled: bool
    message: str = ""


def _registry_hive(scope: AutoStartScope):
    if scope == AutoStartScope.CURRENT_USER:
        return winreg.HKEY_CURRENT_USER
    return winreg.HKEY_LOCAL_MACHINE


def registry_command(scope: AutoStartScope) -> str | None:
    hive = _registry_hive(scope)
    try:
        with winreg.OpenKey(hive, RUN_KEY, 0, winreg.KEY_READ) as handle:
            value, _ = winreg.QueryValueEx(handle, APP_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def install_registry_run(scope: AutoStartScope) -> AutoStartStatus:
    command = startup_command_line()
    if len(command) > 260:
        raise OSError("Run registry command exceeds 260 characters.")

    hive = _registry_hive(scope)
    with winreg.CreateKeyEx(hive, RUN_KEY, 0, access=winreg.KEY_SET_VALUE) as handle:
        winreg.SetValueEx(handle, APP_NAME, 0, winreg.REG_SZ, command)
    return AutoStartStatus(scope=scope, provider=AutoStartProvider.REGISTRY_RUN, enabled=True)


def remove_registry_run(scope: AutoStartScope) -> None:
    hive = _registry_hive(scope)
    try:
        with winreg.OpenKey(hive, RUN_KEY, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.DeleteValue(handle, APP_NAME)
    except FileNotFoundError:
        return
    except OSError:
        return


def _task_command() -> list[str]:
    return [
        "schtasks",
        "/create",
        "/sc",
        "ONLOGON",
        "/tn",
        TASK_NAME,
        "/tr",
        startup_command_line(),
        "/f",
    ]


def install_scheduled_task(scope: AutoStartScope) -> AutoStartStatus:
    command = _task_command()
    if scope == AutoStartScope.CURRENT_USER:
        command.extend(["/rl", "LIMITED"])
    subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603
    return AutoStartStatus(scope=scope, provider=AutoStartProvider.SCHEDULED_TASK, enabled=True)


def remove_scheduled_task() -> None:
    subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        check=False,
        capture_output=True,
        text=True,
    )  # noqa: S603


def detect_autostart(scope: AutoStartScope) -> AutoStartStatus:
    if registry_command(scope):
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.REGISTRY_RUN, enabled=True)

    task_query = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME],
        check=False,
        capture_output=True,
        text=True,
    )  # noqa: S603
    if task_query.returncode == 0:
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.SCHEDULED_TASK, enabled=True)
    return AutoStartStatus(scope=scope, provider=None, enabled=False)


def apply_autostart(scope: AutoStartScope) -> AutoStartStatus:
    if scope == AutoStartScope.DISABLED:
        remove_registry_run(AutoStartScope.CURRENT_USER)
        remove_registry_run(AutoStartScope.ALL_USERS)
        remove_scheduled_task()
        return AutoStartStatus(scope=scope, provider=None, enabled=False)

    try:
        return install_registry_run(scope)
    except OSError:
        return install_scheduled_task(scope)

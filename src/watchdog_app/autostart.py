from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
import winreg
import xml.etree.ElementTree as ET

from .models import APP_NAME, AutoStartProvider, AutoStartScope
from .runtime import runtime_base_dir, startup_command, startup_command_line


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME_CURRENT_USER = f"{APP_NAME} AutoStart (Current User)"
TASK_NAME_ALL_USERS = f"{APP_NAME} AutoStart (All Users)"
USERS_GROUP_SID = "S-1-5-32-545"
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


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


def _task_name(scope: AutoStartScope) -> str:
    if scope == AutoStartScope.ALL_USERS:
        return TASK_NAME_ALL_USERS
    return TASK_NAME_CURRENT_USER


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
    except OSError as exc:
        if registry_command(scope):
            raise OSError(f"Failed to remove Run registry entry for {scope.value}.") from exc
        return


def _scheduled_task_command(scope: AutoStartScope) -> list[str]:
    return [
        "schtasks",
        "/create",
        "/sc",
        "ONLOGON",
        "/tn",
        _task_name(scope),
        "/tr",
        startup_command_line(),
        "/f",
    ]


def _all_users_task_xml() -> str:
    ET.register_namespace("", TASK_XML_NAMESPACE)
    task = ET.Element(f"{{{TASK_XML_NAMESPACE}}}Task", version="1.2")

    principals = ET.SubElement(task, f"{{{TASK_XML_NAMESPACE}}}Principals")
    principal = ET.SubElement(principals, f"{{{TASK_XML_NAMESPACE}}}Principal", id="Author")
    ET.SubElement(principal, f"{{{TASK_XML_NAMESPACE}}}GroupId").text = USERS_GROUP_SID
    ET.SubElement(principal, f"{{{TASK_XML_NAMESPACE}}}LogonType").text = "Group"
    ET.SubElement(principal, f"{{{TASK_XML_NAMESPACE}}}RunLevel").text = "LeastPrivilege"

    triggers = ET.SubElement(task, f"{{{TASK_XML_NAMESPACE}}}Triggers")
    logon_trigger = ET.SubElement(triggers, f"{{{TASK_XML_NAMESPACE}}}LogonTrigger")
    ET.SubElement(logon_trigger, f"{{{TASK_XML_NAMESPACE}}}Enabled").text = "true"

    settings = ET.SubElement(task, f"{{{TASK_XML_NAMESPACE}}}Settings")
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}AllowHardTerminate").text = "true"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}StartWhenAvailable").text = "true"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}RunOnlyIfNetworkAvailable").text = "false"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}AllowStartOnDemand").text = "true"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}Enabled").text = "true"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}Hidden").text = "false"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}RunOnlyIfIdle").text = "false"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}WakeToRun").text = "false"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}ExecutionTimeLimit").text = "PT0S"
    ET.SubElement(settings, f"{{{TASK_XML_NAMESPACE}}}Priority").text = "7"

    actions = ET.SubElement(task, f"{{{TASK_XML_NAMESPACE}}}Actions", Context="Author")
    exec_action = ET.SubElement(actions, f"{{{TASK_XML_NAMESPACE}}}Exec")
    command = startup_command()
    ET.SubElement(exec_action, f"{{{TASK_XML_NAMESPACE}}}Command").text = command[0]
    if len(command) > 1:
        ET.SubElement(exec_action, f"{{{TASK_XML_NAMESPACE}}}Arguments").text = subprocess.list2cmdline(
            command[1:]
        )
    ET.SubElement(exec_action, f"{{{TASK_XML_NAMESPACE}}}WorkingDirectory").text = str(runtime_base_dir())

    return ET.tostring(task, encoding="unicode", xml_declaration=True)


def install_scheduled_task(scope: AutoStartScope) -> AutoStartStatus:
    if scope == AutoStartScope.ALL_USERS:
        xml_text = _all_users_task_xml()
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".xml", delete=False) as handle:
            handle.write(xml_text)
            xml_path = Path(handle.name)
        try:
            subprocess.run(
                ["schtasks", "/create", "/tn", _task_name(scope), "/xml", str(xml_path), "/f"],
                check=True,
                capture_output=True,
                text=True,
            )  # noqa: S603
        finally:
            xml_path.unlink(missing_ok=True)
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.SCHEDULED_TASK, enabled=True)

    command = _scheduled_task_command(scope)
    if scope == AutoStartScope.CURRENT_USER:
        command.extend(["/rl", "LIMITED"])
    subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603
    return AutoStartStatus(scope=scope, provider=AutoStartProvider.SCHEDULED_TASK, enabled=True)


def remove_scheduled_task(scope: AutoStartScope | None = None) -> None:
    scopes = (
        [AutoStartScope.CURRENT_USER, AutoStartScope.ALL_USERS]
        if scope is None
        else [scope]
    )
    for scheduled_scope in scopes:
        query = subprocess.run(
            ["schtasks", "/query", "/tn", _task_name(scheduled_scope)],
            check=False,
            capture_output=True,
            text=True,
        )  # noqa: S603
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", _task_name(scheduled_scope), "/f"],
            check=False,
            capture_output=True,
            text=True,
        )  # noqa: S603
        if query.returncode == 0 and result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise OSError(f"Failed to remove scheduled task for {scheduled_scope.value}: {detail}")


def detect_autostart(scope: AutoStartScope) -> AutoStartStatus:
    if registry_command(scope):
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.REGISTRY_RUN, enabled=True)

    task_query = subprocess.run(
        ["schtasks", "/query", "/tn", _task_name(scope)],
        check=False,
        capture_output=True,
        text=True,
    )  # noqa: S603
    if task_query.returncode == 0:
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.SCHEDULED_TASK, enabled=True)
    return AutoStartStatus(scope=scope, provider=None, enabled=False)


def apply_autostart(scope: AutoStartScope) -> AutoStartStatus:
    remove_registry_run(AutoStartScope.CURRENT_USER)
    remove_registry_run(AutoStartScope.ALL_USERS)
    remove_scheduled_task()

    if scope == AutoStartScope.DISABLED:
        return AutoStartStatus(scope=scope, provider=None, enabled=False)

    try:
        return install_registry_run(scope)
    except OSError:
        return install_scheduled_task(scope)

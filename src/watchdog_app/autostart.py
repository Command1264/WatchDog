from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path
import logging
import os
import subprocess
import tempfile
import winreg
import xml.etree.ElementTree as ET

from .models import (
    APP_NAME,
    AutoStartProvider,
    AutoStartScope,
    normalize_path_text,
)
from .runtime import runtime_base_dir, startup_command


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME_CURRENT_USER = f"{APP_NAME} AutoStart (Current User)"
TASK_NAME_ALL_USERS = f"{APP_NAME} AutoStart (All Users)"
STARTUP_SHORTCUT_NAME = f"{APP_NAME}.lnk"
USERS_GROUP_SID = "S-1-5-32-545"
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AutoStartStatus:
    scope: AutoStartScope
    provider: AutoStartProvider | None
    enabled: bool
    message: str = ""


def _normalize_scope(scope: AutoStartScope | str) -> AutoStartScope:
    if isinstance(scope, AutoStartScope):
        return scope
    return AutoStartScope(str(scope))


def _decode_command_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", "cp950", "mbcs", "latin1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _run_schtasks(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, check=False, capture_output=True)  # noqa: S603
    stdout = _decode_command_output(result.stdout)
    stderr = _decode_command_output(result.stderr)
    normalized = subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if check and normalized.returncode != 0:
        raise subprocess.CalledProcessError(
            normalized.returncode,
            normalized.args,
            output=normalized.stdout,
            stderr=normalized.stderr,
        )
    return normalized


def _hidden_subprocess_options() -> dict[str, object]:
    options: dict[str, object] = {}
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        options["startupinfo"] = startupinfo
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        options["creationflags"] = create_no_window
    return options


def _run_powershell(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=False,
        capture_output=True,
        **_hidden_subprocess_options(),
    )
    stdout = _decode_command_output(result.stdout)
    stderr = _decode_command_output(result.stderr)
    normalized = subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if check and normalized.returncode != 0:
        raise subprocess.CalledProcessError(
            normalized.returncode,
            normalized.args,
            output=normalized.stdout,
            stderr=normalized.stderr,
        )
    return normalized


def _format_schtasks_failure(exc: subprocess.CalledProcessError) -> str:
    detail = (exc.stderr or exc.output or "").strip()
    if detail:
        return detail
    return f"schtasks exited with code {exc.returncode}"


def _is_access_denied_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) == 5:
        return True
    message = str(exc)
    return "Access is denied" in message or "存取被拒" in message


def _registry_hive(scope: AutoStartScope | str):
    scope = _normalize_scope(scope)
    if scope == AutoStartScope.CURRENT_USER:
        return winreg.HKEY_CURRENT_USER
    return winreg.HKEY_LOCAL_MACHINE


def _task_name(scope: AutoStartScope | str) -> str:
    scope = _normalize_scope(scope)
    if scope == AutoStartScope.ALL_USERS:
        return TASK_NAME_ALL_USERS
    return TASK_NAME_CURRENT_USER


def _native_path_text(value: str | Path) -> str:
    return os.path.normpath(str(value).strip())


def _is_path_argument(value: str) -> bool:
    return ":/" in value or ":\\" in value or value.startswith(("\\\\", "//"))


def _startup_command_for_windows_entry() -> list[str]:
    command = startup_command()
    if not command:
        return command
    return [
        _native_path_text(part) if index == 0 or _is_path_argument(part) else part
        for index, part in enumerate(command)
    ]


def _startup_command_line_for_windows_entry() -> str:
    return subprocess.list2cmdline(_startup_command_for_windows_entry())


def _startup_folder(scope: AutoStartScope | str) -> Path:
    scope = _normalize_scope(scope)
    if scope == AutoStartScope.CURRENT_USER:
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif scope == AutoStartScope.ALL_USERS:
        root = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    else:
        raise ValueError(f"Unsupported startup folder scope: {scope.value}")
    return root / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_shortcut_path(scope: AutoStartScope | str) -> Path:
    return _startup_folder(scope) / STARTUP_SHORTCUT_NAME


def startup_folder_shortcut_exists(scope: AutoStartScope | str) -> bool:
    return _startup_shortcut_path(scope).exists()


def _create_startup_shortcut(shortcut_path: Path, command: list[str], working_dir: Path) -> None:
    payload = {
        "ShortcutPath": _native_path_text(shortcut_path),
        "TargetPath": _native_path_text(command[0]),
        "Arguments": subprocess.list2cmdline(command[1:]) if len(command) > 1 else "",
        "WorkingDirectory": _native_path_text(working_dir),
        "IconLocation": _native_path_text(command[0]),
        "Description": f"{APP_NAME} 自動啟動",
    }
    encoded_payload = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
$json = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{encoded_payload}'))
$data = $json | ConvertFrom-Json
$folder = Split-Path -Parent $data.ShortcutPath
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($data.ShortcutPath)
$shortcut.TargetPath = $data.TargetPath
$shortcut.Arguments = $data.Arguments
$shortcut.WorkingDirectory = $data.WorkingDirectory
$shortcut.IconLocation = $data.IconLocation
$shortcut.Description = $data.Description
$shortcut.Save()
"""
    try:
        _run_powershell(script, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.output or "").strip()
        if not detail:
            detail = f"powershell exited with code {exc.returncode}"
        raise OSError(f"Failed to create startup shortcut: {detail}") from exc


def registry_command(scope: AutoStartScope | str) -> str | None:
    scope = _normalize_scope(scope)
    hive = _registry_hive(scope)
    try:
        with winreg.OpenKey(hive, RUN_KEY, 0, winreg.KEY_READ) as handle:
            value, _ = winreg.QueryValueEx(handle, APP_NAME)
            command = str(value)
            desired = _startup_command_line_for_windows_entry()
            if command != desired:
                try:
                    with winreg.CreateKeyEx(hive, RUN_KEY, 0, access=winreg.KEY_SET_VALUE) as writable:
                        winreg.SetValueEx(writable, APP_NAME, 0, winreg.REG_SZ, desired)
                    logger.info("Updated autostart registry command. scope=%s", scope.value)
                except OSError as exc:
                    logger.warning(
                        "Failed to rewrite legacy autostart registry command. scope=%s error=%s",
                        scope.value,
                        exc,
                    )
                    return command
            return desired
    except FileNotFoundError:
        return None
    except OSError:
        return None


def install_registry_run(scope: AutoStartScope | str) -> AutoStartStatus:
    scope = _normalize_scope(scope)
    command = _startup_command_line_for_windows_entry()
    if len(command) > 260:
        raise OSError("Run registry command exceeds 260 characters.")

    hive = _registry_hive(scope)
    with winreg.CreateKeyEx(hive, RUN_KEY, 0, access=winreg.KEY_SET_VALUE) as handle:
        winreg.SetValueEx(handle, APP_NAME, 0, winreg.REG_SZ, command)
    return AutoStartStatus(scope=scope, provider=AutoStartProvider.REGISTRY_RUN, enabled=True)


def remove_registry_run(scope: AutoStartScope | str) -> None:
    scope = _normalize_scope(scope)
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


def _scheduled_task_command(scope: AutoStartScope | str) -> list[str]:
    scope = _normalize_scope(scope)
    return [
        "schtasks",
        "/create",
        "/sc",
        "ONLOGON",
        "/tn",
        _task_name(scope),
        "/tr",
        _startup_command_line_for_windows_entry(),
        "/f",
    ]


def _build_all_users_task_xml_tree() -> ET.Element:
    ET.register_namespace("", TASK_XML_NAMESPACE)
    task = ET.Element(f"{{{TASK_XML_NAMESPACE}}}Task", version="1.2")

    principals = ET.SubElement(task, f"{{{TASK_XML_NAMESPACE}}}Principals")
    principal = ET.SubElement(principals, f"{{{TASK_XML_NAMESPACE}}}Principal", id="Author")
    ET.SubElement(principal, f"{{{TASK_XML_NAMESPACE}}}GroupId").text = USERS_GROUP_SID
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
    command = _startup_command_for_windows_entry()
    ET.SubElement(exec_action, f"{{{TASK_XML_NAMESPACE}}}Command").text = command[0]
    if len(command) > 1:
        ET.SubElement(exec_action, f"{{{TASK_XML_NAMESPACE}}}Arguments").text = subprocess.list2cmdline(
            command[1:]
        )
    ET.SubElement(exec_action, f"{{{TASK_XML_NAMESPACE}}}WorkingDirectory").text = _native_path_text(
        runtime_base_dir()
    )

    return task


def _all_users_task_xml() -> str:
    return ET.tostring(_build_all_users_task_xml_tree(), encoding="unicode")


def _all_users_task_xml_bytes() -> bytes:
    return ET.tostring(
        _build_all_users_task_xml_tree(),
        encoding="utf-16",
        xml_declaration=True,
    )


def install_scheduled_task(scope: AutoStartScope | str) -> AutoStartStatus:
    scope = _normalize_scope(scope)
    if scope == AutoStartScope.ALL_USERS:
        xml_bytes = _all_users_task_xml_bytes()
        with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as handle:
            handle.write(xml_bytes)
            xml_path = Path(handle.name)
        try:
            try:
                _run_schtasks(
                    ["schtasks", "/create", "/tn", _task_name(scope), "/xml", str(xml_path), "/f"],
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                raise OSError(
                    f"Failed to create scheduled task for {scope.value}: {_format_schtasks_failure(exc)}"
                ) from exc
        finally:
            xml_path.unlink(missing_ok=True)
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.SCHEDULED_TASK, enabled=True)

    command = _scheduled_task_command(scope)
    if scope == AutoStartScope.CURRENT_USER:
        command.extend(["/rl", "LIMITED"])
    try:
        _run_schtasks(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise OSError(f"Failed to create scheduled task for {scope.value}: {_format_schtasks_failure(exc)}") from exc
    return AutoStartStatus(scope=scope, provider=AutoStartProvider.SCHEDULED_TASK, enabled=True)


def install_startup_folder_shortcut(scope: AutoStartScope | str) -> AutoStartStatus:
    scope = _normalize_scope(scope)
    shortcut_path = _startup_shortcut_path(scope)
    _create_startup_shortcut(
        shortcut_path,
        _startup_command_for_windows_entry(),
        runtime_base_dir(),
    )
    if not shortcut_path.exists():
        raise OSError(f"Startup shortcut was not created: {normalize_path_text(shortcut_path)}")
    return AutoStartStatus(scope=scope, provider=AutoStartProvider.STARTUP_FOLDER, enabled=True)


def remove_scheduled_task(scope: AutoStartScope | str | None = None) -> None:
    scopes = (
        [AutoStartScope.CURRENT_USER, AutoStartScope.ALL_USERS]
        if scope is None
        else [_normalize_scope(scope)]
    )
    for scheduled_scope in scopes:
        query = _run_schtasks(
            ["schtasks", "/query", "/tn", _task_name(scheduled_scope)],
            check=False,
        )
        result = _run_schtasks(
            ["schtasks", "/delete", "/tn", _task_name(scheduled_scope), "/f"],
            check=False,
        )
        if query.returncode == 0 and result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise OSError(f"Failed to remove scheduled task for {scheduled_scope.value}: {detail}")


def remove_startup_folder_shortcut(scope: AutoStartScope | str | None = None) -> None:
    scopes = (
        [AutoStartScope.CURRENT_USER, AutoStartScope.ALL_USERS]
        if scope is None
        else [_normalize_scope(scope)]
    )
    for startup_scope in scopes:
        shortcut_path = _startup_shortcut_path(startup_scope)
        try:
            shortcut_path.unlink(missing_ok=True)
        except OSError as exc:
            if shortcut_path.exists():
                raise OSError(
                    f"Failed to remove startup shortcut for {startup_scope.value}: "
                    f"{normalize_path_text(shortcut_path)}"
                ) from exc


def detect_autostart(scope: AutoStartScope | str) -> AutoStartStatus:
    scope = _normalize_scope(scope)
    if registry_command(scope):
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.REGISTRY_RUN, enabled=True)

    task_query = _run_schtasks(
        ["schtasks", "/query", "/tn", _task_name(scope)],
        check=False,
    )
    if task_query.returncode == 0:
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.SCHEDULED_TASK, enabled=True)
    if startup_folder_shortcut_exists(scope):
        return AutoStartStatus(scope=scope, provider=AutoStartProvider.STARTUP_FOLDER, enabled=True)
    return AutoStartStatus(scope=scope, provider=None, enabled=False)


def apply_autostart(scope: AutoStartScope | str) -> AutoStartStatus:
    scope = _normalize_scope(scope)
    remove_registry_run(AutoStartScope.CURRENT_USER)
    remove_registry_run(AutoStartScope.ALL_USERS)
    remove_scheduled_task()
    remove_startup_folder_shortcut()

    if scope == AutoStartScope.DISABLED:
        logger.info("Autostart disabled.")
        return AutoStartStatus(scope=scope, provider=None, enabled=False)

    try:
        status = install_registry_run(scope)
        logger.info("Autostart enabled via registry run entry. scope=%s", scope.value)
        return status
    except OSError as registry_exc:
        logger.warning(
            "Registry autostart unavailable; trying scheduled task fallback. scope=%s error=%s",
            scope.value,
            registry_exc,
        )
        try:
            status = install_scheduled_task(scope)
            logger.info("Autostart enabled via scheduled task. scope=%s", scope.value)
            return status
        except OSError as task_exc:
            logger.warning(
                "Scheduled task autostart unavailable; trying startup folder fallback. "
                "scope=%s error=%s",
                scope.value,
                task_exc,
            )
            try:
                status = install_startup_folder_shortcut(scope)
                logger.info(
                    "Autostart enabled via startup folder shortcut. scope=%s shortcut=%s",
                    scope.value,
                    normalize_path_text(_startup_shortcut_path(scope)),
                )
                return status
            except OSError as shortcut_exc:
                if scope == AutoStartScope.ALL_USERS and (
                    _is_access_denied_error(registry_exc)
                    or _is_access_denied_error(task_exc)
                    or _is_access_denied_error(shortcut_exc)
                ):
                    logger.warning("All-users autostart requires administrative privileges.")
                    raise OSError(
                        "所有使用者自動啟動需要系統管理員權限。請改用「目前使用者」，"
                        "或以系統管理員身分執行 WatchDog 後再儲存。"
                    ) from shortcut_exc
                logger.error(
                    "Failed to apply autostart. scope=%s registry_error=%s "
                    "scheduled_task_error=%s startup_folder_error=%s",
                    scope.value,
                    registry_exc,
                    task_exc,
                    shortcut_exc,
                )
                raise

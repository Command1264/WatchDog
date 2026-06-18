from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from watchdog_app import autostart
from watchdog_app.models import AutoStartProvider, AutoStartScope


def test_apply_autostart_falls_back_to_scheduled_task(monkeypatch: object) -> None:
    calls: list[str] = []

    monkeypatch.setattr(autostart, "remove_registry_run", lambda scope: None)
    monkeypatch.setattr(autostart, "remove_scheduled_task", lambda scope=None: None)
    monkeypatch.setattr(autostart, "remove_startup_folder_shortcut", lambda scope=None: None)

    def _fail_registry(scope: AutoStartScope):
        calls.append("registry")
        raise OSError("denied")

    def _ok_task(scope: AutoStartScope):
        calls.append("task")
        return autostart.AutoStartStatus(
            scope=scope,
            provider=AutoStartProvider.SCHEDULED_TASK,
            enabled=True,
        )

    monkeypatch.setattr(autostart, "install_registry_run", _fail_registry)
    monkeypatch.setattr(autostart, "install_scheduled_task", _ok_task)

    status = autostart.apply_autostart(AutoStartScope.CURRENT_USER)

    assert status.provider == AutoStartProvider.SCHEDULED_TASK
    assert calls == ["registry", "task"]


def test_apply_autostart_falls_back_to_startup_folder(monkeypatch: object) -> None:
    calls: list[str] = []

    monkeypatch.setattr(autostart, "remove_registry_run", lambda scope: None)
    monkeypatch.setattr(autostart, "remove_scheduled_task", lambda scope=None: None)
    monkeypatch.setattr(autostart, "remove_startup_folder_shortcut", lambda scope=None: None)

    def _fail_registry(scope: AutoStartScope):
        calls.append("registry")
        raise OSError("registry unavailable")

    def _fail_task(scope: AutoStartScope):
        calls.append("task")
        raise OSError("task unavailable")

    def _ok_shortcut(scope: AutoStartScope):
        calls.append("shortcut")
        return autostart.AutoStartStatus(
            scope=scope,
            provider=AutoStartProvider.STARTUP_FOLDER,
            enabled=True,
        )

    monkeypatch.setattr(autostart, "install_registry_run", _fail_registry)
    monkeypatch.setattr(autostart, "install_scheduled_task", _fail_task)
    monkeypatch.setattr(autostart, "install_startup_folder_shortcut", _ok_shortcut)

    status = autostart.apply_autostart(AutoStartScope.CURRENT_USER)

    assert status.provider == AutoStartProvider.STARTUP_FOLDER
    assert calls == ["registry", "task", "shortcut"]


def test_apply_autostart_clears_existing_entries_before_reinstall(monkeypatch: object) -> None:
    calls: list[tuple[str, object | None]] = []

    monkeypatch.setattr(
        autostart,
        "remove_registry_run",
        lambda scope: calls.append(("remove_registry", scope)),
    )
    monkeypatch.setattr(
        autostart,
        "remove_scheduled_task",
        lambda scope=None: calls.append(("remove_task", scope)),
    )
    monkeypatch.setattr(
        autostart,
        "remove_startup_folder_shortcut",
        lambda scope=None: calls.append(("remove_shortcut", scope)),
    )

    def _install_registry(scope: AutoStartScope):
        calls.append(("install_registry", scope))
        return autostart.AutoStartStatus(
            scope=scope,
            provider=AutoStartProvider.REGISTRY_RUN,
            enabled=True,
        )

    monkeypatch.setattr(autostart, "install_registry_run", _install_registry)

    status = autostart.apply_autostart(AutoStartScope.ALL_USERS)

    assert status.provider == AutoStartProvider.REGISTRY_RUN
    assert calls == [
        ("remove_registry", AutoStartScope.CURRENT_USER),
        ("remove_registry", AutoStartScope.ALL_USERS),
        ("remove_task", None),
        ("remove_shortcut", None),
        ("install_registry", AutoStartScope.ALL_USERS),
    ]


def test_apply_autostart_raises_when_existing_entries_cannot_be_cleared(monkeypatch: object) -> None:
    monkeypatch.setattr(autostart, "registry_command", lambda scope: "enabled" if scope == AutoStartScope.ALL_USERS else None)
    monkeypatch.setattr(
        autostart,
        "remove_registry_run",
        lambda scope: (_ for _ in ()).throw(OSError("denied")) if scope == AutoStartScope.ALL_USERS else None,
    )
    monkeypatch.setattr(autostart, "remove_scheduled_task", lambda scope=None: None)

    with pytest.raises(OSError, match="denied"):
        autostart.apply_autostart(AutoStartScope.CURRENT_USER)


def test_apply_autostart_all_users_reports_admin_requirement_on_access_denied(monkeypatch: object) -> None:
    monkeypatch.setattr(autostart, "remove_registry_run", lambda scope: None)
    monkeypatch.setattr(autostart, "remove_scheduled_task", lambda scope=None: None)
    monkeypatch.setattr(autostart, "remove_startup_folder_shortcut", lambda scope=None: None)

    def _fail_registry(scope: AutoStartScope):
        raise PermissionError(5, "Access is denied")

    def _fail_task(scope: AutoStartScope):
        raise OSError("ERROR: Access is denied.")

    def _fail_shortcut(scope: AutoStartScope):
        raise OSError("ERROR: Access is denied.")

    monkeypatch.setattr(autostart, "install_registry_run", _fail_registry)
    monkeypatch.setattr(autostart, "install_scheduled_task", _fail_task)
    monkeypatch.setattr(autostart, "install_startup_folder_shortcut", _fail_shortcut)

    with pytest.raises(OSError, match="需要系統管理員權限"):
        autostart.apply_autostart(AutoStartScope.ALL_USERS)


def test_apply_autostart_accepts_string_scope(monkeypatch: object) -> None:
    calls: list[AutoStartScope] = []

    monkeypatch.setattr(autostart, "remove_registry_run", lambda scope: None)
    monkeypatch.setattr(autostart, "remove_scheduled_task", lambda scope=None: None)
    monkeypatch.setattr(autostart, "remove_startup_folder_shortcut", lambda scope=None: None)

    def _install_registry(scope):
        calls.append(scope)
        return autostart.AutoStartStatus(
            scope=scope,
            provider=AutoStartProvider.REGISTRY_RUN,
            enabled=True,
        )

    monkeypatch.setattr(autostart, "install_registry_run", _install_registry)

    status = autostart.apply_autostart("all_users")

    assert status.scope == AutoStartScope.ALL_USERS
    assert calls == [AutoStartScope.ALL_USERS]


def test_detect_autostart_queries_scope_specific_scheduled_task_names(monkeypatch: object) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(autostart, "registry_command", lambda scope: None)

    def _run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(autostart.subprocess, "run", _run)

    current = autostart.detect_autostart(AutoStartScope.CURRENT_USER)
    all_users = autostart.detect_autostart(AutoStartScope.ALL_USERS)

    assert current.provider == AutoStartProvider.SCHEDULED_TASK
    assert all_users.provider == AutoStartProvider.SCHEDULED_TASK
    assert [command[3] for command in commands] == [
        autostart.TASK_NAME_CURRENT_USER,
        autostart.TASK_NAME_ALL_USERS,
    ]


def test_detect_autostart_returns_startup_folder_provider(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(autostart, "registry_command", lambda scope: None)

    def _run_schtasks(command, *, check: bool):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr(autostart, "_run_schtasks", _run_schtasks)
    shortcut_path = autostart._startup_shortcut_path(AutoStartScope.CURRENT_USER)
    shortcut_path.parent.mkdir(parents=True)
    shortcut_path.write_text("shortcut", encoding="utf-8")

    status = autostart.detect_autostart(AutoStartScope.CURRENT_USER)

    assert status.provider == AutoStartProvider.STARTUP_FOLDER


def test_install_startup_folder_shortcut_creates_shortcut(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    scripts: list[str] = []

    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(
        autostart,
        "startup_command",
        lambda: ["C:/Watch Dog/WatchDog.exe", "--child-app"],
    )
    monkeypatch.setattr(autostart, "runtime_base_dir", lambda: tmp_path / "app")

    def _run_powershell(script: str, *, check: bool):
        scripts.append(script)
        shortcut_path = autostart._startup_shortcut_path(AutoStartScope.CURRENT_USER)
        shortcut_path.parent.mkdir(parents=True)
        shortcut_path.write_text("shortcut", encoding="utf-8")
        return subprocess.CompletedProcess(["powershell.exe"], 0, stdout="", stderr="")

    monkeypatch.setattr(autostart, "_run_powershell", _run_powershell)

    status = autostart.install_startup_folder_shortcut(AutoStartScope.CURRENT_USER)

    assert status.provider == AutoStartProvider.STARTUP_FOLDER
    assert autostart._startup_shortcut_path(AutoStartScope.CURRENT_USER).exists()
    assert "FromBase64String" in scripts[0]


def test_windows_autostart_command_line_uses_native_executable_path(monkeypatch: object) -> None:
    monkeypatch.setattr(
        autostart,
        "startup_command",
        lambda: ["C:/Watch Dog/pythonw.exe", "C:/Watch Dog/src/watchdog_app/main.py"],
    )

    command = autostart._startup_command_line_for_windows_entry()

    assert command == subprocess.list2cmdline(
        [
            autostart._native_path_text("C:/Watch Dog/pythonw.exe"),
            autostart._native_path_text("C:/Watch Dog/src/watchdog_app/main.py"),
        ]
    )


def test_remove_startup_folder_shortcut_without_scope_removes_both_shortcuts(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    current_shortcut = autostart._startup_shortcut_path(AutoStartScope.CURRENT_USER)
    all_users_shortcut = autostart._startup_shortcut_path(AutoStartScope.ALL_USERS)
    current_shortcut.parent.mkdir(parents=True)
    all_users_shortcut.parent.mkdir(parents=True)
    current_shortcut.write_text("shortcut", encoding="utf-8")
    all_users_shortcut.write_text("shortcut", encoding="utf-8")

    autostart.remove_startup_folder_shortcut()

    assert not current_shortcut.exists()
    assert not all_users_shortcut.exists()


def test_install_scheduled_task_rejects_all_users_scope() -> None:
    xml_text = autostart._all_users_task_xml()

    assert autostart.USERS_GROUP_SID in xml_text
    assert "<LogonType>Group</LogonType>" not in xml_text
    assert "<LogonTrigger>" in xml_text


def test_install_scheduled_task_all_users_uses_xml_import(monkeypatch: object, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    written_payloads: list[bytes] = []

    class _FakeFile:
        def __init__(self) -> None:
            self.name = str(tmp_path / "task.xml")
            self.path = Path(self.name)

        def write(self, content) -> None:
            if isinstance(content, bytes):
                written_payloads.append(content)
                self.path.write_bytes(content)
            else:
                self.path.write_text(content, encoding="utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(autostart.tempfile, "NamedTemporaryFile", lambda *args, **kwargs: _FakeFile())
    monkeypatch.setattr(autostart, "runtime_base_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(autostart, "startup_command", lambda: ["C:/WatchDog/WatchDog.exe", "--child-app"])

    def _run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(autostart.subprocess, "run", _run)

    status = autostart.install_scheduled_task(AutoStartScope.ALL_USERS)

    assert status.provider == AutoStartProvider.SCHEDULED_TASK
    assert commands == [
        [
            "schtasks",
            "/create",
            "/tn",
            autostart.TASK_NAME_ALL_USERS,
            "/xml",
            str(tmp_path / "task.xml"),
            "/f",
        ]
    ]
    assert written_payloads
    assert written_payloads[0].startswith(b"\xff\xfe") or written_payloads[0].startswith(b"\xfe\xff")
    assert not (tmp_path / "task.xml").exists()
    xml_text = written_payloads[0].decode("utf-16")
    assert "<Command>C:\\WatchDog\\WatchDog.exe</Command>" in xml_text


def test_remove_scheduled_task_without_scope_removes_both_task_names(monkeypatch: object) -> None:
    commands: list[list[str]] = []

    def _run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(autostart.subprocess, "run", _run)

    autostart.remove_scheduled_task()

    assert [command[3] for command in commands] == [
        autostart.TASK_NAME_CURRENT_USER,
        autostart.TASK_NAME_CURRENT_USER,
        autostart.TASK_NAME_ALL_USERS,
        autostart.TASK_NAME_ALL_USERS,
    ]


def test_registry_command_rewrites_legacy_separator_style_to_native_path(monkeypatch: object) -> None:
    writes: list[tuple[object, str, int, object, str]] = []

    class _Handle:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(autostart.winreg, "OpenKey", lambda *args, **kwargs: _Handle())
    monkeypatch.setattr(autostart.winreg, "CreateKeyEx", lambda *args, **kwargs: _Handle())
    desired = subprocess.list2cmdline([autostart._native_path_text("C:/Watch Dog/WatchDog.exe")])
    monkeypatch.setattr(autostart, "_startup_command_line_for_windows_entry", lambda: desired)
    monkeypatch.setattr(
        autostart.winreg,
        "QueryValueEx",
        lambda *_args, **_kwargs: ('"C:/Watch Dog/WatchDog.exe"', autostart.winreg.REG_SZ),
    )
    monkeypatch.setattr(
        autostart.winreg,
        "SetValueEx",
        lambda *args: writes.append(args),
    )

    command = autostart.registry_command(AutoStartScope.CURRENT_USER)

    assert command == desired
    assert writes
    assert writes[0][-1] == desired


def test_registry_command_rewrites_legacy_python_console_host(monkeypatch: object) -> None:
    writes: list[tuple[object, str, int, object, str]] = []

    class _Handle:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(autostart.winreg, "OpenKey", lambda *args, **kwargs: _Handle())
    monkeypatch.setattr(autostart.winreg, "CreateKeyEx", lambda *args, **kwargs: _Handle())
    desired = subprocess.list2cmdline(
        [
            autostart._native_path_text("C:/venv/Scripts/pythonw.exe"),
            autostart._native_path_text("C:/Watch Dog/src/watchdog_app/main.py"),
        ]
    )
    monkeypatch.setattr(autostart, "_startup_command_line_for_windows_entry", lambda: desired)
    monkeypatch.setattr(
        autostart.winreg,
        "QueryValueEx",
        lambda *_args, **_kwargs: ('"C:/venv/Scripts/python.exe" C:/Watch Dog/src/watchdog_app/main.py', autostart.winreg.REG_SZ),
    )
    monkeypatch.setattr(
        autostart.winreg,
        "SetValueEx",
        lambda *args: writes.append(args),
    )

    command = autostart.registry_command(AutoStartScope.CURRENT_USER)

    assert command == desired
    assert writes
    assert writes[0][-1] == desired

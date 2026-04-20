from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from watchdog_app import autostart
from watchdog_app.models import AutoStartProvider, AutoStartScope


def test_apply_autostart_falls_back_to_scheduled_task(monkeypatch: object) -> None:
    calls: list[str] = []

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

    def _fail_registry(scope: AutoStartScope):
        raise PermissionError(5, "Access is denied")

    def _fail_task(scope: AutoStartScope):
        raise OSError("ERROR: Access is denied.")

    monkeypatch.setattr(autostart, "install_registry_run", _fail_registry)
    monkeypatch.setattr(autostart, "install_scheduled_task", _fail_task)

    with pytest.raises(OSError, match="需要系統管理員權限"):
        autostart.apply_autostart(AutoStartScope.ALL_USERS)


def test_apply_autostart_accepts_string_scope(monkeypatch: object) -> None:
    calls: list[AutoStartScope] = []

    monkeypatch.setattr(autostart, "remove_registry_run", lambda scope: None)
    monkeypatch.setattr(autostart, "remove_scheduled_task", lambda scope=None: None)

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
    monkeypatch.setattr(
        autostart,
        "startup_command",
        lambda: ["C:/WatchDog/WatchDog.exe", "--child-app"],
    )

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


def test_registry_command_normalizes_legacy_backslashes_and_writes_back(monkeypatch: object) -> None:
    writes: list[tuple[object, str, int, object, str]] = []

    class _Handle:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(autostart.winreg, "OpenKey", lambda *args, **kwargs: _Handle())
    monkeypatch.setattr(autostart.winreg, "CreateKeyEx", lambda *args, **kwargs: _Handle())
    monkeypatch.setattr(autostart, "startup_command_line", lambda: '"C:/Watch Dog/WatchDog.exe"')
    monkeypatch.setattr(
        autostart.winreg,
        "QueryValueEx",
        lambda *_args, **_kwargs: ('"C:\\Watch Dog\\WatchDog.exe"', autostart.winreg.REG_SZ),
    )
    monkeypatch.setattr(
        autostart.winreg,
        "SetValueEx",
        lambda *args: writes.append(args),
    )

    command = autostart.registry_command(AutoStartScope.CURRENT_USER)

    assert command == '"C:/Watch Dog/WatchDog.exe"'
    assert writes
    assert writes[0][-1] == '"C:/Watch Dog/WatchDog.exe"'


def test_registry_command_rewrites_legacy_python_console_host(monkeypatch: object) -> None:
    writes: list[tuple[object, str, int, object, str]] = []

    class _Handle:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(autostart.winreg, "OpenKey", lambda *args, **kwargs: _Handle())
    monkeypatch.setattr(autostart.winreg, "CreateKeyEx", lambda *args, **kwargs: _Handle())
    monkeypatch.setattr(
        autostart,
        "startup_command_line",
        lambda: '"C:/venv/Scripts/pythonw.exe" -m watchdog_app.main',
    )
    monkeypatch.setattr(
        autostart.winreg,
        "QueryValueEx",
        lambda *_args, **_kwargs: ('"C:/venv/Scripts/python.exe" -m watchdog_app.main', autostart.winreg.REG_SZ),
    )
    monkeypatch.setattr(
        autostart.winreg,
        "SetValueEx",
        lambda *args: writes.append(args),
    )

    command = autostart.registry_command(AutoStartScope.CURRENT_USER)

    assert command == '"C:/venv/Scripts/pythonw.exe" -m watchdog_app.main'
    assert writes
    assert writes[0][-1] == '"C:/venv/Scripts/pythonw.exe" -m watchdog_app.main'

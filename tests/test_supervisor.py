from __future__ import annotations

import subprocess

from watchdog_app.models import ExitReason
from watchdog_app.supervisor import Supervisor


def test_supervisor_restarts_until_non_restart_reason(monkeypatch: object) -> None:
    calls = {"count": 0}
    kwargs_seen: list[dict[str, object]] = []

    class _Completed:
        def __init__(self, code: int) -> None:
            self.returncode = code

    def _run(*args, **kwargs):
        kwargs_seen.append(kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            return _Completed(ExitReason.CRITICAL_EXCEPTION.value)
        return _Completed(ExitReason.USER_EXIT.value)

    monkeypatch.setattr("watchdog_app.supervisor.subprocess.run", _run)
    monkeypatch.setattr("watchdog_app.supervisor.time.sleep", lambda _: None)
    monkeypatch.setattr("watchdog_app.supervisor.child_command", lambda: ["D:/Apps/WatchDog.exe", "--child-app"])

    code = Supervisor().run()

    assert code == ExitReason.USER_EXIT.value
    assert calls["count"] == 2
    assert all(item["check"] is False for item in kwargs_seen)
    assert all("startupinfo" not in item for item in kwargs_seen)
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert all("creationflags" not in item for item in kwargs_seen)


def test_supervisor_hides_child_window_only_for_console_hosts(monkeypatch: object) -> None:
    monkeypatch.setattr("watchdog_app.supervisor.os.name", "nt")

    kwargs = Supervisor._child_run_kwargs(["C:/Python312/python.exe", "-m", "watchdog_app.main", "--child-app"])

    assert kwargs["check"] is False
    assert "startupinfo" in kwargs
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW


def test_supervisor_does_not_hide_frozen_watchdog_exe(monkeypatch: object) -> None:
    monkeypatch.setattr("watchdog_app.supervisor.os.name", "nt")

    kwargs = Supervisor._child_run_kwargs(["D:/Apps/WatchDog.exe", "--child-app"])

    assert kwargs == {"check": False}

from __future__ import annotations

import subprocess

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

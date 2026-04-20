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

    code = Supervisor().run()

    assert code == ExitReason.USER_EXIT.value
    assert calls["count"] == 2
    assert all(item["check"] is False for item in kwargs_seen)
    assert all("startupinfo" in item for item in kwargs_seen)
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert all(item.get("creationflags") == subprocess.CREATE_NO_WINDOW for item in kwargs_seen)

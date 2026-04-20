from __future__ import annotations

from pathlib import Path

from watchdog_app import runtime


def test_startup_command_prefers_pythonw_when_available(monkeypatch, tmp_path: Path) -> None:
    python_exe = tmp_path / "python.exe"
    pythonw_exe = tmp_path / "pythonw.exe"
    python_exe.write_text("", encoding="utf-8")
    pythonw_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime.sys, "executable", str(python_exe))

    command = runtime.startup_command()

    assert command == [runtime.normalize_path_text(pythonw_exe), "-m", "watchdog_app.main"]


def test_startup_command_falls_back_to_python_when_pythonw_missing(monkeypatch, tmp_path: Path) -> None:
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime.sys, "executable", str(python_exe))

    command = runtime.startup_command()

    assert command == [runtime.normalize_path_text(python_exe), "-m", "watchdog_app.main"]

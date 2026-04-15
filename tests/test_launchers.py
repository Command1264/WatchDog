from __future__ import annotations

from pathlib import Path
import sys

from watchdog_app.launchers import build_command, infer_process_match
from watchdog_app.models import LaunchKind, LaunchSpec


def test_python_launch_uses_current_interpreter() -> None:
    command = build_command(
        LaunchSpec(path="C:/demo.py", args=["--ok"], kind=LaunchKind.PYTHON)
    )

    assert command == [sys.executable, "C:/demo.py", "--ok"]


def test_auto_detects_cmd_files() -> None:
    command = build_command(LaunchSpec(path="C:/demo.cmd", args=["--ok"], kind=LaunchKind.AUTO))

    assert command[:3] == ["cmd.exe", "/c", "C:/demo.cmd"]


def test_infer_process_match_for_executable_path() -> None:
    inferred = infer_process_match("C:/tools/demo.exe")

    assert inferred.process_name == "demo.exe"
    assert Path(inferred.executable_path) == Path("C:/tools/demo.exe")
    assert inferred.note == ""


def test_infer_process_match_for_batch_file_uses_cmd_host() -> None:
    inferred = infer_process_match("C:/tools/demo.bat")

    assert inferred.process_name == "cmd.exe"
    assert Path(inferred.executable_path).name.casefold() == "cmd.exe"
    assert "cmd.exe" in inferred.note

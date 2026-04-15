from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys

from .models import ConfigValidationError, LaunchKind, LaunchSpec


@dataclass(slots=True)
class LaunchResult:
    pid: int
    command: list[str]
    working_dir: str


@dataclass(slots=True)
class ProcessMatchInference:
    process_name: str
    executable_path: str
    note: str = ""


def detect_launch_kind(path: str) -> LaunchKind:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return LaunchKind.PYTHON
    if suffix in {".ps1"}:
        return LaunchKind.POWERSHELL
    if suffix in {".cmd", ".bat"}:
        return LaunchKind.CMD
    return LaunchKind.EXE


def infer_process_match(path: str) -> ProcessMatchInference:
    target_path = Path(path).expanduser()
    launch_kind = detect_launch_kind(str(target_path))

    if launch_kind == LaunchKind.EXE:
        return ProcessMatchInference(
            process_name=target_path.name,
            executable_path=str(target_path),
        )

    if launch_kind == LaunchKind.CMD:
        host = shutil.which("cmd.exe") or "cmd.exe"
        return ProcessMatchInference(
            process_name=Path(host).name,
            executable_path=host,
            note="批次檔實際會由 cmd.exe 執行，名稱檢查將比對 cmd.exe。",
        )

    if launch_kind == LaunchKind.POWERSHELL:
        host = shutil.which("powershell.exe") or "powershell.exe"
        return ProcessMatchInference(
            process_name=Path(host).name,
            executable_path=host,
            note="PowerShell 腳本實際會由 powershell.exe 執行，名稱檢查將比對 powershell.exe。",
        )

    return ProcessMatchInference(
        process_name=Path(sys.executable).name,
        executable_path=sys.executable,
        note="Python 腳本實際會由目前的 Python 直譯器執行，名稱檢查將比對該直譯器。",
    )


def build_command(launch: LaunchSpec) -> list[str]:
    launch.validate()
    kind = launch.kind if launch.kind != LaunchKind.AUTO else detect_launch_kind(launch.path)

    if kind == LaunchKind.PYTHON:
        return [sys.executable, launch.path, *launch.args]
    if kind == LaunchKind.POWERSHELL:
        return ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", launch.path, *launch.args]
    if kind == LaunchKind.CMD:
        return ["cmd.exe", "/c", launch.path, *launch.args]
    return [launch.path, *launch.args]


def launch_process(launch: LaunchSpec) -> LaunchResult:
    launch.validate()
    executable = Path(launch.path)
    if not executable.exists():
        raise ConfigValidationError(f"啟動目標不存在：{launch.path}")

    working_dir = launch.working_dir or str(executable.parent)
    working_path = Path(working_dir)
    if not working_path.exists():
        raise ConfigValidationError(f"工作目錄不存在：{working_dir}")

    command = build_command(launch)
    process = subprocess.Popen(  # noqa: S603
        command,
        cwd=str(working_path),
        shell=False,
        start_new_session=True,
    )
    return LaunchResult(pid=process.pid, command=command, working_dir=str(working_path))

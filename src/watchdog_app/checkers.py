from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import socket

import psutil

from .models import CheckLogic, CheckSpec, CheckType, TargetConfig


@dataclass(slots=True)
class CheckContext:
    runtime_pid: int | None = None


@dataclass(slots=True)
class CheckResult:
    healthy: bool
    summary: str
    details: str = ""


@dataclass(slots=True)
class AggregatedCheckResult:
    healthy: bool
    check_results: list[CheckResult]
    summary: str


def _process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _read_pidfile(path_text: str) -> int | None:
    path = Path(path_text)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _check_process_name(check: CheckSpec) -> CheckResult:
    target_name = check.process_name.casefold()
    expected_exe = Path(check.executable_path).resolve() if check.executable_path else None

    for process in psutil.process_iter(["name", "exe"]):
        name = (process.info.get("name") or "").casefold()
        if name != target_name:
            continue
        if expected_exe:
            exe = process.info.get("exe")
            if not exe:
                continue
            try:
                if Path(exe).resolve() != expected_exe:
                    continue
            except OSError:
                continue
        return CheckResult(True, check.summary(), "已找到程序。")
    return CheckResult(False, check.summary(), "找不到程序。")


def _check_tcp_port(check: CheckSpec) -> CheckResult:
    try:
        with socket.create_connection((check.host, check.port), timeout=check.timeout_sec):
            return CheckResult(True, check.summary(), "連接埠可正常連線。")
    except OSError as exc:
        return CheckResult(False, check.summary(), str(exc))


def _check_http_endpoint(check: CheckSpec) -> CheckResult:
    request = Request(check.url, method=check.method)
    try:
        with urlopen(request, timeout=check.timeout_sec) as response:  # noqa: S310
            status_ok = response.status == check.expected_status
            body = response.read().decode("utf-8", errors="replace")
            body_ok = not check.body_substring or check.body_substring in body
            healthy = status_ok and body_ok
            details = f"狀態碼={response.status}"
            if check.body_substring and not body_ok:
                details = f"{details}，缺少指定的回應內容"
            return CheckResult(healthy, check.summary(), details)
    except HTTPError as exc:
        return CheckResult(False, check.summary(), f"HTTP {exc.code}")
    except URLError as exc:
        return CheckResult(False, check.summary(), str(exc.reason))


def evaluate_check(check: CheckSpec, context: CheckContext) -> CheckResult:
    validated = check.validate()
    if validated.type == CheckType.RUNTIME_PID:
        return CheckResult(
            _process_exists(context.runtime_pid),
            validated.summary(),
            f"PID={context.runtime_pid or '未提供'}",
        )
    if validated.type == CheckType.PIDFILE:
        pid = _read_pidfile(validated.pidfile_path)
        return CheckResult(_process_exists(pid), validated.summary(), f"PID={pid or '未提供'}")
    if validated.type == CheckType.PROCESS_NAME:
        return _check_process_name(validated)
    if validated.type == CheckType.TCP_PORT:
        return _check_tcp_port(validated)
    return _check_http_endpoint(validated)


def evaluate_target(target: TargetConfig, context: CheckContext) -> AggregatedCheckResult:
    results = [evaluate_check(check, context) for check in target.checks]
    if target.check_logic == CheckLogic.ALL:
        healthy = all(result.healthy for result in results)
    else:
        healthy = any(result.healthy for result in results)

    summary = "檢查通過" if healthy else "檢查未通過"
    return AggregatedCheckResult(healthy=healthy, check_results=results, summary=summary)

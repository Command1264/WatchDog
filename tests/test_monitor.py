from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import sys
import time

from watchdog_app.models import AppConfig, CheckSpec, CheckType, LaunchKind, LaunchSpec, TargetConfig
from watchdog_app.launchers import LaunchResult
from watchdog_app.checkers import AggregatedCheckResult, CheckResult
from watchdog_app.monitor import MonitorEngine, TargetRuntimeState, TargetStatus


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_monitor_manual_launch_and_health_check(tmp_path: Path) -> None:
    port = _free_port()
    helper = Path(__file__).parent / "helpers" / "demo_process.py"
    target = TargetConfig(
        id="demo",
        name="Demo",
        enabled=True,
        launch=LaunchSpec(
            path=str(helper),
            kind=LaunchKind.PYTHON,
            args=["--http-port", str(port), "--sleep", "5"],
            working_dir=str(helper.parent),
        ),
        startup_delay_sec=0.05,
        check_interval_sec=0.1,
        restart_cooldown_sec=0.1,
        checks=[
            CheckSpec(type=CheckType.HTTP_ENDPOINT, url=f"http://127.0.0.1:{port}/health")
        ],
    ).validate()
    engine = MonitorEngine(AppConfig(targets=[target]))

    try:
        result = engine.manual_launch(target.id)
        assert result.pid > 0

        time.sleep(0.4)
        check = engine.test_target(target.id)
        assert check.healthy is True
        assert engine.states[target.id].status == TargetStatus.RUNNING
    finally:
        engine.shutdown()
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(engine.states[target.id].runtime_pid or result.pid), "/F", "/T"],
            check=False,
            capture_output=True,
        )


def test_monitor_records_launch_errors_for_missing_executable() -> None:
    target = TargetConfig(
        id="missing",
        name="Missing",
        enabled=True,
        launch=LaunchSpec(path="Z:/missing.exe", kind=LaunchKind.EXE, working_dir="C:/"),
        startup_delay_sec=0.05,
        check_interval_sec=0.1,
        restart_cooldown_sec=0.1,
        checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
    ).validate()
    engine = MonitorEngine(AppConfig(targets=[target]))
    engine.start()
    try:
        time.sleep(0.25)
        assert engine.states[target.id].status == TargetStatus.ERROR
        assert "不存在" in engine.states[target.id].last_error
    finally:
        engine.shutdown()


def test_first_startup_respects_target_startup_delay(monkeypatch) -> None:
    launches: list[str] = []
    clock = {"now": 10.0}

    def _fake_launch(launch: LaunchSpec) -> LaunchResult:
        launches.append(launch.path)
        return LaunchResult(pid=1234, command=[launch.path], working_dir="C:/")

    monkeypatch.setattr("watchdog_app.monitor.launch_process", _fake_launch)

    target = TargetConfig(
        id="delayed",
        name="Delayed",
        enabled=True,
        launch=LaunchSpec(path="C:/demo.exe", kind=LaunchKind.EXE, working_dir="C:/"),
        startup_delay_sec=1.5,
        check_interval_sec=5.0,
        restart_cooldown_sec=5.0,
        checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
    ).validate()
    engine = MonitorEngine(
        AppConfig(targets=[target]),
        time_provider=lambda: clock["now"],
        wall_time_provider=lambda: 1000.0,
        sleep_interval=0.01,
    )
    engine.start()
    try:
        time.sleep(0.05)
        assert launches == []

        clock["now"] = 11.6
        time.sleep(0.05)
        assert launches == ["C:/demo.exe"]
    finally:
        engine.shutdown()


def test_startup_skips_launch_when_existing_target_is_already_healthy(monkeypatch) -> None:
    launches: list[str] = []
    clock = {"now": 10.0}

    def _fake_launch(launch: LaunchSpec) -> LaunchResult:
        launches.append(launch.path)
        return LaunchResult(pid=1234, command=[launch.path], working_dir="C:/")

    def _healthy_target(target: TargetConfig, context) -> AggregatedCheckResult:
        return AggregatedCheckResult(
            healthy=True,
            check_results=[CheckResult(True, "名稱檢查", "已找到程序。")],
            summary="檢查通過",
        )

    monkeypatch.setattr("watchdog_app.monitor.launch_process", _fake_launch)
    monkeypatch.setattr("watchdog_app.monitor.evaluate_target", _healthy_target)

    target = TargetConfig(
        id="existing",
        name="Existing",
        enabled=True,
        launch=LaunchSpec(path="C:/demo.exe", kind=LaunchKind.EXE, working_dir="C:/"),
        startup_delay_sec=0.2,
        check_interval_sec=5.0,
        restart_cooldown_sec=5.0,
        checks=[CheckSpec(type=CheckType.PROCESS_NAME, process_name="demo.exe")],
    ).validate()
    engine = MonitorEngine(
        AppConfig(targets=[target]),
        time_provider=lambda: clock["now"],
        wall_time_provider=lambda: 1000.0,
        sleep_interval=0.01,
    )
    engine.start()
    try:
        time.sleep(0.05)
        assert launches == []

        clock["now"] = 10.3
        time.sleep(0.05)
        state = engine.states[target.id]
        assert launches == []
        assert state.status == TargetStatus.RUNNING
        assert state.last_check_at == 1000.0
        assert state.next_check_at == 15.3
    finally:
        engine.shutdown()


def test_set_config_drops_runtime_states_for_removed_targets() -> None:
    engine = MonitorEngine(
        AppConfig(
            targets=[
                TargetConfig(
                    id="alpha",
                    name="Alpha",
                    enabled=True,
                    launch=LaunchSpec(path="C:/alpha.exe", kind=LaunchKind.EXE, working_dir="C:/"),
                    checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
                ).validate(),
                TargetConfig(
                    id="beta",
                    name="Beta",
                    enabled=True,
                    launch=LaunchSpec(path="C:/beta.exe", kind=LaunchKind.EXE, working_dir="C:/"),
                    checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
                ).validate(),
            ]
        ).validate()
    )

    engine.set_config(
        AppConfig(
            targets=[
                TargetConfig(
                    id="beta",
                    name="Beta",
                    enabled=True,
                    launch=LaunchSpec(path="C:/beta.exe", kind=LaunchKind.EXE, working_dir="C:/"),
                    checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
                ).validate()
            ]
        ).validate()
    )

    assert set(engine.states) == {"beta"}


def test_handle_start_sequence_skips_targets_missing_from_runtime_states(monkeypatch) -> None:
    target = TargetConfig(
        id="alpha",
        name="Alpha",
        enabled=True,
        launch=LaunchSpec(path="C:/alpha.exe", kind=LaunchKind.EXE, working_dir="C:/"),
        startup_delay_sec=0.05,
        check_interval_sec=0.1,
        restart_cooldown_sec=0.1,
        checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
    ).validate()
    engine = MonitorEngine(AppConfig(targets=[target]).validate())
    engine._startup_pending = True
    engine._startup_index = 0
    engine._next_start_at = 0.0
    engine._states = {}

    monkeypatch.setattr(
        "watchdog_app.monitor.evaluate_target",
        lambda target, context: AggregatedCheckResult(
            healthy=False,
            check_results=[CheckResult(False, "PID 檢查", "程序不存在。")],
            summary="檢查失敗",
        ),
    )

    engine._handle_start_sequence(1.0, 1.0)

    assert engine.states == {}


def test_check_targets_skips_targets_missing_from_runtime_states(monkeypatch) -> None:
    target = TargetConfig(
        id="alpha",
        name="Alpha",
        enabled=True,
        launch=LaunchSpec(path="C:/alpha.exe", kind=LaunchKind.EXE, working_dir="C:/"),
        startup_delay_sec=0.05,
        check_interval_sec=0.1,
        restart_cooldown_sec=0.1,
        checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
    ).validate()
    engine = MonitorEngine(AppConfig(targets=[target]).validate())
    engine._states = {}

    monkeypatch.setattr(
        "watchdog_app.monitor.evaluate_target",
        lambda target, context: AggregatedCheckResult(
            healthy=True,
            check_results=[CheckResult(True, "PID 檢查", "程序存在。")],
            summary="檢查通過",
        ),
    )

    engine._check_targets(1.0, 1.0)

    assert engine.states == {}


def test_set_config_while_running_respects_startup_delay_for_new_target(monkeypatch) -> None:
    launches: list[str] = []
    clock = {"now": 10.0}

    def _fake_launch(launch: LaunchSpec) -> LaunchResult:
        launches.append(launch.path)
        return LaunchResult(pid=4321, command=[launch.path], working_dir="C:/")

    monkeypatch.setattr("watchdog_app.monitor.launch_process", _fake_launch)
    monkeypatch.setattr(
        "watchdog_app.monitor.evaluate_target",
        lambda target, context: AggregatedCheckResult(
            healthy=False,
            check_results=[CheckResult(False, "PID 檢查", "程序不存在。")],
            summary="檢查失敗",
        ),
    )

    engine = MonitorEngine(
        AppConfig(targets=[]).validate(),
        time_provider=lambda: clock["now"],
        wall_time_provider=lambda: 1000.0,
        sleep_interval=0.01,
    )
    engine.start()
    try:
        delayed = TargetConfig(
            id="dynamic",
            name="Dynamic",
            enabled=True,
            launch=LaunchSpec(path="C:/dynamic.exe", kind=LaunchKind.EXE, working_dir="C:/"),
            startup_delay_sec=1.0,
            check_interval_sec=5.0,
            restart_cooldown_sec=5.0,
            checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
        ).validate()

        engine.set_config(AppConfig(targets=[delayed]).validate())
        time.sleep(0.05)
        assert launches == []

        clock["now"] = 10.8
        time.sleep(0.05)
        assert launches == []

        clock["now"] = 11.1
        time.sleep(0.05)
        assert launches == ["C:/dynamic.exe"]
    finally:
        engine.shutdown()


def test_set_config_resets_runtime_state_and_reschedules_reconfigured_target(monkeypatch) -> None:
    launches: list[str] = []
    clock = {"now": 10.0}

    def _fake_launch(launch: LaunchSpec) -> LaunchResult:
        launches.append(launch.path)
        return LaunchResult(pid=9876, command=[launch.path], working_dir="C:/")

    monkeypatch.setattr("watchdog_app.monitor.launch_process", _fake_launch)
    monkeypatch.setattr(
        "watchdog_app.monitor.evaluate_target",
        lambda target, context: AggregatedCheckResult(
            healthy=False,
            check_results=[CheckResult(False, "名稱檢查", "程序不存在。")],
            summary="檢查失敗",
        ),
    )

    original = TargetConfig(
        id="alpha",
        name="Alpha",
        enabled=True,
        launch=LaunchSpec(path="C:/old.exe", kind=LaunchKind.EXE, working_dir="C:/"),
        startup_delay_sec=1.0,
        check_interval_sec=5.0,
        restart_cooldown_sec=5.0,
        checks=[
            CheckSpec(
                type=CheckType.PROCESS_NAME,
                process_name="old.exe",
                executable_path="C:/old.exe",
            )
        ],
    ).validate()
    updated = TargetConfig(
        id="alpha",
        name="Alpha",
        enabled=True,
        launch=LaunchSpec(path="C:/new.exe", kind=LaunchKind.EXE, working_dir="C:/"),
        startup_delay_sec=1.5,
        check_interval_sec=5.0,
        restart_cooldown_sec=5.0,
        checks=[
            CheckSpec(
                type=CheckType.PROCESS_NAME,
                process_name="new.exe",
                executable_path="C:/new.exe",
            )
        ],
    ).validate()

    engine = MonitorEngine(
        AppConfig(targets=[original]).validate(),
        time_provider=lambda: clock["now"],
        wall_time_provider=lambda: 1000.0,
        sleep_interval=0.01,
    )
    engine._running = True
    engine._states["alpha"] = TargetRuntimeState(
        status=TargetStatus.RUNNING,
        runtime_pid=111,
        last_check_at=900.0,
        last_restart_at=800.0,
        last_restart_monotonic=9.0,
        last_error="stale",
        last_error_detail="stale detail",
        next_check_at=999.0,
    )

    engine.set_config(AppConfig(targets=[updated]).validate())

    state = engine.states["alpha"]
    assert state.status == TargetStatus.STOPPED
    assert state.runtime_pid is None
    assert state.last_error == ""
    assert state.last_error_detail == ""
    assert state.next_check_at == 0.0
    assert engine._startup_queue == ["alpha"]
    assert engine._next_start_at == 11.5

    engine._handle_start_sequence(11.4, 1000.0)
    assert launches == []

    engine._handle_start_sequence(11.6, 1000.0)
    assert launches == ["C:/new.exe"]

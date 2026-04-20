from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import threading
import time
from typing import Callable

from .checkers import AggregatedCheckResult, CheckContext, evaluate_target
from .launchers import LaunchResult, launch_process
from .models import AppConfig, ConfigValidationError


logger = logging.getLogger(__name__)


class TargetStatus(str, Enum):
    DISABLED = "disabled"
    STOPPED = "stopped"
    SCHEDULED = "scheduled"
    LAUNCHING = "launching"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"
    ERROR = "error"


@dataclass(slots=True)
class TargetRuntimeState:
    status: TargetStatus = TargetStatus.STOPPED
    runtime_pid: int | None = None
    last_check_at: float | None = None
    last_restart_at: float | None = None
    last_restart_monotonic: float | None = None
    last_error: str = ""
    last_error_detail: str = ""
    next_check_at: float = 0.0


@dataclass(slots=True)
class MonitorEvent:
    target_id: str | None
    status: TargetStatus | None
    message: str
    snapshot: dict[str, TargetRuntimeState] = field(default_factory=dict)


class MonitorEngine:
    def __init__(
        self,
        config: AppConfig,
        *,
        event_sink: Callable[[MonitorEvent], None] | None = None,
        time_provider: Callable[[], float] = time.monotonic,
        wall_time_provider: Callable[[], float] = time.time,
        sleep_interval: float = 0.05,
    ) -> None:
        self._config = config.validate()
        self._event_sink = event_sink
        self._time = time_provider
        self._wall_time = wall_time_provider
        self._sleep_interval = sleep_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._running = False
        self._startup_pending = False
        self._states = {
            target.id: (TargetRuntimeState(status=TargetStatus.STOPPED if target.enabled else TargetStatus.DISABLED))
            for target in self._config.targets
        }
        self._startup_index = 0
        self._startup_queue: list[str] = []
        self._next_start_at = 0.0

    @property
    def states(self) -> dict[str, TargetRuntimeState]:
        with self._lock:
            return {
                key: TargetRuntimeState(
                    status=value.status,
                    runtime_pid=value.runtime_pid,
                    last_check_at=value.last_check_at,
                    last_restart_at=value.last_restart_at,
                    last_restart_monotonic=value.last_restart_monotonic,
                    last_error=value.last_error,
                    last_error_detail=value.last_error_detail,
                    next_check_at=value.next_check_at,
                )
                for key, value in self._states.items()
            }

    def set_config(self, config: AppConfig) -> None:
        with self._lock:
            previous_config = self._config
            previous_queue_head = self._startup_queue[0] if self._startup_queue else None
            self._config = config.validate()
            previous_states = self._states
            updated_states: dict[str, TargetRuntimeState] = {}
            previous_targets = {target.id: target for target in previous_config.targets}
            previous_enabled = {target.id: target.enabled for target in previous_config.targets}
            reconfigured_target_ids: set[str] = set()
            for target in self._config.targets:
                previous_target = previous_targets.get(target.id)
                definition_changed = (
                    previous_target is not None
                    and self._target_behavior_snapshot(previous_target)
                    != self._target_behavior_snapshot(target)
                )
                state = previous_states.get(target.id)
                if state is None or definition_changed:
                    state = TargetRuntimeState(
                        status=TargetStatus.STOPPED if target.enabled else TargetStatus.DISABLED
                    )
                    if definition_changed:
                        reconfigured_target_ids.add(target.id)
                elif not target.enabled:
                    state.status = TargetStatus.DISABLED
                elif state.status == TargetStatus.DISABLED:
                    state.status = TargetStatus.STOPPED
                updated_states[target.id] = state
            self._states = updated_states
            valid_target_ids = set(updated_states)
            self._startup_queue = [
                target_id
                for target_id in self._startup_queue
                if target_id in valid_target_ids and target_id not in reconfigured_target_ids
            ]
            if self._running:
                queued_ids = set(self._startup_queue)
                for target in self._config.targets:
                    if (
                        target.enabled
                        and (
                            not previous_enabled.get(target.id, False)
                            or target.id in reconfigured_target_ids
                        )
                        and target.id not in queued_ids
                    ):
                        self._startup_queue.append(target.id)
                        queued_ids.add(target.id)
                if (
                    reconfigured_target_ids
                    or (self._startup_queue[0] if self._startup_queue else None) != previous_queue_head
                ):
                    self._next_start_at = 0.0
                self._sync_startup_schedule_locked(self._time())
            else:
                self._startup_pending = False
                self._next_start_at = 0.0

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._startup_index = 0
            self._startup_queue = [target.id for target in self._config.targets if target.enabled]
            self._sync_startup_schedule_locked(self._time())
            self._stop_event.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="watchdog-monitor",
                    daemon=True,
                )
                self._thread.start()
        logger.info("Monitoring engine started. enabled_targets=%s", len(self._startup_queue))
        self._emit(None, None, "監測已啟動。")

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._startup_pending = False
            self._startup_queue = []
            self._next_start_at = 0.0
            for target in self._config.targets:
                state = self._states[target.id]
                state.status = TargetStatus.STOPPED if target.enabled else TargetStatus.DISABLED
                state.last_error = ""
                state.last_error_detail = ""
        logger.info("Monitoring engine stopped.")
        self._emit(None, None, "監測已停止。")

    def shutdown(self) -> None:
        self.stop()
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def manual_launch(self, target_id: str) -> LaunchResult:
        target = self._target_by_id(target_id)
        result = launch_process(target.launch)
        now = self._time()
        with self._lock:
            state = self._states.get(target.id)
            if state is None:
                raise KeyError(target.id)
            state.runtime_pid = result.pid
            state.last_restart_at = self._wall_time()
            state.last_restart_monotonic = now
            state.status = TargetStatus.RUNNING
            state.last_error = ""
            state.last_error_detail = ""
        logger.info("Manual target launch succeeded. target=%s pid=%s", target.id, result.pid)
        self._emit(target.id, TargetStatus.RUNNING, "已手動啟動目標。")
        return result

    def test_target(self, target_id: str) -> AggregatedCheckResult:
        target = self._target_by_id(target_id)
        with self._lock:
            state = self._states.get(target.id)
            if state is None:
                raise KeyError(target.id)
            runtime_pid = state.runtime_pid
        result = evaluate_target(target, CheckContext(runtime_pid=runtime_pid))
        with self._lock:
            state = self._states.get(target.id)
            if state is None:
                raise KeyError(target.id)
            state.last_check_at = self._wall_time()
            state.status = TargetStatus.RUNNING if result.healthy else TargetStatus.UNHEALTHY
            if result.healthy:
                state.last_error = ""
                state.last_error_detail = ""
            else:
                state.last_error, state.last_error_detail = self._summarize_check_failure(result)
        self._emit(target.id, state.status, result.summary)
        return result

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._running:
                self._stop_event.wait(self._sleep_interval)
                continue

            now = self._time()
            now_wall = self._wall_time()
            self._handle_start_sequence(now, now_wall)
            self._check_targets(now, now_wall)
            self._stop_event.wait(self._sleep_interval)

    def _handle_start_sequence(self, now: float, now_wall: float) -> None:
        with self._lock:
            if not self._startup_pending or now < self._next_start_at:
                return

            target = None
            while self._startup_queue:
                candidate_id = self._startup_queue.pop(0)
                candidate = next(
                    (item for item in self._config.targets if item.id == candidate_id and item.enabled),
                    None,
                )
                if candidate is not None:
                    target = candidate
                    break
            self._next_start_at = 0.0
            self._sync_startup_schedule_locked(now)
            if target is None:
                return

        startup_health = self._startup_check_passes(target, now, now_wall)
        if startup_health is True:
            return
        if startup_health is None:
            return

        with self._lock:
            state = self._states.get(target.id)
            if state is None:
                return
            state.status = TargetStatus.SCHEDULED
        self._launch_target(target, now, now_wall, "排程啟動")

    def _startup_check_passes(self, target, now: float, now_wall: float) -> bool | None:
        with self._lock:
            state = self._states.get(target.id)
            if state is None:
                return None
            runtime_pid = state.runtime_pid
        try:
            result = evaluate_target(target, CheckContext(runtime_pid=runtime_pid))
        except (ConfigValidationError, OSError) as exc:
            self._record_error(target.id, str(exc))
            return None
        except Exception as exc:
            self._record_error(target.id, f"{type(exc).__name__}: {exc}", include_traceback=True)
            return None

        with self._lock:
            state = self._states.get(target.id)
            if state is None:
                return None
            state.last_check_at = now_wall
            state.next_check_at = now + target.check_interval_sec
            if not result.healthy:
                state.last_error, state.last_error_detail = self._summarize_check_failure(result)
                return False

            state.status = TargetStatus.RUNNING
            state.last_error = ""
            state.last_error_detail = ""
        logger.info("Startup check passed; launch skipped. target=%s", target.id)
        self._emit(target.id, state.status, "啟動時檢查通過，略過啟動。")
        return True

    def _check_targets(self, now: float, now_wall: float) -> None:
        with self._lock:
            targets = list(self._config.targets)

        for target in targets:
            with self._lock:
                state = self._states.get(target.id)
                startup_pending = self._startup_pending
                if state is None:
                    continue
                if not target.enabled:
                    state.status = TargetStatus.DISABLED
                    continue
                if startup_pending and state.status in {
                    TargetStatus.STOPPED,
                    TargetStatus.SCHEDULED,
                }:
                    continue
                if state.next_check_at and state.next_check_at > now:
                    continue
                runtime_pid = state.runtime_pid

            try:
                result = evaluate_target(target, CheckContext(runtime_pid=runtime_pid))
                emit_status: TargetStatus | None = None
                should_launch = False
                with self._lock:
                    state = self._states.get(target.id)
                    if state is None:
                        continue
                    state.last_check_at = now_wall
                    state.next_check_at = now + target.check_interval_sec
                    if result.healthy:
                        state.status = TargetStatus.RUNNING
                        state.last_error = ""
                        state.last_error_detail = ""
                        emit_status = state.status
                    else:
                        if state.status != TargetStatus.ERROR:
                            state.status = TargetStatus.UNHEALTHY
                            state.last_error, state.last_error_detail = self._summarize_check_failure(result)
                        emit_status = state.status
                        should_launch = (
                            state.status != TargetStatus.ERROR
                            and (
                                not state.last_restart_monotonic
                                or (now - state.last_restart_monotonic) >= target.restart_cooldown_sec
                            )
                        )
                if emit_status is not None:
                    self._emit(target.id, emit_status, result.summary)
                if should_launch:
                    self._launch_target(target, now, now_wall, "自動重新啟動")
            except (ConfigValidationError, OSError) as exc:
                self._record_error(target.id, str(exc))
            except Exception as exc:
                self._record_error(target.id, f"{type(exc).__name__}: {exc}", include_traceback=True)

    def _launch_target(self, target, now: float, now_wall: float, message: str) -> None:
        with self._lock:
            if target.id not in self._states:
                return
        self._emit(target.id, TargetStatus.LAUNCHING, message)
        try:
            result = launch_process(target.launch)
        except (ConfigValidationError, OSError) as exc:
            with self._lock:
                state = self._states.get(target.id)
                if state is None:
                    return
                state.last_restart_monotonic = now
                state.next_check_at = now + target.check_interval_sec
            self._record_error(target.id, str(exc))
            return

        with self._lock:
            state = self._states.get(target.id)
            if state is None:
                return
            state.runtime_pid = result.pid
            state.last_restart_at = now_wall
            state.last_restart_monotonic = now
            state.next_check_at = now + target.check_interval_sec
            state.status = TargetStatus.RUNNING
            state.last_error = ""
            state.last_error_detail = ""
        logger.info("Target launch succeeded. target=%s pid=%s mode=%s", target.id, result.pid, message)
        self._emit(target.id, state.status, f"{message}：PID={result.pid}")

    def _record_error(self, target_id: str, message: str, *, include_traceback: bool = False) -> None:
        if include_traceback:
            logger.exception("Target %s operation failed: %s", target_id, message)
        else:
            logger.error("Target %s operation failed: %s", target_id, message)
        with self._lock:
            state = self._states.get(target_id)
            if state is None:
                return
            state.status = TargetStatus.ERROR
            state.last_error = self._summarize_text(message)
            state.last_error_detail = message
        self._emit(target_id, TargetStatus.ERROR, message)

    @staticmethod
    def _summarize_text(message: str, limit: int = 72) -> str:
        compact = " ".join(message.split())
        if len(compact) <= limit:
            return compact
        return f"{compact[: limit - 1]}…"

    def _summarize_check_failure(self, result: AggregatedCheckResult) -> tuple[str, str]:
        failing_checks = [check for check in result.check_results if not check.healthy]
        if not failing_checks:
            return ("檢查未通過", result.summary)

        summary_source = failing_checks[0]
        summary_detail = summary_source.details or summary_source.summary
        summary = self._summarize_text(f"{summary_source.summary}: {summary_detail}")
        detail_lines = []
        for check in result.check_results:
            state = "通過" if check.healthy else "失敗"
            line = f"[{state}] {check.summary}"
            if check.details:
                line = f"{line} - {check.details}"
            detail_lines.append(line)
        return summary, "\n".join(detail_lines)

    def _emit(self, target_id: str | None, status: TargetStatus | None, message: str) -> None:
        if not self._event_sink:
            return
        self._event_sink(
            MonitorEvent(
                target_id=target_id,
                status=status,
                message=message,
                snapshot=self.states,
            )
        )

    def _target_by_id(self, target_id: str):
        with self._lock:
            for target in self._config.targets:
                if target.id == target_id:
                    return target
        raise KeyError(target_id)

    def _enabled_targets(self):
        with self._lock:
            return [target for target in self._config.targets if target.enabled]

    @staticmethod
    def _check_behavior_snapshot(check) -> dict[str, object]:
        return {
            "type": getattr(check.type, "value", check.type),
            "pidfile_path": check.pidfile_path,
            "process_name": check.process_name,
            "executable_path": check.executable_path,
            "host": check.host,
            "port": check.port,
            "url": check.url,
            "method": check.method,
            "timeout_sec": check.timeout_sec,
            "expected_status": check.expected_status,
            "body_substring": check.body_substring,
        }

    @classmethod
    def _target_behavior_snapshot(cls, target) -> dict[str, object]:
        return {
            "launch": target.launch.to_dict(),
            "check_logic": getattr(target.check_logic, "value", target.check_logic),
            "checks": [cls._check_behavior_snapshot(check) for check in target.checks],
        }

    def _sync_startup_schedule_locked(self, now: float) -> None:
        if not self._running:
            self._startup_pending = False
            self._next_start_at = 0.0
            return

        self._startup_queue = [
            target_id
            for target_id in self._startup_queue
            if any(target.id == target_id and target.enabled for target in self._config.targets)
        ]
        if not self._startup_queue:
            self._startup_pending = False
            self._next_start_at = 0.0
            return

        first_target = next(
            (target for target in self._config.targets if target.id == self._startup_queue[0]),
            None,
        )
        if first_target is None:
            self._startup_queue.pop(0)
            self._sync_startup_schedule_locked(now)
            return

        if not self._startup_pending or self._next_start_at == 0.0:
            self._startup_pending = True
            self._next_start_at = now + first_target.startup_delay_sec

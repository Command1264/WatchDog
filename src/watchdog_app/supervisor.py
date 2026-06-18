from __future__ import annotations

import logging
import os
import subprocess
import time

from .models import ExitReason
from .runtime import child_command


logger = logging.getLogger(__name__)


NON_RESTART_REASONS = {
    ExitReason.USER_EXIT,
    ExitReason.CTRL_C_EXIT,
    ExitReason.OS_SESSION_END,
    ExitReason.SECONDARY_INSTANCE,
}


class Supervisor:
    def __init__(self, child_args: list[str] | None = None) -> None:
        self._child_args = child_args or []

    def run(self) -> int:
        backoff_seconds = 1.0
        while True:
            command = [*child_command(), *self._child_args]
            logger.info("Launching child app: %s", command)
            completed = subprocess.run(command, **self._child_run_kwargs(command))  # noqa: S603
            reason = ExitReason.from_exit_code(completed.returncode)

            if reason in NON_RESTART_REASONS:
                logger.info("Child exited without restart: %s", reason)
                return completed.returncode

            logger.warning(
                "Child exited unexpectedly (code=%s, reason=%s). Restarting in %.1fs.",
                completed.returncode,
                reason,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2.0, 30.0)

    @staticmethod
    def _child_run_kwargs(command: list[str]) -> dict[str, object]:
        kwargs: dict[str, object] = {"check": False}
        if os.name != "nt" or not Supervisor._should_hide_child_window(command):
            return kwargs

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
        startf_use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)

        if startupinfo_factory is not None:
            startupinfo = startupinfo_factory()
            startupinfo.dwFlags |= startf_use_show_window
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
        if creationflags:
            kwargs["creationflags"] = creationflags
        return kwargs

    @staticmethod
    def _should_hide_child_window(command: list[str]) -> bool:
        if not command:
            return False
        executable = os.path.basename(command[0]).casefold()
        return executable in {"python.exe", "python", "py.exe"}

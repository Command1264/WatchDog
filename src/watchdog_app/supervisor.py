from __future__ import annotations

import logging
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
            completed = subprocess.run(command, check=False)  # noqa: S603
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

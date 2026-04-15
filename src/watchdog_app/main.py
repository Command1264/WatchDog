from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from watchdog_app.app import run_child_app
    from watchdog_app.models import ExitReason
    from watchdog_app.supervisor import Supervisor
else:
    from .app import run_child_app
    from .models import ExitReason
    from .supervisor import Supervisor


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--child-app" in args:
        args.remove("--child-app")
        return run_child_app()

    try:
        return Supervisor(child_args=args).run()
    except KeyboardInterrupt:
        return ExitReason.CTRL_C_EXIT.value


if __name__ == "__main__":
    raise SystemExit(main())

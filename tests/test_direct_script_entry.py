from __future__ import annotations

from pathlib import Path
import runpy


def test_app_script_can_be_loaded_without_package_context() -> None:
    script_path = Path(__file__).resolve().parents[1] / "src" / "watchdog_app" / "app.py"
    namespace = runpy.run_path(str(script_path), run_name="__script_smoke_test__")

    assert "run_child_app" in namespace


def test_main_script_can_be_loaded_without_package_context() -> None:
    script_path = Path(__file__).resolve().parents[1] / "src" / "watchdog_app" / "main.py"
    namespace = runpy.run_path(str(script_path), run_name="__script_smoke_test__")

    assert "main" in namespace

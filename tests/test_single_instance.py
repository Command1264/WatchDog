from __future__ import annotations

from pathlib import Path

from watchdog_app.single_instance import SingleInstanceCoordinator


def test_single_instance_server_name_is_stable_per_runtime_root(monkeypatch) -> None:
    monkeypatch.setattr("watchdog_app.single_instance.runtime_base_dir", lambda: Path("C:/Apps/WatchDog-A"))

    first = SingleInstanceCoordinator()
    second = SingleInstanceCoordinator()

    assert first._server_name == second._server_name
    assert first._server_name.startswith("WatchDog_PrimaryInstance_")


def test_single_instance_server_name_differs_for_different_runtime_roots(monkeypatch) -> None:
    monkeypatch.setattr("watchdog_app.single_instance.runtime_base_dir", lambda: Path("C:/Apps/WatchDog-A"))
    first = SingleInstanceCoordinator()

    monkeypatch.setattr("watchdog_app.single_instance.runtime_base_dir", lambda: Path("D:/Portable/WatchDog-B"))
    second = SingleInstanceCoordinator()

    assert first._server_name != second._server_name

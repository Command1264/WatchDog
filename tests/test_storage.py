from __future__ import annotations

from pathlib import Path

from watchdog_app.models import BootstrapState, StorageMode, StoragePreferences
from watchdog_app import storage


def test_resolve_paths_falls_back_when_exe_dir_not_writable(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setattr(storage, "runtime_base_dir", lambda: tmp_path / "exe")
    monkeypatch.setattr(storage, "appdata_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(storage, "local_appdata_dir", lambda: tmp_path / "localappdata")
    monkeypatch.setattr(
        storage,
        "_is_writable",
        lambda path: path != tmp_path / "exe",
    )

    resolved = storage.resolve_paths(
        StoragePreferences(config_mode=StorageMode.EXE, log_mode=StorageMode.EXE)
    )

    assert resolved.config_path == tmp_path / "appdata" / "config.json"
    assert resolved.log_directory == tmp_path / "localappdata"
    assert resolved.config_fallback_used is True
    assert resolved.log_fallback_used is True


def test_bootstrap_round_trip(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setattr(storage, "bootstrap_path", lambda: tmp_path / "bootstrap.json")

    state = BootstrapState(
        storage=StoragePreferences(),
        config_path="C:/tmp/config.json",
        log_directory="C:/tmp/logs",
        first_run_completed=True,
    )
    storage.save_bootstrap_state(state)

    loaded = storage.load_bootstrap_state()

    assert loaded.first_run_completed is True
    assert loaded.config_path == "C:/tmp/config.json"

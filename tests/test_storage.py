from __future__ import annotations

from pathlib import Path
import json

from watchdog_app.models import BootstrapState, StorageMode, StoragePreferences, normalize_path_text
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


def test_effective_storage_preferences_reflects_actual_resolved_locations(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(storage, "runtime_base_dir", lambda: tmp_path / "exe")
    monkeypatch.setattr(storage, "appdata_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(storage, "local_appdata_dir", lambda: tmp_path / "localappdata")

    effective = storage.effective_storage_preferences(
        storage.ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "appdata" / "config.json",
            log_directory=tmp_path / "localappdata",
            config_fallback_used=True,
            log_fallback_used=True,
        )
    )

    assert effective == StoragePreferences(
        config_mode=StorageMode.APPDATA,
        log_mode=StorageMode.LOCALAPPDATA,
    )


def test_update_bootstrap_for_storage_persists_effective_storage_modes(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(storage, "bootstrap_path", lambda: tmp_path / "bootstrap.json")
    monkeypatch.setattr(storage, "runtime_base_dir", lambda: tmp_path / "exe")
    monkeypatch.setattr(storage, "appdata_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(storage, "local_appdata_dir", lambda: tmp_path / "localappdata")
    monkeypatch.setattr(
        storage,
        "_is_writable",
        lambda path: path != tmp_path / "exe",
    )

    storage.update_bootstrap_for_storage(
        StoragePreferences(config_mode=StorageMode.EXE, log_mode=StorageMode.EXE)
    )
    loaded = storage.load_bootstrap_state()

    assert loaded.storage == StoragePreferences(
        config_mode=StorageMode.APPDATA,
        log_mode=StorageMode.LOCALAPPDATA,
    )
    assert loaded.config_path == normalize_path_text(tmp_path / "appdata" / "config.json")
    assert loaded.log_directory == normalize_path_text(tmp_path / "localappdata")


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


def test_resolve_paths_supports_custom_storage_roots(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setattr(storage, "bootstrap_path", lambda: tmp_path / "bootstrap.json")
    monkeypatch.setattr(storage, "_is_writable", lambda _path: True)

    resolved = storage.resolve_paths(
        StoragePreferences(
            config_mode=StorageMode.CUSTOM,
            log_mode=StorageMode.CUSTOM,
            config_custom_path=str(tmp_path / "cfg"),
            log_custom_path=str(tmp_path / "logs"),
        )
    )

    assert resolved.config_path == tmp_path / "cfg" / "config.json"
    assert resolved.log_directory == tmp_path / "logs"
    assert resolved.config_fallback_used is False
    assert resolved.log_fallback_used is False


def test_effective_storage_preferences_reflects_custom_resolved_locations(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(storage, "runtime_base_dir", lambda: tmp_path / "exe")
    monkeypatch.setattr(storage, "appdata_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(storage, "local_appdata_dir", lambda: tmp_path / "localappdata")

    effective = storage.effective_storage_preferences(
        storage.ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "custom-config" / "config.json",
            log_directory=tmp_path / "custom-logs",
        )
    )

    assert effective == StoragePreferences(
        config_mode=StorageMode.CUSTOM,
        log_mode=StorageMode.CUSTOM,
        config_custom_path=normalize_path_text((tmp_path / "custom-config").resolve()),
        log_custom_path=normalize_path_text((tmp_path / "custom-logs").resolve()),
    )


def test_load_bootstrap_state_rewrites_legacy_backslash_paths(monkeypatch: object, tmp_path: Path) -> None:
    bootstrap_file = tmp_path / "bootstrap.json"
    bootstrap_file.write_text(
        json.dumps(
            {
                "config_path": r"C:\Watch Dog\config.json",
                "log_directory": r"C:\Watch Dog\logs",
                "first_run_completed": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "bootstrap_path", lambda: bootstrap_file)

    loaded = storage.load_bootstrap_state()

    assert loaded.config_path == "C:/Watch Dog/config.json"
    assert loaded.log_directory == "C:/Watch Dog/logs"
    rewritten = json.loads(bootstrap_file.read_text(encoding="utf-8"))
    assert rewritten["config_path"] == "C:/Watch Dog/config.json"
    assert rewritten["log_directory"] == "C:/Watch Dog/logs"


def test_load_config_rewrites_legacy_backslash_paths(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "storage": {
                    "config_mode": "custom",
                    "log_mode": "custom",
                    "config_custom_path": r"D:\Config Root",
                    "log_custom_path": r"E:\Log Root",
                },
                "targets": [
                    {
                        "id": "alpha",
                        "name": "Alpha",
                        "enabled": True,
                        "launch": {
                            "path": r"C:\Apps\demo.exe",
                            "args": [],
                            "working_dir": r"C:\Apps",
                            "kind": "exe",
                        },
                        "checks": [
                            {"type": "pidfile", "pidfile_path": r"C:\Apps\demo.pid"},
                            {
                                "type": "process_name",
                                "process_name": "demo.exe",
                                "executable_path": r"C:\Apps\demo.exe",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = storage.load_config(config_file)

    assert loaded.storage.config_custom_path == "D:/Config Root"
    assert loaded.storage.log_custom_path == "E:/Log Root"
    assert loaded.targets[0].launch.path == "C:/Apps/demo.exe"
    assert loaded.targets[0].launch.working_dir == "C:/Apps"
    assert loaded.targets[0].checks[0].pidfile_path == "C:/Apps/demo.pid"
    assert loaded.targets[0].checks[1].executable_path == "C:/Apps/demo.exe"
    rewritten = json.loads(config_file.read_text(encoding="utf-8"))
    assert rewritten["storage"]["config_custom_path"] == "D:/Config Root"
    assert rewritten["targets"][0]["launch"]["path"] == "C:/Apps/demo.exe"
    assert rewritten["targets"][0]["checks"][0]["pidfile_path"] == "C:/Apps/demo.pid"

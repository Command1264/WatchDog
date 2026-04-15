from __future__ import annotations

from pathlib import Path
import json

from .models import (
    APP_NAME,
    AppConfig,
    BOOTSTRAP_FILE_NAME,
    BootstrapState,
    CONFIG_FILE_NAME,
    LOG_FILE_NAME,
    ResolvedPaths,
    StorageMode,
    StoragePreferences,
)
from .runtime import appdata_dir, bootstrap_path, local_appdata_dir, runtime_base_dir


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_bootstrap_state() -> BootstrapState:
    path = bootstrap_path()
    if not path.exists():
        return BootstrapState()
    return BootstrapState.from_dict(_read_json(path))


def save_bootstrap_state(state: BootstrapState) -> Path:
    path = bootstrap_path()
    _write_json(path, state.to_dict())
    return path


def _storage_root(mode: StorageMode) -> Path:
    if mode == StorageMode.EXE:
        return runtime_base_dir()
    if mode == StorageMode.APPDATA:
        return appdata_dir()
    return local_appdata_dir()


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".watchdog_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def resolve_paths(storage: StoragePreferences) -> ResolvedPaths:
    config_root = _storage_root(storage.config_mode)
    log_root = _storage_root(storage.log_mode)

    config_fallback_used = False
    log_fallback_used = False

    if not _is_writable(config_root):
        config_root = appdata_dir()
        config_fallback_used = True
    if not _is_writable(log_root):
        log_root = local_appdata_dir()
        log_fallback_used = True

    return ResolvedPaths(
        bootstrap_path=bootstrap_path(),
        config_path=config_root / CONFIG_FILE_NAME,
        log_directory=log_root,
        config_fallback_used=config_fallback_used,
        log_fallback_used=log_fallback_used,
    )


def discover_config_path() -> Path | None:
    state = load_bootstrap_state()
    if state.config_path:
        path = Path(state.config_path)
        if path.exists():
            return path

    candidates = [
        runtime_base_dir() / CONFIG_FILE_NAME,
        appdata_dir() / CONFIG_FILE_NAME,
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def load_config(path: Path | None = None) -> AppConfig:
    candidate = path or discover_config_path()
    if not candidate or not candidate.exists():
        return AppConfig.default()
    return AppConfig.from_dict(_read_json(candidate))


def save_config(config: AppConfig, path: Path) -> Path:
    config.validate()
    _write_json(path, config.to_dict())
    return path


def update_bootstrap_for_storage(storage: StoragePreferences) -> ResolvedPaths:
    resolved = resolve_paths(storage)
    save_bootstrap_state(
        BootstrapState(
            storage=storage,
            config_path=str(resolved.config_path),
            log_directory=str(resolved.log_directory),
            first_run_completed=True,
        )
    )
    return resolved


def log_file_path(log_directory: Path) -> Path:
    return log_directory / LOG_FILE_NAME

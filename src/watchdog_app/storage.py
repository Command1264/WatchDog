from __future__ import annotations

from pathlib import Path
import json

from .models import (
    APP_NAME,
    AppConfig,
    BOOTSTRAP_FILE_NAME,
    BootstrapState,
    CONFIG_FILE_NAME,
    LOGS_DIRECTORY_NAME,
    ResolvedPaths,
    StorageMode,
    StoragePreferences,
    normalize_path_text,
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
    raw = _read_json(path)
    state = BootstrapState.from_dict(raw)
    normalized = state.to_dict()
    if raw != normalized:
        _write_json(path, normalized)
    return state


def save_bootstrap_state(state: BootstrapState) -> Path:
    path = bootstrap_path()
    _write_json(path, state.to_dict())
    return path


def _storage_root(mode: StorageMode, custom_path: str = "") -> Path:
    if mode == StorageMode.EXE:
        return runtime_base_dir()
    if mode == StorageMode.APPDATA:
        return appdata_dir()
    if mode == StorageMode.LOCALAPPDATA:
        return local_appdata_dir()
    return Path(custom_path).expanduser()


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
    storage = storage.validate()
    config_root = _storage_root(storage.config_mode, storage.config_custom_path)
    log_root = _storage_root(storage.log_mode, storage.log_custom_path)

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


def effective_storage_preferences(resolved: ResolvedPaths) -> StoragePreferences:
    runtime_root = runtime_base_dir().resolve()
    appdata_root = appdata_dir().resolve()
    local_appdata_root = local_appdata_dir().resolve()

    config_root = resolved.config_path.parent.resolve()
    log_root = resolved.log_directory.resolve()

    config_mode = StorageMode.EXE if config_root == runtime_root else StorageMode.APPDATA
    log_mode = StorageMode.EXE if log_root == runtime_root else StorageMode.LOCALAPPDATA
    config_custom_path = ""
    log_custom_path = ""

    if config_root not in {runtime_root, appdata_root}:
        config_mode = StorageMode.CUSTOM
        config_custom_path = normalize_path_text(config_root)
    if log_root not in {runtime_root, local_appdata_root}:
        log_mode = StorageMode.CUSTOM
        log_custom_path = normalize_path_text(log_root)

    return StoragePreferences(
        config_mode=config_mode,
        log_mode=log_mode,
        config_custom_path=config_custom_path,
        log_custom_path=log_custom_path,
    ).validate()


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
    raw = _read_json(candidate)
    config = AppConfig.from_dict(raw)
    normalized = config.to_dict()
    if raw != normalized:
        _write_json(candidate, normalized)
    return config


def save_config(config: AppConfig, path: Path) -> Path:
    config.validate()
    _write_json(path, config.to_dict())
    return path


def update_bootstrap_for_storage(storage: StoragePreferences) -> ResolvedPaths:
    resolved = resolve_paths(storage)
    effective_storage = effective_storage_preferences(resolved)
    save_bootstrap_state(
        BootstrapState(
            storage=effective_storage,
            config_path=normalize_path_text(resolved.config_path),
            log_directory=normalize_path_text(resolved.log_directory),
            first_run_completed=True,
        )
    )
    return resolved


def log_output_root(log_directory: Path) -> Path:
    return log_directory / LOGS_DIRECTORY_NAME

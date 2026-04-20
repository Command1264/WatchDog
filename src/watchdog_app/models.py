from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import uuid


APP_NAME = "WatchDog"
CONFIG_FILE_NAME = "config.json"
BOOTSTRAP_FILE_NAME = "bootstrap.json"
LOGS_DIRECTORY_NAME = "WatchDogLogs"
MIN_INTERVAL_SECONDS = 0.05


class WatchDogError(Exception):
    """Base exception for the application."""


class ConfigValidationError(WatchDogError):
    """Raised when configuration data is invalid."""


class ExitReason(Enum):
    USER_EXIT = 10
    CTRL_C_EXIT = 11
    OS_SESSION_END = 12
    SECONDARY_INSTANCE = 13
    CRITICAL_EXCEPTION = 20
    UNEXPECTED_TERMINATION = 21

    @classmethod
    def from_exit_code(cls, value: int) -> ExitReason | None:
        for reason in cls:
            if reason.value == value:
                return reason
        return None


class LaunchKind(str, Enum):
    AUTO = "auto"
    EXE = "exe"
    CMD = "cmd"
    POWERSHELL = "powershell"
    PYTHON = "python"


class CheckLogic(str, Enum):
    ALL = "ALL"
    ANY = "ANY"


class CheckType(str, Enum):
    RUNTIME_PID = "runtime_pid"
    PIDFILE = "pidfile"
    PROCESS_NAME = "process_name"
    TCP_PORT = "tcp_port"
    HTTP_ENDPOINT = "http_endpoint"


class StorageMode(str, Enum):
    EXE = "exe"
    APPDATA = "appdata"
    LOCALAPPDATA = "localappdata"
    CUSTOM = "custom"


class AutoStartScope(str, Enum):
    DISABLED = "disabled"
    CURRENT_USER = "current_user"
    ALL_USERS = "all_users"


class AutoStartProvider(str, Enum):
    NONE = "none"
    REGISTRY_RUN = "registry_run"
    SCHEDULED_TASK = "scheduled_task"


def _coerce_enum(enum_type: type[Enum], value: Enum | str) -> Enum:
    if isinstance(value, enum_type):
        return value
    return enum_type(value)


def _coerce_bool(value: Any, field_name: str, *, default: bool | None = None) -> bool:
    if value is None:
        if default is not None:
            return default
        raise ConfigValidationError(f"{field_name} 不可為空值。")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", ""}:
            return False
    raise ConfigValidationError(f"{field_name} 的布林值格式無效：{value!r}")


def normalize_separators(value: str) -> str:
    return value.replace("\\", "/")


def normalize_path_text(value: str | Path) -> str:
    return normalize_separators(str(value)).strip()


def _validate_min_interval(value: float, field_name: str) -> float:
    if value < MIN_INTERVAL_SECONDS:
        raise ConfigValidationError(
            f"{field_name} 必須大於或等於 {MIN_INTERVAL_SECONDS:.2f} 秒。"
        )
    return value


def _as_existing_parent(path_text: str, field_name: str) -> str:
    if not path_text.strip():
        raise ConfigValidationError(f"{field_name} 不可為空白。")
    return path_text.strip()


def _as_path_text(path_text: str, field_name: str) -> str:
    normalized = normalize_path_text(path_text)
    if not normalized:
        raise ConfigValidationError(f"{field_name} 不可為空白。")
    return normalized


def _as_loopback_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigValidationError("伺服器檢查只允許使用 loopback 主機。")
    return normalized


def _as_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigValidationError("HTTP 端點檢查只接受 http 或 https URL。")
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigValidationError("HTTP 端點檢查只支援 loopback 主機。")
    return url


@dataclass(slots=True)
class LaunchSpec:
    path: str
    args: list[str] = field(default_factory=list)
    working_dir: str = ""
    kind: LaunchKind = LaunchKind.AUTO

    def validate(self) -> LaunchSpec:
        self.path = _as_path_text(self.path, "launch.path")
        self.kind = _coerce_enum(LaunchKind, self.kind)  # type: ignore[assignment]
        if self.working_dir:
            self.working_dir = normalize_path_text(self.working_dir)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "args": list(self.args),
            "working_dir": self.working_dir,
            "kind": self.kind.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LaunchSpec:
        spec = cls(
            path=str(data.get("path", "")).strip(),
            args=[str(item) for item in data.get("args", [])],
            working_dir=str(data.get("working_dir", "")).strip(),
            kind=LaunchKind(str(data.get("kind", LaunchKind.AUTO.value))),
        )
        return spec.validate()


@dataclass(slots=True)
class CheckSpec:
    type: CheckType
    label: str = ""
    pidfile_path: str = ""
    process_name: str = ""
    executable_path: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    url: str = ""
    method: str = "GET"
    timeout_sec: float = 1.0
    expected_status: int = 200
    body_substring: str = ""

    def validate(self) -> CheckSpec:
        self.type = _coerce_enum(CheckType, self.type)  # type: ignore[assignment]
        self.label = self.label.strip()
        self.timeout_sec = _validate_min_interval(float(self.timeout_sec), "check.timeout_sec")
        if self.type == CheckType.PIDFILE:
            self.pidfile_path = _as_path_text(self.pidfile_path, "check.pidfile_path")
        elif self.type == CheckType.PROCESS_NAME:
            self.process_name = _as_existing_parent(self.process_name, "check.process_name")
            self.executable_path = normalize_path_text(self.executable_path)
        elif self.type == CheckType.TCP_PORT:
            self.host = _as_loopback_host(self.host)
            if not (1 <= int(self.port) <= 65535):
                raise ConfigValidationError("TCP 連接埠必須介於 1 到 65535 之間。")
            self.port = int(self.port)
        elif self.type == CheckType.HTTP_ENDPOINT:
            self.url = _as_http_url(self.url)
            self.method = (self.method or "GET").upper()
            self.expected_status = int(self.expected_status)
        return self

    def summary(self) -> str:
        if self.type == CheckType.RUNTIME_PID:
            return "WatchDog 啟動的 PID"
        if self.type == CheckType.PIDFILE:
            return f"PID 檔案：{self.pidfile_path}"
        if self.type == CheckType.PROCESS_NAME:
            if self.executable_path:
                return f"程序名稱：{self.process_name} @ {self.executable_path}"
            return f"程序名稱：{self.process_name}"
        if self.type == CheckType.TCP_PORT:
            return f"TCP：{self.host}:{self.port}"
        return f"HTTP：{self.method} {self.url}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "label": self.label,
            "pidfile_path": self.pidfile_path,
            "process_name": self.process_name,
            "executable_path": self.executable_path,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "method": self.method,
            "timeout_sec": self.timeout_sec,
            "expected_status": self.expected_status,
            "body_substring": self.body_substring,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckSpec:
        check = cls(
            type=CheckType(str(data.get("type", CheckType.RUNTIME_PID.value))),
            label=str(data.get("label", "")),
            pidfile_path=str(data.get("pidfile_path", "")),
            process_name=str(data.get("process_name", "")),
            executable_path=str(data.get("executable_path", "")),
            host=str(data.get("host", "127.0.0.1")),
            port=int(data.get("port", 0)),
            url=str(data.get("url", "")),
            method=str(data.get("method", "GET")),
            timeout_sec=float(data.get("timeout_sec", 1.0)),
            expected_status=int(data.get("expected_status", 200)),
            body_substring=str(data.get("body_substring", "")),
        )
        return check.validate()


@dataclass(slots=True)
class TargetConfig:
    id: str
    name: str
    enabled: bool
    launch: LaunchSpec
    startup_delay_sec: float = 0.05
    check_interval_sec: float = 1.0
    restart_cooldown_sec: float = 1.0
    check_logic: CheckLogic = CheckLogic.ALL
    checks: list[CheckSpec] = field(default_factory=list)

    def validate(self) -> TargetConfig:
        self.id = self.id.strip() or uuid.uuid4().hex
        self.name = _as_existing_parent(self.name, "target.name")
        self.launch.validate()
        self.check_logic = _coerce_enum(CheckLogic, self.check_logic)  # type: ignore[assignment]
        self.startup_delay_sec = _validate_min_interval(
            float(self.startup_delay_sec), "target.startup_delay_sec"
        )
        self.check_interval_sec = _validate_min_interval(
            float(self.check_interval_sec), "target.check_interval_sec"
        )
        self.restart_cooldown_sec = _validate_min_interval(
            float(self.restart_cooldown_sec), "target.restart_cooldown_sec"
        )
        if not self.checks:
            self.checks = [CheckSpec(type=CheckType.RUNTIME_PID)]
        self.checks = [check.validate() for check in self.checks]
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "launch": self.launch.to_dict(),
            "startup_delay_sec": self.startup_delay_sec,
            "check_interval_sec": self.check_interval_sec,
            "restart_cooldown_sec": self.restart_cooldown_sec,
            "check_logic": self.check_logic.value,
            "checks": [check.to_dict() for check in self.checks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TargetConfig:
        target = cls(
            id=str(data.get("id", "")).strip() or uuid.uuid4().hex,
            name=str(data.get("name", "")).strip(),
            enabled=_coerce_bool(data.get("enabled"), "target.enabled", default=True),
            launch=LaunchSpec.from_dict(dict(data.get("launch", {}))),
            startup_delay_sec=float(data.get("startup_delay_sec", MIN_INTERVAL_SECONDS)),
            check_interval_sec=float(data.get("check_interval_sec", 1.0)),
            restart_cooldown_sec=float(data.get("restart_cooldown_sec", 1.0)),
            check_logic=CheckLogic(str(data.get("check_logic", CheckLogic.ALL.value))),
            checks=[CheckSpec.from_dict(item) for item in data.get("checks", [])],
        )
        return target.validate()


@dataclass(slots=True)
class StoragePreferences:
    config_mode: StorageMode = StorageMode.APPDATA
    log_mode: StorageMode = StorageMode.LOCALAPPDATA
    config_custom_path: str = ""
    log_custom_path: str = ""

    def validate(self) -> StoragePreferences:
        self.config_mode = _coerce_enum(StorageMode, self.config_mode)  # type: ignore[assignment]
        self.log_mode = _coerce_enum(StorageMode, self.log_mode)  # type: ignore[assignment]
        self.config_custom_path = normalize_path_text(self.config_custom_path)
        self.log_custom_path = normalize_path_text(self.log_custom_path)
        if self.config_mode == StorageMode.CUSTOM:
            self.config_custom_path = _as_path_text(
                self.config_custom_path,
                "storage.config_custom_path",
            )
        if self.log_mode == StorageMode.CUSTOM:
            self.log_custom_path = _as_path_text(
                self.log_custom_path,
                "storage.log_custom_path",
            )
        return self

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {
            "config_mode": self.config_mode.value,
            "log_mode": self.log_mode.value,
            "config_custom_path": self.config_custom_path,
            "log_custom_path": self.log_custom_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoragePreferences:
        return cls(
            config_mode=StorageMode(str(data.get("config_mode", StorageMode.APPDATA.value))),
            log_mode=StorageMode(str(data.get("log_mode", StorageMode.LOCALAPPDATA.value))),
            config_custom_path=str(data.get("config_custom_path", "")),
            log_custom_path=str(data.get("log_custom_path", "")),
        )


@dataclass(slots=True)
class AppConfig:
    storage: StoragePreferences = field(default_factory=StoragePreferences)
    auto_start_scope: AutoStartScope = AutoStartScope.DISABLED
    auto_start_provider: AutoStartProvider = AutoStartProvider.NONE
    start_monitoring_on_login: bool = False
    minimize_to_tray: bool = True
    targets: list[TargetConfig] = field(default_factory=list)

    def validate(self) -> AppConfig:
        self.storage = self.storage.validate()
        self.auto_start_scope = _coerce_enum(  # type: ignore[assignment]
            AutoStartScope,
            self.auto_start_scope,
        )
        self.auto_start_provider = _coerce_enum(  # type: ignore[assignment]
            AutoStartProvider,
            self.auto_start_provider,
        )
        self.targets = [target.validate() for target in self.targets]
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage": self.storage.to_dict(),
            "auto_start_scope": self.auto_start_scope.value,
            "auto_start_provider": self.auto_start_provider.value,
            "start_monitoring_on_login": self.start_monitoring_on_login,
            "minimize_to_tray": self.minimize_to_tray,
            "targets": [target.to_dict() for target in self.targets],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        config = cls(
            storage=StoragePreferences.from_dict(dict(data.get("storage", {}))),
            auto_start_scope=AutoStartScope(
                str(data.get("auto_start_scope", AutoStartScope.DISABLED.value))
            ),
            auto_start_provider=AutoStartProvider(
                str(data.get("auto_start_provider", AutoStartProvider.NONE.value))
            ),
            start_monitoring_on_login=_coerce_bool(
                data.get("start_monitoring_on_login"),
                "app.start_monitoring_on_login",
                default=False,
            ),
            minimize_to_tray=_coerce_bool(
                data.get("minimize_to_tray"),
                "app.minimize_to_tray",
                default=True,
            ),
            targets=[TargetConfig.from_dict(item) for item in data.get("targets", [])],
        )
        return config.validate()

    @classmethod
    def default(cls) -> AppConfig:
        return cls().validate()


@dataclass(slots=True)
class BootstrapState:
    storage: StoragePreferences | None = None
    config_path: str = ""
    log_directory: str = ""
    first_run_completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage": self.storage.to_dict() if self.storage else None,
            "config_path": normalize_path_text(self.config_path),
            "log_directory": normalize_path_text(self.log_directory),
            "first_run_completed": self.first_run_completed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BootstrapState:
        raw_storage = data.get("storage")
        storage = (
            StoragePreferences.from_dict(dict(raw_storage))
            if isinstance(raw_storage, dict)
            else None
        )
        return cls(
            storage=storage,
            config_path=normalize_path_text(str(data.get("config_path", ""))),
            log_directory=normalize_path_text(str(data.get("log_directory", ""))),
            first_run_completed=_coerce_bool(
                data.get("first_run_completed"),
                "bootstrap.first_run_completed",
                default=False,
            ),
        )


@dataclass(slots=True)
class ResolvedPaths:
    bootstrap_path: Path
    config_path: Path
    log_directory: Path
    config_fallback_used: bool = False
    log_fallback_used: bool = False

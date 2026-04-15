from __future__ import annotations

import pytest

from watchdog_app.models import (
    AppConfig,
    AutoStartProvider,
    AutoStartScope,
    CheckLogic,
    CheckSpec,
    CheckType,
    ConfigValidationError,
    LaunchKind,
    LaunchSpec,
    StoragePreferences,
    StorageMode,
    TargetConfig,
)


def test_target_requires_minimum_intervals() -> None:
    with pytest.raises(ConfigValidationError):
        TargetConfig(
            id="alpha",
            name="Alpha",
            enabled=True,
            launch=LaunchSpec(path="C:/demo.exe", kind=LaunchKind.EXE),
            startup_delay_sec=0.01,
        ).validate()


def test_http_check_rejects_non_loopback_hosts() -> None:
    with pytest.raises(ConfigValidationError):
        CheckSpec(type=CheckType.HTTP_ENDPOINT, url="http://example.com/health").validate()


def test_app_config_round_trip() -> None:
    config = AppConfig.from_dict(
        {
            "targets": [
                {
                    "id": "alpha",
                    "name": "Alpha",
                    "enabled": True,
                    "launch": {
                        "path": "C:/demo.exe",
                        "args": ["--ok"],
                        "working_dir": "C:/",
                        "kind": "exe",
                    },
                    "startup_delay_sec": 0.1,
                    "check_interval_sec": 1.25,
                    "restart_cooldown_sec": 2.0,
                    "check_logic": "ANY",
                    "checks": [{"type": "runtime_pid"}],
                }
            ]
        }
    )

    raw = config.to_dict()
    restored = AppConfig.from_dict(raw)

    assert restored.targets[0].check_logic == CheckLogic.ANY
    assert restored.targets[0].launch.args == ["--ok"]
    assert restored.targets[0].checks[0].type == CheckType.RUNTIME_PID


def test_string_backed_enums_from_ui_are_normalized() -> None:
    config = AppConfig(
        storage=StoragePreferences(config_mode="exe", log_mode="localappdata"),
        auto_start_scope="current_user",
        auto_start_provider="scheduled_task",
        targets=[
            TargetConfig(
                id="alpha",
                name="Alpha",
                enabled=True,
                launch=LaunchSpec(path="C:/demo.exe", kind="exe"),
                check_logic="ANY",
                checks=[CheckSpec(type="runtime_pid")],
            )
        ],
    ).validate()

    assert config.storage.config_mode == StorageMode.EXE
    assert config.storage.log_mode == StorageMode.LOCALAPPDATA
    assert config.auto_start_scope == AutoStartScope.CURRENT_USER
    assert config.auto_start_provider == AutoStartProvider.SCHEDULED_TASK
    assert config.targets[0].launch.kind == LaunchKind.EXE
    assert config.targets[0].check_logic == CheckLogic.ANY
    assert config.targets[0].checks[0].type == CheckType.RUNTIME_PID
    assert config.to_dict()["storage"]["config_mode"] == "exe"


def test_boolean_like_strings_are_parsed_correctly_from_config() -> None:
    config = AppConfig.from_dict(
        {
            "start_monitoring_on_login": "true",
            "minimize_to_tray": "0",
            "targets": [
                {
                    "id": "alpha",
                    "name": "Alpha",
                    "enabled": "false",
                    "launch": {
                        "path": "C:/demo.exe",
                        "args": [],
                        "working_dir": "",
                        "kind": "exe",
                    },
                    "checks": [{"type": "runtime_pid"}],
                },
                {
                    "id": "beta",
                    "name": "Beta",
                    "enabled": "1",
                    "launch": {
                        "path": "C:/demo2.exe",
                        "args": [],
                        "working_dir": "",
                        "kind": "exe",
                    },
                    "checks": [{"type": "runtime_pid"}],
                },
            ],
        }
    )

    assert config.start_monitoring_on_login is True
    assert config.minimize_to_tray is False
    assert config.targets[0].enabled is False
    assert config.targets[1].enabled is True

from __future__ import annotations

import logging
import sys

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon, QWidget

from watchdog_app.app import (
    AppController,
    LeftClickOnlyMenu,
    _install_exception_hooks,
    _load_bootstrap_state_with_recovery,
    _load_config_with_recovery,
)
from watchdog_app.autostart import AutoStartStatus
from watchdog_app.models import (
    AppConfig,
    AutoStartProvider,
    AutoStartScope,
    BootstrapState,
    ExitReason,
    ResolvedPaths,
    StoragePreferences,
)


class DummyMonitorEngine:
    def __init__(self, *args, **kwargs) -> None:
        self._running = False
        self.config = args[0] if args else None

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def manual_launch(self, target_id: str) -> None:
        return None

    def test_target(self, target_id: str):
        raise AssertionError("test_target should not be called in this test")

    def set_config(self, config) -> None:
        self.config = config

    def shutdown(self) -> None:
        self._running = False


class DummyMainWindow(QWidget):
    config_changed = Signal(object)
    manual_launch_requested = Signal(str)
    test_requested = Signal(str)
    system_settings_requested = Signal()
    user_exit_requested = Signal()

    def __init__(self, config: AppConfig, resolved_paths: ResolvedPaths) -> None:
        super().__init__()
        self.config = config
        self.resolved_paths = resolved_paths
        self.unsaved_changes = False
        self.save_pending_result = True
        self.save_calls = 0

    def set_monitoring_running(self, running: bool) -> None:
        return None

    def set_resolved_paths(self, resolved_paths: ResolvedPaths) -> None:
        self.resolved_paths = resolved_paths

    def set_config(self, config: AppConfig) -> None:
        self.config = config

    def apply_monitor_event(self, event) -> None:
        return None

    def has_unsaved_changes(self) -> bool:
        return self.unsaved_changes

    def save_pending_changes(self) -> bool:
        self.save_calls += 1
        return self.save_pending_result


class DummySingleInstance(QObject):
    show_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DummyTrayIcon(QObject):
    activated = Signal(object)
    ActivationReason = QSystemTrayIcon.ActivationReason
    last_created: DummyTrayIcon | None = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__()
        self.tooltip = ""
        self.set_context_menu_calls = 0
        self.context_menu = None
        self.icon = args[0] if args else None
        DummyTrayIcon.last_created = self

    def setToolTip(self, text: str) -> None:
        self.tooltip = text

    def setIcon(self, icon) -> None:
        self.icon = icon

    def setContextMenu(self, menu) -> None:
        self.set_context_menu_calls += 1
        self.context_menu = menu

    def show(self) -> None:
        return None

    def hide(self) -> None:
        return None


class DummySystemSettingsDialog:
    def __init__(self, *args, **kwargs) -> None:
        return None

    def exec(self) -> bool:
        return True

    def values(self) -> tuple[StoragePreferences, AutoStartScope, bool]:
        return StoragePreferences(), AutoStartScope.DISABLED, False


class DummyChangingSystemSettingsDialog(DummySystemSettingsDialog):
    def values(self) -> tuple[StoragePreferences, AutoStartScope, bool]:
        return (
            StoragePreferences(config_mode="exe", log_mode="exe"),
            AutoStartScope.CURRENT_USER,
            True,
        )


def _solid_icon(color: Qt.GlobalColor) -> QIcon:
    pixmap = QPixmap(16, 16)
    pixmap.fill(color)
    return QIcon(pixmap)


def test_left_click_only_menu_ignores_right_click(qtbot) -> None:
    menu = LeftClickOnlyMenu()
    action = menu.addAction("結束")
    triggered: list[str] = []
    action.triggered.connect(lambda: triggered.append("triggered"))

    menu.popup(QPoint(240, 240))
    qtbot.waitUntil(lambda: menu.isVisible())
    action_rect = menu.actionGeometry(action)

    QTest.mouseClick(
        menu,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
        action_rect.center(),
    )
    assert triggered == []

    if not menu.isVisible():
        menu.popup(QPoint(240, 240))
        qtbot.waitUntil(lambda: menu.isVisible())
        action_rect = menu.actionGeometry(action)

    QTest.mouseClick(
        menu,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        action_rect.center(),
    )
    assert triggered == ["triggered"]


def test_app_controller_uses_manual_tray_menu_popup_for_context(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    tray = DummyTrayIcon.last_created
    assert tray is not None
    assert tray.set_context_menu_calls == 0
    assert tray.context_menu is None
    assert isinstance(controller._tray_menu, LeftClickOnlyMenu)

    events: list[str] = []
    controller._show_tray_menu = lambda: events.append("menu")  # type: ignore[method-assign]
    controller.show_settings_window = lambda: events.append("window")  # type: ignore[method-assign]

    controller._handle_tray_activated(QSystemTrayIcon.ActivationReason.Context)
    controller._handle_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)

    assert events == ["menu", "window"]


def test_tray_action_is_blocked_without_valid_left_click_token(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller._toggle_action.trigger()

    assert controller._monitor.is_running() is False


def test_tray_action_without_token_is_blocked_during_popup_guard_window(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )

    current_time = [100.0]
    monkeypatch.setattr("watchdog_app.app.time.monotonic", lambda: current_time[0])

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller._tray_action_guard.mark_popup_started()
    controller._toggle_action.trigger()

    assert controller._monitor.is_running() is False


def test_tray_action_runs_with_valid_left_click_even_inside_popup_guard_window(
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )

    current_time = [100.0]
    monkeypatch.setattr("watchdog_app.app.time.monotonic", lambda: current_time[0])

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller._tray_action_guard.mark_popup_started()
    controller._tray_action_guard.note_press(controller._toggle_action, Qt.MouseButton.LeftButton)
    controller._tray_action_guard.note_release(controller._toggle_action, Qt.MouseButton.LeftButton)
    controller._toggle_action.trigger()

    assert controller._monitor.is_running() is True


def test_tray_menu_hide_defers_token_reset_until_next_event_loop(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )

    current_time = [100.0]
    monkeypatch.setattr("watchdog_app.app.time.monotonic", lambda: current_time[0])

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller._tray_action_guard.mark_popup_started()
    controller._tray_action_guard.note_press(controller._toggle_action, Qt.MouseButton.LeftButton)
    controller._tray_action_guard.note_release(controller._toggle_action, Qt.MouseButton.LeftButton)
    controller._handle_tray_menu_about_to_hide()
    controller._toggle_action.trigger()

    assert controller._monitor.is_running() is True

    qtbot.wait(0)
    controller._toggle_action.trigger()

    assert controller._monitor.is_running() is True


def test_tray_menu_popup_offsets_and_ignores_reentry(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr("watchdog_app.app.QCursor.pos", lambda: QPoint(120, 220))

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    popups: list[QPoint] = []
    monkeypatch.setattr(controller._tray_menu, "popup", lambda point: popups.append(point))

    controller._show_tray_menu()
    controller._show_tray_menu()
    controller._handle_tray_menu_about_to_hide()
    controller._show_tray_menu()

    assert popups == [QPoint(120, 228), QPoint(120, 228)]


def test_existing_instance_show_request_reloads_config_before_showing(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )

    initial = AppConfig.default()
    reloaded = AppConfig.from_dict(
        {
            "targets": [
                {
                    "id": "alpha",
                    "name": "Reloaded",
                    "enabled": False,
                    "launch": {"path": "C:/reloaded.exe", "args": [], "working_dir": "", "kind": "auto"},
                    "checks": [{"type": "runtime_pid"}],
                }
            ]
        }
    )
    monkeypatch.setattr("watchdog_app.app.load_config", lambda path: reloaded)

    controller = AppController(
        app,
        initial,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    shown: list[str] = []
    controller.show_settings_window = lambda: shown.append("shown")  # type: ignore[method-assign]

    controller._single_instance.show_requested.emit()

    assert shown == ["shown"]
    assert controller._window.config.targets[0].name == "Reloaded"
    assert controller._monitor.config.targets[0].name == "Reloaded"


def test_load_bootstrap_state_with_recovery_moves_invalid_file_aside(monkeypatch, tmp_path) -> None:
    bootstrap_file = tmp_path / "bootstrap.json"
    bootstrap_file.write_text("{ invalid", encoding="utf-8")

    def _raise():
        raise ValueError("broken bootstrap")

    monkeypatch.setattr("watchdog_app.app.bootstrap_path", lambda: bootstrap_file)
    monkeypatch.setattr("watchdog_app.app.load_bootstrap_state", _raise)

    state, warning = _load_bootstrap_state_with_recovery()

    assert state == BootstrapState()
    assert warning is not None
    assert "啟動設定檔讀取失敗" in warning
    assert not bootstrap_file.exists()
    assert len(list(tmp_path.glob("bootstrap.invalid.*.json"))) == 1


def test_load_bootstrap_state_with_recovery_logs_warning(monkeypatch, tmp_path, caplog) -> None:
    bootstrap_file = tmp_path / "bootstrap.json"
    bootstrap_file.write_text("{ invalid", encoding="utf-8")

    def _raise():
        raise ValueError("broken bootstrap")

    monkeypatch.setattr("watchdog_app.app.bootstrap_path", lambda: bootstrap_file)
    monkeypatch.setattr("watchdog_app.app.load_bootstrap_state", _raise)

    with caplog.at_level(logging.WARNING):
        _load_bootstrap_state_with_recovery()

    records = [record for record in caplog.records if record.name == "watchdog_app.app"]
    assert any(record.levelno == logging.WARNING for record in records)
    assert not any(record.levelno >= logging.ERROR for record in records)


def test_load_config_with_recovery_moves_invalid_file_aside(monkeypatch, tmp_path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text("{ invalid", encoding="utf-8")

    def _raise(path):
        raise ValueError(f"broken config: {path}")

    monkeypatch.setattr("watchdog_app.app.load_config", _raise)

    config, warning = _load_config_with_recovery(config_file)

    assert config == AppConfig.default()
    assert warning is not None
    assert "設定檔讀取失敗" in warning
    assert not config_file.exists()
    assert len(list(tmp_path.glob("config.invalid.*.json"))) == 1


def test_exception_hook_logs_error_and_exits(monkeypatch, caplog) -> None:
    exit_codes: list[int] = []
    monkeypatch.setattr("watchdog_app.app.logging.shutdown", lambda: None)

    def _exit(code: int) -> None:
        exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("watchdog_app.app.os._exit", _exit)

    old_sys, old_thread = _install_exception_hooks()
    try:
        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc_info:
                sys.excepthook(ValueError, ValueError("boom"), None)
    finally:
        sys.excepthook = old_sys
        import threading

        threading.excepthook = old_thread

    assert exc_info.value.code == ExitReason.CRITICAL_EXCEPTION.value
    assert exit_codes == [ExitReason.CRITICAL_EXCEPTION.value]
    records = [record for record in caplog.records if record.name == "watchdog_app.app"]
    assert any(record.levelno == logging.ERROR for record in records)
    assert not any(record.levelno == logging.CRITICAL for record in records)


def test_open_system_settings_dialog_persists_disabled_provider_as_none(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr("watchdog_app.app.SystemSettingsDialog", DummySystemSettingsDialog)
    monkeypatch.setattr(
        "watchdog_app.app.apply_autostart",
        lambda scope: AutoStartStatus(scope=scope, provider=None, enabled=False),
    )
    monkeypatch.setattr(
        "watchdog_app.app.update_bootstrap_for_storage",
        lambda storage: ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    monkeypatch.setattr("watchdog_app.app.configure_logging", lambda *_args, **_kwargs: None)

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller.open_system_settings_dialog()

    assert controller._config.auto_start_scope == AutoStartScope.DISABLED
    assert controller._config.auto_start_provider == AutoStartProvider.NONE


def test_open_system_settings_dialog_rolls_back_partial_side_effects_when_save_fails(
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr("watchdog_app.app.SystemSettingsDialog", DummyChangingSystemSettingsDialog)
    monkeypatch.setattr(
        "watchdog_app.app.resolve_paths",
        lambda _storage: ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )

    autostart_calls: list[AutoStartScope] = []
    monkeypatch.setattr(
        "watchdog_app.app.apply_autostart",
        lambda scope: (
            autostart_calls.append(scope),
            AutoStartStatus(scope=scope, provider=AutoStartProvider.REGISTRY_RUN, enabled=True),
        )[1],
    )

    save_calls: list[tuple[str, str]] = []

    def _save(config, path):
        save_calls.append((config.auto_start_scope.value, str(path)))
        if len(save_calls) == 1:
            raise OSError("disk full")
        return path

    monkeypatch.setattr("watchdog_app.app.save_config", _save)

    bootstrap_calls: list[StoragePreferences] = []
    monkeypatch.setattr(
        "watchdog_app.app.update_bootstrap_for_storage",
        lambda storage: (
            bootstrap_calls.append(storage),
            ResolvedPaths(
                bootstrap_path=tmp_path / "bootstrap.json",
                config_path=tmp_path / "config.json",
                log_directory=tmp_path / "logs",
            ),
        )[1],
    )

    configured_logs: list[str] = []
    monkeypatch.setattr(
        "watchdog_app.app.configure_logging",
        lambda directory: configured_logs.append(str(directory)),
    )

    warnings: list[str] = []
    monkeypatch.setattr(
        "watchdog_app.app.QMessageBox.warning",
        lambda _parent, _title, text: warnings.append(text),
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller.open_system_settings_dialog()

    assert autostart_calls == [AutoStartScope.CURRENT_USER, AutoStartScope.DISABLED]
    assert save_calls == [
        ("current_user", str(tmp_path / "config.json")),
        ("disabled", str(tmp_path / "config.json")),
    ]
    assert bootstrap_calls == [StoragePreferences()]
    assert configured_logs == [str(tmp_path / "logs")]
    assert controller._config.auto_start_scope == AutoStartScope.DISABLED
    assert controller._config.start_monitoring_on_login is False
    assert warnings == ["系統設定儲存失敗：disk full"]


def test_open_system_settings_dialog_persists_effective_storage_modes_after_fallback(
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr("watchdog_app.app.SystemSettingsDialog", DummyChangingSystemSettingsDialog)
    monkeypatch.setattr(
        "watchdog_app.app.apply_autostart",
        lambda scope: AutoStartStatus(scope=scope, provider=AutoStartProvider.REGISTRY_RUN, enabled=True),
    )
    resolved = ResolvedPaths(
        bootstrap_path=tmp_path / "bootstrap.json",
        config_path=tmp_path / "appdata" / "config.json",
        log_directory=tmp_path / "localappdata",
        config_fallback_used=True,
        log_fallback_used=True,
    )
    monkeypatch.setattr("watchdog_app.app.resolve_paths", lambda _storage: resolved)
    monkeypatch.setattr(
        "watchdog_app.app.effective_storage_preferences",
        lambda _resolved: StoragePreferences(config_mode="appdata", log_mode="localappdata"),
    )
    monkeypatch.setattr("watchdog_app.app.update_bootstrap_for_storage", lambda _storage: resolved)
    monkeypatch.setattr("watchdog_app.app.configure_logging", lambda *_args, **_kwargs: None)

    saved: list[AppConfig] = []
    monkeypatch.setattr(
        "watchdog_app.app.save_config",
        lambda config, path: saved.append(AppConfig.from_dict(config.to_dict())) or path,
    )

    warnings: list[str] = []
    monkeypatch.setattr(
        "watchdog_app.app.QMessageBox.warning",
        lambda _parent, _title, text: warnings.append(text),
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller.open_system_settings_dialog()

    assert saved[0].storage == StoragePreferences(config_mode="appdata", log_mode="localappdata")
    assert controller._config.storage == StoragePreferences(config_mode="appdata", log_mode="localappdata")
    assert controller._window.config.storage == StoragePreferences(
        config_mode="appdata",
        log_mode="localappdata",
    )
    assert warnings[-1] == "指定的 .exe 所在路徑不可寫入，已自動回退到 AppData/LocalAppData。"


def test_open_system_settings_dialog_removes_staged_config_when_later_step_fails(
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr("watchdog_app.app.SystemSettingsDialog", DummyChangingSystemSettingsDialog)

    staged_path = tmp_path / "staged" / "config.json"
    previous_path = tmp_path / "current" / "config.json"
    previous_path.parent.mkdir(parents=True, exist_ok=True)
    previous_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "watchdog_app.app.resolve_paths",
        lambda _storage: ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=staged_path,
            log_directory=tmp_path / "staged-logs",
        ),
    )
    monkeypatch.setattr(
        "watchdog_app.app.effective_storage_preferences",
        lambda _resolved: StoragePreferences(config_mode="exe", log_mode="exe"),
    )
    monkeypatch.setattr(
        "watchdog_app.app.apply_autostart",
        lambda scope: AutoStartStatus(scope=scope, provider=AutoStartProvider.REGISTRY_RUN, enabled=True),
    )

    def _save(config, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config.to_dict()["auto_start_scope"], encoding="utf-8")
        return path

    monkeypatch.setattr("watchdog_app.app.save_config", _save)

    update_calls = {"count": 0}

    def _update(storage):
        update_calls["count"] += 1
        if update_calls["count"] == 1:
            raise OSError("bootstrap failed")
        return ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=previous_path,
            log_directory=tmp_path / "current-logs",
        )

    monkeypatch.setattr("watchdog_app.app.update_bootstrap_for_storage", _update)
    monkeypatch.setattr("watchdog_app.app.configure_logging", lambda *_args, **_kwargs: None)

    warnings: list[str] = []
    monkeypatch.setattr(
        "watchdog_app.app.QMessageBox.warning",
        lambda _parent, _title, text: warnings.append(text),
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=previous_path,
            log_directory=tmp_path / "current-logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller.open_system_settings_dialog()

    assert staged_path.exists() is False
    assert previous_path.read_text(encoding="utf-8") == "disabled"
    assert warnings == ["系統設定儲存失敗：bootstrap failed"]


def test_apply_config_failure_restores_last_good_config_and_shows_warning(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr("watchdog_app.app.save_config", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    warnings: list[str] = []
    monkeypatch.setattr(
        "watchdog_app.app.QMessageBox.warning",
        lambda _parent, _title, text: warnings.append(text),
    )

    initial = AppConfig.from_dict(
        {
            "targets": [
                {
                    "id": "alpha",
                    "name": "Original",
                    "enabled": True,
                    "launch": {"path": "C:/original.exe", "args": [], "working_dir": "", "kind": "exe"},
                    "checks": [{"type": "runtime_pid"}],
                }
            ]
        }
    )
    changed = AppConfig.from_dict(
        {
            "targets": [
                {
                    "id": "alpha",
                    "name": "Changed",
                    "enabled": False,
                    "launch": {"path": "C:/changed.exe", "args": [], "working_dir": "", "kind": "exe"},
                    "checks": [{"type": "runtime_pid"}],
                }
            ]
        }
    )

    controller = AppController(
        app,
        initial,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    controller.apply_config(changed)

    assert controller._config.targets[0].name == "Original"
    assert controller._window.config.targets[0].name == "Original"
    assert controller._monitor.config.targets[0].name == "Original"
    assert warnings == ["無法儲存設定：disk full"]


def test_existing_instance_show_request_uses_recovery_loader(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )

    initial = AppConfig.from_dict(
        {
            "targets": [
                {
                    "id": "alpha",
                    "name": "Original",
                    "enabled": True,
                    "launch": {"path": "C:/original.exe", "args": [], "working_dir": "", "kind": "exe"},
                    "checks": [{"type": "runtime_pid"}],
                }
            ]
        }
    )
    recovered = AppConfig.from_dict(
        {
            "targets": [
                {
                    "id": "beta",
                    "name": "Recovered",
                    "enabled": False,
                    "launch": {"path": "C:/recovered.exe", "args": [], "working_dir": "", "kind": "exe"},
                    "checks": [{"type": "runtime_pid"}],
                }
            ]
        }
    )
    save_calls: list[tuple[str, str]] = []
    warnings: list[str] = []
    monkeypatch.setattr(
        "watchdog_app.app._load_config_with_recovery",
        lambda path: (recovered, "設定檔已修復"),
    )
    monkeypatch.setattr(
        "watchdog_app.app.save_config",
        lambda config, path: save_calls.append((config.targets[0].name, str(path))),
    )
    monkeypatch.setattr(
        "watchdog_app.app.QMessageBox.warning",
        lambda _parent, _title, text: warnings.append(text),
    )

    controller = AppController(
        app,
        initial,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    shown: list[str] = []
    controller.show_settings_window = lambda: shown.append("shown")  # type: ignore[method-assign]

    controller._single_instance.show_requested.emit()

    assert shown == ["shown"]
    assert controller._config.targets[0].name == "Recovered"
    assert controller._window.config.targets[0].name == "Recovered"
    assert controller._monitor.config.targets[0].name == "Recovered"
    assert save_calls == [("Recovered", str(tmp_path / "config.json"))]
    assert warnings == ["設定檔已修復"]


def test_exit_user_cancels_when_unsaved_changes_prompt_is_canceled(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr(
        "watchdog_app.app.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Cancel,
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)
    controller._window.unsaved_changes = True

    controller.exit_user()

    assert controller.exit_reason is None
    assert controller._window.save_calls == 0


def test_exit_user_saves_pending_changes_before_exit(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr(
        "watchdog_app.app.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)
    controller._window.unsaved_changes = True
    requested_reasons: list[ExitReason] = []
    controller._request_exit = lambda reason: requested_reasons.append(reason)  # type: ignore[method-assign]

    controller.exit_user()

    assert controller._window.save_calls == 1
    assert requested_reasons == [ExitReason.USER_EXIT]


def test_exit_user_does_not_exit_when_save_pending_changes_fails(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (_solid_icon(Qt.GlobalColor.green), _solid_icon(Qt.GlobalColor.red)),
    )
    monkeypatch.setattr(
        "watchdog_app.app.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)
    controller._window.unsaved_changes = True
    controller._window.save_pending_result = False
    requested_reasons: list[ExitReason] = []
    controller._request_exit = lambda reason: requested_reasons.append(reason)  # type: ignore[method-assign]

    controller.exit_user()

    assert controller._window.save_calls == 1
    assert requested_reasons == []


def test_app_and_tray_icon_follow_monitoring_state(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    ready_icon = _solid_icon(Qt.GlobalColor.green)
    not_ready_icon = _solid_icon(Qt.GlobalColor.red)

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
    monkeypatch.setattr(
        "watchdog_app.app.AppController._load_status_icons",
        lambda self: (ready_icon, not_ready_icon),
    )

    controller = AppController(
        app,
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        DummySingleInstance(),
    )
    qtbot.addWidget(controller._window)

    tray = DummyTrayIcon.last_created
    assert tray is not None
    assert tray.icon.cacheKey() == not_ready_icon.cacheKey()
    assert controller._window.windowIcon().cacheKey() == not_ready_icon.cacheKey()
    assert app.windowIcon().cacheKey() == not_ready_icon.cacheKey()

    controller.start_monitoring()
    assert tray.icon.cacheKey() == ready_icon.cacheKey()
    assert controller._window.windowIcon().cacheKey() == ready_icon.cacheKey()
    assert app.windowIcon().cacheKey() == ready_icon.cacheKey()

    controller.stop_monitoring()
    assert tray.icon.cacheKey() == not_ready_icon.cacheKey()

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon, QWidget

from watchdog_app.app import AppController, LeftClickOnlyMenu
from watchdog_app.models import AppConfig, ExitReason, ResolvedPaths


class DummyMonitorEngine:
    def __init__(self, *args, **kwargs) -> None:
        self._running = False
        self.config = None

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
        DummyTrayIcon.last_created = self

    def setToolTip(self, text: str) -> None:
        self.tooltip = text

    def setContextMenu(self, menu) -> None:
        self.set_context_menu_calls += 1
        self.context_menu = menu

    def show(self) -> None:
        return None

    def hide(self) -> None:
        return None


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


def test_exit_user_cancels_when_unsaved_changes_prompt_is_canceled(monkeypatch, qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None

    monkeypatch.setattr("watchdog_app.app.MonitorEngine", DummyMonitorEngine)
    monkeypatch.setattr("watchdog_app.app.MainWindow", DummyMainWindow)
    monkeypatch.setattr("watchdog_app.app.QSystemTrayIcon", DummyTrayIcon)
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

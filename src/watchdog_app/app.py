from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCursor, QIcon, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QStyle, QSystemTrayIcon

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from watchdog_app.autostart import apply_autostart
    from watchdog_app.gui.dialogs import StorageSetupDialog, SystemSettingsDialog
    from watchdog_app.gui.main_window import MainWindow
    from watchdog_app.logging_utils import configure_logging
    from watchdog_app.models import AppConfig, AutoStartProvider, ExitReason
    from watchdog_app.monitor import MonitorEngine
    from watchdog_app.single_instance import SingleInstanceCoordinator
    from watchdog_app.storage import (
        load_bootstrap_state,
        load_config,
        resolve_paths,
        save_config,
        update_bootstrap_for_storage,
    )
else:
    from .autostart import apply_autostart
    from .gui.dialogs import StorageSetupDialog, SystemSettingsDialog
    from .gui.main_window import MainWindow
    from .logging_utils import configure_logging
    from .models import AppConfig, AutoStartProvider, ExitReason
    from .monitor import MonitorEngine
    from .single_instance import SingleInstanceCoordinator
    from .storage import (
        load_bootstrap_state,
        load_config,
        resolve_paths,
        save_config,
        update_bootstrap_for_storage,
    )


logger = logging.getLogger(__name__)


class TrayActionGuard:
    def __init__(
        self,
        *,
        popup_guard_seconds: float = 0.15,
        token_timeout_seconds: float = 1.5,
    ) -> None:
        self._popup_guard_seconds = popup_guard_seconds
        self._token_timeout_seconds = token_timeout_seconds
        self._popup_started_at = 0.0
        self._pressed_action: QAction | None = None
        self._pending_action: QAction | None = None
        self._pending_started_at = 0.0

    def reset_pending_trigger(self) -> None:
        self._pressed_action = None
        self._pending_action = None
        self._pending_started_at = 0.0

    def mark_popup_started(self) -> None:
        self._popup_started_at = time.monotonic()
        self.reset_pending_trigger()

    def note_press(self, action: QAction | None, button: Qt.MouseButton) -> None:
        if button != Qt.MouseButton.LeftButton or action is None:
            self.reset_pending_trigger()
            return
        self._pressed_action = action
        self._pending_action = None
        self._pending_started_at = 0.0

    def note_release(self, action: QAction | None, button: Qt.MouseButton) -> None:
        if (
            button != Qt.MouseButton.LeftButton
            or action is None
            or action is not self._pressed_action
        ):
            self.reset_pending_trigger()
            return
        self._pending_action = action
        self._pending_started_at = time.monotonic()
        self._pressed_action = None

    def allow_trigger(self, action: QAction) -> bool:
        now = time.monotonic()
        if self._pending_action is action and (
            now - self._pending_started_at
        ) <= self._token_timeout_seconds:
            self.reset_pending_trigger()
            return True
        if (now - self._popup_started_at) < self._popup_guard_seconds:
            logger.debug(
                "Ignoring tray action during popup guard window without valid left-click token. action=%s",
                action.text(),
            )
            self.reset_pending_trigger()
            return False
        if self._pending_action is not action:
            logger.debug("Ignoring tray action without matching left-click token. action=%s", action.text())
            self.reset_pending_trigger()
            return False
        if (now - self._pending_started_at) > self._token_timeout_seconds:
            logger.debug("Ignoring stale tray action token. action=%s", action.text())
            self.reset_pending_trigger()
            return False
        self.reset_pending_trigger()
        return False


class LeftClickOnlyMenu(QMenu):
    def __init__(
        self,
        title: str = "",
        parent: QObject | None = None,
        *,
        action_guard: TrayActionGuard | None = None,
    ) -> None:
        super().__init__(title, parent)
        self._action_guard = action_guard or TrayActionGuard()
        self.aboutToShow.connect(self._action_guard.reset_pending_trigger)
        self.aboutToHide.connect(self._schedule_pending_trigger_reset)

    def _schedule_pending_trigger_reset(self) -> None:
        QTimer.singleShot(0, self._action_guard.reset_pending_trigger)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._action_guard.note_press(self.actionAt(event.position().toPoint()), event.button())
        if event.button() != Qt.MouseButton.LeftButton:
            logger.debug("Ignoring non-left tray menu press: %s", event.button())
            event.ignore()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._action_guard.note_release(self.actionAt(event.position().toPoint()), event.button())
        if event.button() != Qt.MouseButton.LeftButton:
            logger.debug("Ignoring non-left tray menu release: %s", event.button())
            event.ignore()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        self._action_guard.reset_pending_trigger()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._action_guard.reset_pending_trigger()
        super().keyReleaseEvent(event)


class AppController(QObject):
    monitor_event_signal = Signal(object)

    def __init__(
        self,
        app: QApplication,
        config: AppConfig,
        resolved_paths,
        single_instance: SingleInstanceCoordinator,
    ) -> None:
        super().__init__(app)
        self._app = app
        self._config = config
        self._resolved_paths = resolved_paths
        self._single_instance = single_instance
        self._exit_reason: ExitReason | None = None

        self.monitor_event_signal.connect(self._handle_monitor_event)
        self._monitor = MonitorEngine(config, event_sink=self.monitor_event_signal.emit)
        self._window = MainWindow(config, resolved_paths)
        self._window.config_changed.connect(self.apply_config)
        self._window.manual_launch_requested.connect(self.launch_target)
        self._window.test_requested.connect(self.test_target)
        self._window.system_settings_requested.connect(self.open_system_settings_dialog)
        self._window.user_exit_requested.connect(self.exit_user)
        self._window.installEventFilter(self)
        self._single_instance.show_requested.connect(self._handle_show_request)
        self._tray_action_guard = TrayActionGuard()
        self._tray_menu_visible = False

        self._tray = QSystemTrayIcon(self._build_icon(), self)
        self._tray.setToolTip("WatchDog")
        self._tray.activated.connect(self._handle_tray_activated)

        self._tray_menu = LeftClickOnlyMenu(action_guard=self._tray_action_guard)
        self._tray_menu.aboutToShow.connect(self._handle_tray_menu_about_to_show)
        self._tray_menu.aboutToHide.connect(self._handle_tray_menu_about_to_hide)
        self._toggle_action = self._add_guarded_tray_action(
            self._tray_menu,
            "啟動",
            self.toggle_monitoring,
        )
        system_menu = LeftClickOnlyMenu(
            "系統設計",
            self._tray_menu,
            action_guard=self._tray_action_guard,
        )
        self._tray_menu.addMenu(system_menu)
        self._add_guarded_tray_action(system_menu, "參數設定", self.show_settings_window)
        self._add_guarded_tray_action(system_menu, "系統設定", self.open_system_settings_dialog)
        self._add_guarded_tray_action(self._tray_menu, "結束", self.exit_user)
        self._tray.show()

        self._app.commitDataRequest.connect(self._handle_session_end)
        self._app.aboutToQuit.connect(self._about_to_quit)

        if self._config.start_monitoring_on_login:
            self.start_monitoring()

    @property
    def exit_reason(self) -> ExitReason | None:
        return self._exit_reason

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched is self._window and event.type() == QEvent.Type.Close:
            self._window.hide()
            event.ignore()
            return True
        return super().eventFilter(watched, event)

    def show_settings_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _handle_show_request(self) -> None:
        self._reload_config_from_disk()
        self.show_settings_window()

    def toggle_monitoring(self) -> None:
        if self._monitor.is_running():
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self) -> None:
        self._monitor.start()
        self._window.set_monitoring_running(True)
        self._toggle_action.setText("關閉")

    def stop_monitoring(self) -> None:
        self._monitor.stop()
        self._window.set_monitoring_running(False)
        self._toggle_action.setText("啟動")

    def launch_target(self, target_id: str) -> None:
        try:
            self._monitor.manual_launch(target_id)
        except Exception as exc:
            QMessageBox.warning(self._window, "啟動失敗", str(exc))

    def test_target(self, target_id: str) -> None:
        try:
            result = self._monitor.test_target(target_id)
            detail_lines = []
            for item in result.check_results:
                detail = item.details or ("檢查通過" if item.healthy else "檢查未通過")
                detail_lines.append(f"{item.summary}：{detail}")
            QMessageBox.information(
                self._window,
                "檢查結果",
                f"{result.summary}\n" + "\n".join(detail_lines),
            )
        except Exception as exc:
            QMessageBox.warning(self._window, "檢查失敗", str(exc))

    def apply_config(self, config: AppConfig) -> None:
        self._config = config.validate()
        save_config(self._config, self._resolved_paths.config_path)
        self._monitor.set_config(self._config)
        self._window.set_config(self._config)

    def _reload_config_from_disk(self) -> None:
        if self._window.has_unsaved_changes():
            return
        config = load_config(self._resolved_paths.config_path)
        self._config = config.validate()
        self._monitor.set_config(self._config)
        self._window.set_config(self._config)

    def open_system_settings_dialog(self) -> None:
        dialog = SystemSettingsDialog(
            self._config.storage,
            self._resolved_paths,
            self._config.auto_start_scope,
            self._config.start_monitoring_on_login,
            self._window,
        )
        if not dialog.exec():
            return

        storage, scope, start_on_login = dialog.values()
        try:
            status = apply_autostart(scope)
        except Exception as exc:
            QMessageBox.warning(self._window, "系統設定儲存失敗", f"自動啟動設定失敗：{exc}")
            return

        self._config.storage = storage
        self._config.auto_start_scope = scope
        self._config.auto_start_provider = status.provider or AutoStartProvider.REGISTRY_RUN
        self._config.start_monitoring_on_login = start_on_login
        resolved = update_bootstrap_for_storage(storage)
        self._resolved_paths = resolved
        configure_logging(resolved.log_directory)
        self._window.set_resolved_paths(resolved)
        self.apply_config(self._config)
        if resolved.config_fallback_used or resolved.log_fallback_used:
            QMessageBox.warning(
                self._window,
                "儲存位置已回退",
                "指定的 .exe 所在路徑不可寫入，已自動回退到 AppData/LocalAppData。",
            )

    def exit_user(self) -> None:
        logger.info("User requested application exit from tray menu.")
        if self._window.has_unsaved_changes():
            decision = QMessageBox.question(
                self._window,
                "尚未儲存的設定",
                "目前有尚未儲存的設定，是否要先儲存再結束？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            logger.info("Exit confirmation with unsaved changes. decision=%s", decision)
            if decision == QMessageBox.StandardButton.Cancel:
                return
            if decision == QMessageBox.StandardButton.Yes and not self._window.save_pending_changes():
                return
        self._request_exit(ExitReason.USER_EXIT)

    def _request_exit(self, reason: ExitReason) -> None:
        if self._exit_reason is not None:
            return
        logger.info("Application exit requested. reason=%s", reason.name)
        self._exit_reason = reason
        self._monitor.shutdown()
        self._tray.hide()
        self._single_instance.close()
        self._app.exit(reason.value)

    def _handle_session_end(self, *args) -> None:
        self._request_exit(ExitReason.OS_SESSION_END)

    def _about_to_quit(self) -> None:
        if self._exit_reason is None:
            self._exit_reason = ExitReason.UNEXPECTED_TERMINATION

    def _handle_monitor_event(self, event) -> None:
        self._window.apply_monitor_event(event)

    def _add_guarded_tray_action(
        self,
        menu: QMenu,
        text: str,
        callback: Callable[[], None],
    ) -> QAction:
        action = menu.addAction(text)
        action.triggered.connect(
            lambda _checked=False, tray_action=action, handler=callback: self._invoke_guarded_tray_action(
                tray_action,
                handler,
            )
        )
        return action

    def _invoke_guarded_tray_action(
        self,
        action: QAction,
        callback: Callable[[], None],
    ) -> None:
        if not self._tray_action_guard.allow_trigger(action):
            logger.info("Blocked tray menu action without a valid left-click trigger. action=%s", action.text())
            return
        logger.info("Executing tray menu action from validated left-click. action=%s", action.text())
        callback()

    def _handle_tray_menu_about_to_show(self) -> None:
        self._tray_menu_visible = True
        self._tray_action_guard.reset_pending_trigger()

    def _handle_tray_menu_about_to_hide(self) -> None:
        self._tray_menu_visible = False

    def _show_tray_menu(self) -> None:
        if self._tray_menu_visible:
            logger.debug("Ignoring tray menu popup request while menu is already visible.")
            return
        self._tray_action_guard.mark_popup_started()
        self._tray_menu_visible = True
        position = QCursor.pos() + QPoint(0, 8)
        logger.debug("Showing tray menu at %s", position)
        self._tray_menu.popup(position)

    def _handle_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        logger.debug("Tray activated. reason=%s", reason)
        if reason == QSystemTrayIcon.ActivationReason.Context:
            self._show_tray_menu()
            return
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_settings_window()

    def _build_icon(self) -> QIcon:
        icon = self._app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        if not icon.isNull():
            return icon
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.darkGreen)
        return QIcon(pixmap)


def _install_exception_hooks() -> tuple[object, object]:
    old_sys = sys.excepthook
    old_thread = threading.excepthook

    def _sys_hook(exc_type, exc_value, exc_traceback) -> None:
        logger.critical(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
        )
        logging.shutdown()
        os._exit(ExitReason.CRITICAL_EXCEPTION.value)

    def _thread_hook(args) -> None:
        logger.critical(
            "Unhandled thread exception:\n%s",
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )
        logging.shutdown()
        os._exit(ExitReason.CRITICAL_EXCEPTION.value)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    return old_sys, old_thread


def run_child_app() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    single_instance = SingleInstanceCoordinator()
    if not single_instance.acquire():
        return ExitReason.SECONDARY_INSTANCE.value

    bootstrap = load_bootstrap_state()
    if bootstrap.storage is None or not bootstrap.first_run_completed:
        dialog = StorageSetupDialog()
        if not dialog.exec():
            single_instance.close()
            return ExitReason.USER_EXIT.value
        selected_storage = dialog.storage_preferences()
        resolved_paths = update_bootstrap_for_storage(selected_storage)
    else:
        selected_storage = bootstrap.storage
        resolved_paths = resolve_paths(bootstrap.storage)

    configure_logging(resolved_paths.log_directory)
    old_sys_hook, old_thread_hook = _install_exception_hooks()
    try:
        config = load_config(resolved_paths.config_path)
        config.storage = selected_storage or config.storage
        save_config(config, resolved_paths.config_path)
        controller = AppController(app, config, resolved_paths, single_instance)
        controller.show_settings_window()
        app_exit_code = app.exec()
        if controller.exit_reason is not None:
            return controller.exit_reason.value
        mapped = ExitReason.from_exit_code(app_exit_code)
        return mapped.value if mapped else ExitReason.UNEXPECTED_TERMINATION.value
    except KeyboardInterrupt:
        return ExitReason.CTRL_C_EXIT.value
    except Exception:
        logger.exception("Critical application failure.")
        return ExitReason.CRITICAL_EXCEPTION.value
    finally:
        sys.excepthook = old_sys_hook
        threading.excepthook = old_thread_hook


if __name__ == "__main__":
    from watchdog_app.main import main as package_main

    raise SystemExit(package_main())

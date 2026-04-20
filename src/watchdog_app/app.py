from __future__ import annotations

from datetime import datetime
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
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from watchdog_app.autostart import apply_autostart
    from watchdog_app.gui.dialogs import StorageSetupDialog, SystemSettingsDialog
    from watchdog_app.gui.main_window import MainWindow
    from watchdog_app.logging_utils import configure_logging
    from watchdog_app.models import AppConfig, AutoStartProvider, BootstrapState, ExitReason
    from watchdog_app.monitor import MonitorEngine
    from watchdog_app.runtime import bootstrap_path, not_ready_icon_path, ready_icon_path
    from watchdog_app.single_instance import SingleInstanceCoordinator
    from watchdog_app.storage import (
        effective_storage_preferences,
        log_output_root,
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
    from .models import AppConfig, AutoStartProvider, BootstrapState, ExitReason
    from .monitor import MonitorEngine
    from .runtime import bootstrap_path, not_ready_icon_path, ready_icon_path
    from .single_instance import SingleInstanceCoordinator
    from .storage import (
        effective_storage_preferences,
        log_output_root,
        load_bootstrap_state,
        load_config,
        resolve_paths,
        save_config,
        update_bootstrap_for_storage,
    )


logger = logging.getLogger(__name__)


def _move_aside_invalid_file(path: Path, marker: str) -> Path | None:
    if not path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(100):
        suffix = f".{marker}.{timestamp}" if index == 0 else f".{marker}.{timestamp}.{index}"
        candidate = path.with_name(f"{path.stem}{suffix}{path.suffix}")
        if candidate.exists():
            continue
        try:
            path.replace(candidate)
        except OSError:
            return None
        return candidate
    return None


def _load_bootstrap_state_with_recovery() -> tuple[BootstrapState, str | None]:
    try:
        return load_bootstrap_state(), None
    except Exception as exc:
        source_path = bootstrap_path()
        backup_path = _move_aside_invalid_file(source_path, "invalid")
        logger.warning("Bootstrap state was invalid and will be reset. path=%s error=%s", source_path, exc)
        lines = [
            f"啟動設定檔讀取失敗：{exc}",
            "WatchDog 將改用首次啟動流程繼續執行。",
        ]
        if backup_path is not None:
            lines.append(f"原始檔已備份到：{backup_path}")
        elif source_path.exists():
            lines.append("原始檔無法自動備份，請手動檢查該檔案。")
        return BootstrapState(), "\n".join(lines)


def _load_config_with_recovery(path: Path) -> tuple[AppConfig, str | None]:
    try:
        return load_config(path), None
    except Exception as exc:
        backup_path = _move_aside_invalid_file(path, "invalid")
        logger.warning("Config file was invalid and will be reset. path=%s error=%s", path, exc)
        lines = [
            f"設定檔讀取失敗：{exc}",
            "WatchDog 將改用預設設定繼續執行。",
        ]
        if backup_path is not None:
            lines.append(f"原始檔已備份到：{backup_path}")
        elif path.exists():
            lines.append("原始檔無法自動備份，請手動檢查該檔案。")
        return AppConfig.default(), "\n".join(lines)


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

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._action_guard.reset_pending_trigger()
        super().leaveEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        self._action_guard.reset_pending_trigger()
        super().focusOutEvent(event)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._schedule_pending_trigger_reset()
        super().hideEvent(event)


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
        self._config = self._clone_config(config)
        self._resolved_paths = resolved_paths
        self._single_instance = single_instance
        self._exit_reason: ExitReason | None = None
        self._fallback_icon = self._build_fallback_icon()
        self._ready_icon, self._not_ready_icon = self._load_status_icons()

        self.monitor_event_signal.connect(self._handle_monitor_event)
        self._monitor = MonitorEngine(
            self._clone_config(self._config),
            event_sink=self.monitor_event_signal.emit,
        )
        self._window = MainWindow(self._clone_config(self._config), resolved_paths)
        self._window.config_changed.connect(self.apply_config)
        self._window.manual_launch_requested.connect(self.launch_target)
        self._window.test_requested.connect(self.test_target)
        self._window.system_settings_requested.connect(self.open_system_settings_dialog)
        self._window.user_exit_requested.connect(self.exit_user)
        self._window.installEventFilter(self)
        self._single_instance.show_requested.connect(self._handle_show_request)
        self._tray_action_guard = TrayActionGuard()
        self._tray_menu_visible = False

        initial_icon = self._status_icon(False)
        self._tray = QSystemTrayIcon(initial_icon, self)
        self._tray.setToolTip("WatchDog")
        self._tray.activated.connect(self._handle_tray_activated)
        self._apply_status_icon(False)

        self._tray_menu = LeftClickOnlyMenu(action_guard=self._tray_action_guard)
        self._tray_menu.aboutToShow.connect(self._handle_tray_menu_about_to_show)
        self._tray_menu.aboutToHide.connect(self._handle_tray_menu_about_to_hide)
        self._toggle_action = self._add_guarded_tray_action(
            self._tray_menu,
            "啟動偵測",
            self.toggle_monitoring,
        )
        system_menu = LeftClickOnlyMenu(
            "系統設定",
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

    @staticmethod
    def _clone_config(config: AppConfig) -> AppConfig:
        return AppConfig.from_dict(config.validate().to_dict())

    def _apply_live_config(self, config: AppConfig) -> None:
        canonical = self._clone_config(config)
        self._config = canonical
        self._monitor.set_config(self._clone_config(canonical))
        self._window.set_config(self._clone_config(canonical))

    def toggle_monitoring(self) -> None:
        if self._monitor.is_running():
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self) -> None:
        self._monitor.start()
        logger.info("Monitoring started.")
        self._window.set_monitoring_running(True)
        self._toggle_action.setText("關閉偵測")
        self._apply_status_icon(True)

    def stop_monitoring(self) -> None:
        self._monitor.stop()
        logger.info("Monitoring stopped.")
        self._window.set_monitoring_running(False)
        self._toggle_action.setText("啟動偵測")
        self._apply_status_icon(False)

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
        previous = self._clone_config(self._config)
        try:
            candidate = self._clone_config(config)
            save_config(candidate, self._resolved_paths.config_path)
        except Exception as exc:
            logger.exception("Failed to persist config to %s", self._resolved_paths.config_path)
            self._apply_live_config(previous)
            QMessageBox.warning(self._window, "設定儲存失敗", f"無法儲存設定：{exc}")
            return
        self._apply_live_config(candidate)

    def _reload_config_from_disk(self) -> None:
        if self._window.has_unsaved_changes():
            return
        config, warning = _load_config_with_recovery(self._resolved_paths.config_path)
        config.storage = self._config.storage
        if warning:
            try:
                save_config(config, self._resolved_paths.config_path)
            except Exception as exc:
                logger.exception("Failed to rewrite recovered config to %s", self._resolved_paths.config_path)
                warning = f"{warning}\n另外，無法將修復後的設定寫回磁碟：{exc}"
        self._apply_live_config(config)
        if warning:
            QMessageBox.warning(self._window, "設定檔已重設", warning)

    def _rollback_system_settings(
        self,
        previous_config: AppConfig,
        previous_resolved_paths,
        transient_config_path: Path | None = None,
    ) -> list[str]:
        rollback_errors: list[str] = []

        try:
            apply_autostart(previous_config.auto_start_scope)
        except Exception as exc:
            logger.exception("Failed to roll back autostart settings.")
            rollback_errors.append(f"自動啟動回復失敗：{exc}")

        try:
            save_config(previous_config, previous_resolved_paths.config_path)
        except Exception as exc:
            logger.exception("Failed to roll back config file at %s", previous_resolved_paths.config_path)
            rollback_errors.append(f"設定檔回復失敗：{exc}")

        if transient_config_path and transient_config_path != previous_resolved_paths.config_path:
            try:
                transient_config_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.exception("Failed to remove staged config file at %s", transient_config_path)
                rollback_errors.append(f"暫存設定清理失敗：{exc}")

        try:
            restored_paths = update_bootstrap_for_storage(previous_config.storage)
        except Exception as exc:
            logger.exception("Failed to roll back bootstrap storage settings.")
            restored_paths = previous_resolved_paths
            rollback_errors.append(f"啟動設定回復失敗：{exc}")

        try:
            configure_logging(restored_paths.log_directory)
        except Exception as exc:
            logger.exception("Failed to roll back logging configuration.")
            rollback_errors.append(f"日誌設定回復失敗：{exc}")

        self._resolved_paths = restored_paths
        self._window.set_resolved_paths(restored_paths)
        self._apply_live_config(previous_config)
        return rollback_errors

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

        requested_storage, scope, start_on_login = dialog.values()
        previous_config = self._clone_config(self._config)
        previous_resolved_paths = self._resolved_paths
        planned_paths = resolve_paths(requested_storage)
        effective_storage = effective_storage_preferences(planned_paths)
        candidate = self._clone_config(self._config)
        candidate.storage = effective_storage
        candidate.auto_start_scope = scope
        candidate.start_monitoring_on_login = start_on_login
        try:
            status = apply_autostart(scope)
            candidate.auto_start_provider = status.provider or AutoStartProvider.NONE
            save_config(candidate, planned_paths.config_path)
            resolved = update_bootstrap_for_storage(effective_storage)
            active_log_path = configure_logging(resolved.log_directory)
        except Exception as exc:
            if "需要系統管理員權限" in str(exc):
                logger.warning("Failed to persist system settings: %s", exc)
            else:
                logger.exception("Failed to persist system settings.")
            rollback_errors = self._rollback_system_settings(
                previous_config,
                previous_resolved_paths,
                planned_paths.config_path,
            )
            message = f"系統設定儲存失敗：{exc}"
            if rollback_errors:
                message = f"{message}\n\n已嘗試回復原設定，但以下項目回復失敗：\n" + "\n".join(
                    rollback_errors
                )
            QMessageBox.warning(self._window, "系統設定儲存失敗", message)
            return

        self._resolved_paths = resolved
        self._window.set_resolved_paths(resolved)
        self._apply_live_config(candidate)
        logger.info(
            "Persisted system settings. config_path=%s log_root=%s active_log=%s auto_start_scope=%s",
            resolved.config_path,
            log_output_root(resolved.log_directory),
            active_log_path,
            scope.value,
        )
        if effective_storage != requested_storage:
            logger.warning(
                "Requested storage was not writable; falling back to config=%s log=%s.",
                candidate.storage.config_mode.value,
                candidate.storage.log_mode.value,
            )
            QMessageBox.warning(
                self._window,
                "儲存位置已回退",
                "指定的儲存位置不可寫入，已自動回退到預設位置。",
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
        logger.info("OS session end requested application shutdown.")
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

    def _load_status_icons(self) -> tuple[QIcon, QIcon]:
        return (
            self._load_icon(ready_icon_path()),
            self._load_icon(not_ready_icon_path()),
        )

    def _load_icon(self, path: Path) -> QIcon:
        icon = QIcon(str(path))
        if icon.isNull():
            logger.warning("Failed to load icon from %s", path)
        return icon

    def _status_icon(self, monitoring_running: bool) -> QIcon:
        icon = self._ready_icon if monitoring_running else self._not_ready_icon
        if icon.isNull():
            return self._fallback_icon
        return icon

    def _apply_status_icon(self, monitoring_running: bool) -> None:
        icon = self._status_icon(monitoring_running)
        self._tray.setIcon(icon)
        self._app.setWindowIcon(icon)
        self._window.setWindowIcon(icon)

    def _build_fallback_icon(self) -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.darkGreen)
        return QIcon(pixmap)


def _install_exception_hooks() -> tuple[object, object]:
    old_sys = sys.excepthook
    old_thread = threading.excepthook

    def _sys_hook(exc_type, exc_value, exc_traceback) -> None:
        logger.error(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
        )
        logging.shutdown()
        os._exit(ExitReason.CRITICAL_EXCEPTION.value)

    def _thread_hook(args) -> None:
        logger.error(
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

    try:
        bootstrap, bootstrap_warning = _load_bootstrap_state_with_recovery()
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

        active_log_path = configure_logging(resolved_paths.log_directory)
        old_sys_hook, old_thread_hook = _install_exception_hooks()
        logger.info("Logging configured. active_log=%s", active_log_path)
    except KeyboardInterrupt:
        return ExitReason.CTRL_C_EXIT.value
    except Exception:
        logger.exception("Critical application failure during startup bootstrap.")
        single_instance.close()
        return ExitReason.CRITICAL_EXCEPTION.value

    try:
        config, config_warning = _load_config_with_recovery(resolved_paths.config_path)
        config.storage = selected_storage or config.storage
        save_config(config, resolved_paths.config_path)
        controller = AppController(app, config, resolved_paths, single_instance)
        controller.show_settings_window()
        if bootstrap_warning:
            QMessageBox.warning(controller._window, "啟動設定已重設", bootstrap_warning)
        if config_warning:
            QMessageBox.warning(controller._window, "設定檔已重設", config_warning)
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

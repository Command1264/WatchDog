from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView

from watchdog_app.gui.dialogs import CheckEditorDialog, SystemSettingsDialog
from watchdog_app.gui.main_window import CenteredCheckboxCell, MainWindow
from watchdog_app.models import (
    AppConfig,
    AutoStartScope,
    CheckSpec,
    CheckType,
    LaunchKind,
    LaunchSpec,
    ResolvedPaths,
    StorageMode,
    StoragePreferences,
    TargetConfig,
)
from watchdog_app.monitor import MonitorEvent, TargetRuntimeState, TargetStatus


def test_main_window_uses_status_bar_without_window_toggle_button(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    assert window.statusBar().currentMessage() == "監測已停止"
    assert not hasattr(window, "_toggle_button")
    window.set_monitoring_running(True)
    assert window.statusBar().currentMessage() == "監測執行中"

    window.set_monitoring_running(False)
    assert window.statusBar().currentMessage() == "監測已停止"
    assert not hasattr(window, "_save_button")


def test_main_window_exposes_system_settings_via_more_settings_toolbar(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    assert window._more_settings_button.text() == "更多設定"
    assert [action.text() for action in window._more_settings_menu.actions()] == ["系統設定"]

    with qtbot.waitSignal(window.system_settings_requested):
        window._system_settings_action.trigger()


def test_system_settings_dialog_inlines_storage_and_autostart_controls(qtbot, tmp_path) -> None:
    dialog = SystemSettingsDialog(
        StoragePreferences(
            config_mode=StorageMode.APPDATA,
            log_mode=StorageMode.LOCALAPPDATA,
        ),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
        AutoStartScope.CURRENT_USER,
        True,
    )
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "系統設定"
    assert dialog._scope_combo.currentData() == AutoStartScope.CURRENT_USER
    assert dialog._start_checkbox.isChecked() is True
    assert dialog._config_path_label.text() == str(tmp_path / "config.json")
    assert dialog._log_path_label.text() == str(tmp_path / "logs")
    assert dialog._save_button.text() == "儲存"
    assert dialog._cancel_button.text() == "取消"

    dialog._config_combo.setCurrentIndex(dialog._config_combo.findData(StorageMode.EXE))
    dialog._log_combo.setCurrentIndex(dialog._log_combo.findData(StorageMode.EXE))
    prefs, scope, start_on_login = dialog.values()

    assert prefs.config_mode == StorageMode.EXE
    assert prefs.log_mode == StorageMode.EXE
    assert scope == AutoStartScope.CURRENT_USER
    assert start_on_login is True


def test_check_editor_can_infer_process_match_from_selected_path(monkeypatch, qtbot, tmp_path) -> None:
    dialog = CheckEditorDialog(CheckSpec(type=CheckType.PROCESS_NAME))
    qtbot.addWidget(dialog)

    monkeypatch.setattr(
        "watchdog_app.gui.dialogs.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(tmp_path / "demo.bat"), ""),
    )

    class _Inference:
        process_name = "cmd.exe"
        executable_path = "C:/Windows/System32/cmd.exe"
        note = "批次檔實際會由 cmd.exe 執行，名稱檢查將比對 cmd.exe。"

    monkeypatch.setattr("watchdog_app.gui.dialogs.infer_process_match", lambda path: _Inference())

    dialog._infer_process_match_from_path()

    assert dialog._process_name.text() == "cmd.exe"
    assert dialog._exe_path.text() == "C:/Windows/System32/cmd.exe"
    assert dialog._process_inference_note.text() == _Inference.note
    assert dialog._process_inference_note.isHidden() is False


def test_left_table_supports_checkbox_and_name_editing(qtbot, tmp_path) -> None:
    config = AppConfig(
        targets=[
            TargetConfig(
                id="alpha",
                name="原始名稱",
                enabled=True,
                launch=LaunchSpec(path="C:/demo.exe"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate()
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    assert window._targets_table.horizontalHeaderItem(0).text() == "啟用"
    assert window._targets_table.horizontalHeaderItem(1).text() == "名稱"

    enabled_item = window._targets_table.item(0, 0)
    assert enabled_item.data(Qt.ItemDataRole.UserRole) is True
    enabled_widget = window._targets_table.cellWidget(0, 0)
    assert isinstance(enabled_widget, CenteredCheckboxCell)
    assert enabled_widget.is_checked() is True

    with qtbot.waitSignal(window.config_changed):
        enabled_widget.checkbox.click()
    assert config.targets[0].enabled is False
    assert enabled_item.data(Qt.ItemDataRole.UserRole) is False
    assert enabled_widget.is_checked() is False

    name_item = window._targets_table.item(0, 1)
    with qtbot.waitSignal(window.config_changed):
        name_item.setText("新的名稱")
    assert config.targets[0].name == "新的名稱"
    assert not hasattr(window, "_enabled_checkbox")


def test_left_table_checkbox_can_be_toggled_by_mouse_click(qtbot, tmp_path) -> None:
    config = AppConfig(
        targets=[
            TargetConfig(
                id="alpha",
                name="Alpha",
                enabled=False,
                launch=LaunchSpec(path="C:/demo.exe"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate()
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    item = window._targets_table.item(0, 0)
    enabled_widget = window._targets_table.cellWidget(0, 0)
    assert isinstance(enabled_widget, CenteredCheckboxCell)

    with qtbot.waitSignal(window.config_changed):
        qtbot.mouseClick(
            enabled_widget.checkbox,
            Qt.MouseButton.LeftButton,
            pos=enabled_widget.checkbox.rect().center(),
        )

    assert item.data(Qt.ItemDataRole.UserRole) is True
    assert config.targets[0].enabled is True
    assert enabled_widget.is_checked() is True


def test_left_table_checkbox_toggles_when_clicking_enabled_cell(qtbot, tmp_path) -> None:
    config = AppConfig(
        targets=[
            TargetConfig(
                id="alpha",
                name="Alpha",
                enabled=False,
                launch=LaunchSpec(path="C:/demo.exe"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate()
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    item = window._targets_table.item(0, 0)
    enabled_widget = window._targets_table.cellWidget(0, 0)
    assert isinstance(enabled_widget, CenteredCheckboxCell)

    with qtbot.waitSignal(window.config_changed):
        qtbot.mouseClick(
            enabled_widget,
            Qt.MouseButton.LeftButton,
            pos=enabled_widget.rect().center(),
        )

    assert item.data(Qt.ItemDataRole.UserRole) is True
    assert config.targets[0].enabled is True
    assert enabled_widget.is_checked() is True


def test_existing_targets_are_loaded_into_editor_on_startup(qtbot, tmp_path) -> None:
    config = AppConfig(
        targets=[
            TargetConfig(
                id="alpha",
                name="Alpha",
                enabled=True,
                launch=LaunchSpec(path="C:/alpha.exe", working_dir="C:/"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate()
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    assert window._selected_index() == 0
    assert window._current_target_id == "alpha"
    assert window._name_edit.text() == "Alpha"
    assert window._path_edit.text() == "C:/alpha.exe"


def test_main_window_detects_unsaved_target_changes_and_can_save_them(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    assert window.has_unsaved_changes() is False

    window._name_edit.setText("New Target")
    window._path_edit.setText("C:/demo.exe")

    assert window.has_unsaved_changes() is True

    with qtbot.waitSignal(window.config_changed):
        assert window.save_pending_changes() is True

    assert len(window._config.targets) == 1
    assert window._config.targets[0].name == "New Target"
    assert window.has_unsaved_changes() is False


def test_status_and_timestamps_are_localized_and_readonly(qtbot, tmp_path) -> None:
    config = AppConfig(
        targets=[
            TargetConfig(
                id="alpha",
                name="Alpha",
                enabled=True,
                launch=LaunchSpec(path="C:/demo.exe"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate()
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    stamp = datetime(2026, 4, 15, 16, 30, 5, 789000).timestamp()
    window.apply_monitor_event(
        MonitorEvent(
            target_id="alpha",
            status=TargetStatus.ERROR,
            message="error",
            snapshot={
                "alpha": TargetRuntimeState(
                    status=TargetStatus.ERROR,
                    last_check_at=stamp,
                    last_restart_at=stamp,
                    last_error="這是摘要",
                    last_error_detail="這是非常詳細的錯誤內容",
                )
            },
        )
    )

    assert window._targets_table.item(0, 2).text() == "錯誤"
    assert window._targets_table.item(0, 3).text() == "2026/04/15 16:30:05.789"
    assert window._targets_table.item(0, 4).text() == "2026/04/15 16:30:05.789"
    assert not (window._targets_table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable)
    assert not (window._targets_table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable)
    assert not (window._targets_table.item(0, 4).flags() & Qt.ItemFlag.ItemIsEditable)


def test_non_name_columns_are_centered_and_columns_resize_to_contents(qtbot, tmp_path) -> None:
    config = AppConfig(
        targets=[
            TargetConfig(
                id="alpha",
                name="Alpha",
                enabled=True,
                launch=LaunchSpec(path="C:/demo.exe"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate()
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    assert (
        window._targets_table.horizontalHeader().sectionResizeMode(0)
        == QHeaderView.ResizeMode.ResizeToContents
    )
    assert (
        window._targets_table.item(0, 0).textAlignment() & int(Qt.AlignmentFlag.AlignHCenter)
    )
    assert (
        window._targets_table.item(0, 2).textAlignment() & int(Qt.AlignmentFlag.AlignHCenter)
    )
    assert not (
        window._targets_table.item(0, 1).textAlignment() & int(Qt.AlignmentFlag.AlignHCenter)
    )
    enabled_widget = window._targets_table.cellWidget(0, 0)
    assert isinstance(enabled_widget, CenteredCheckboxCell)
    checkbox_center = enabled_widget.mapTo(window._targets_table.viewport(), enabled_widget.checkbox.geometry().center())
    cell_rect = window._targets_table.visualItemRect(window._targets_table.item(0, 0))
    assert abs(checkbox_center.x() - cell_rect.center().x()) <= 2
    assert abs(checkbox_center.y() - cell_rect.center().y()) <= 2
    assert (
        window._targets_table.verticalHeader().width()
        == window._targets_table.verticalHeader().defaultSectionSize()
    )
    assert (
        window._checks_table.verticalHeader().width()
        == window._checks_table.verticalHeader().defaultSectionSize()
    )


def test_enabled_cell_widget_stays_centered(qtbot, tmp_path) -> None:
    config = AppConfig(
        targets=[
            TargetConfig(
                id="alpha",
                name="Alpha",
                enabled=True,
                launch=LaunchSpec(path="C:/demo.exe"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate()
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    enabled_widget = window._targets_table.cellWidget(0, 0)
    assert isinstance(enabled_widget, CenteredCheckboxCell)
    checkbox_center = enabled_widget.mapTo(window._targets_table.viewport(), enabled_widget.checkbox.geometry().center())
    cell_rect = window._targets_table.visualItemRect(window._targets_table.item(0, 0))

    assert abs(checkbox_center.x() - cell_rect.center().x()) <= 2
    assert abs(checkbox_center.y() - cell_rect.center().y()) <= 2


def test_main_splitter_starts_near_half_and_half(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    left, right = window._main_splitter.sizes()
    assert abs(left - right) <= 2


def test_checks_table_is_readonly_and_row_select_only(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    assert (
        window._checks_table.selectionBehavior()
        == QAbstractItemView.SelectionBehavior.SelectRows
    )
    assert (
        window._checks_table.selectionMode()
        == QAbstractItemView.SelectionMode.SingleSelection
    )
    assert window._checks_table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert not (window._checks_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable)
    assert not (window._checks_table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable)


def test_edit_and_remove_check_require_selection(monkeypatch, qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    warnings: list[str] = []

    def _warning(parent, title, text):
        warnings.append(f"{title}:{text}")
        return 0

    monkeypatch.setattr("watchdog_app.gui.main_window.QMessageBox.warning", _warning)

    window._checks_table.clearSelection()
    window._edit_check()
    window._remove_check()

    assert warnings == [
        "未選擇檢查器:請先選擇要編輯的檢查器。",
        "未選擇檢查器:請先選擇要刪除的檢查器。",
    ]


def test_remove_selected_check_works_with_row_selection(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    assert len(window._current_checks) == 1
    window._checks_table.selectRow(0)
    window._remove_check()

    assert len(window._current_checks) == 0
    assert window._checks_table.rowCount() == 0


def test_tables_use_button_reorder_and_localized_labels(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    assert window._targets_table.dragDropMode() == QAbstractItemView.DragDropMode.NoDragDrop
    assert window._targets_table.dragEnabled() is False
    assert window._checks_table.dragDropMode() == QAbstractItemView.DragDropMode.NoDragDrop
    assert window._checks_table.dragEnabled() is False
    assert window._targets_table.viewportMargins().right() > 0
    window.show()
    qtbot.waitExposed(window)
    target_controls = window._targets_table._control_host.geometry()
    check_controls = window._checks_table._control_host.geometry()
    assert target_controls.right() <= window._targets_table.rect().right()
    assert check_controls.right() <= window._checks_table.rect().right()
    assert target_controls.left() >= window._targets_table.rect().center().x()
    assert window._targets_table.up_button.arrowType() == Qt.ArrowType.UpArrow
    assert window._targets_table.down_button.arrowType() == Qt.ArrowType.DownArrow
    assert window._checks_table.up_button.arrowType() == Qt.ArrowType.UpArrow
    assert window._checks_table.down_button.arrowType() == Qt.ArrowType.DownArrow
    target_up_center = window._targets_table._control_host.mapTo(
        window._targets_table,
        window._targets_table.up_button.geometry().center(),
    ).y()
    target_down_center = window._targets_table._control_host.mapTo(
        window._targets_table,
        window._targets_table.down_button.geometry().center(),
    ).y()
    target_button_center = (target_up_center + target_down_center) / 2
    assert abs(target_button_center - window._targets_table.rect().center().y()) <= 4
    assert window._kind_combo.itemText(0) == "自動判斷"
    assert window._kind_combo.itemText(1) == "執行檔"
    assert window._kind_combo.itemText(4) == "Python"
    assert window._logic_combo.itemText(0) == "全部通過"
    assert window._logic_combo.itemText(1) == "任一通過"
    assert window._checks_table.item(0, 0).text() == "PID 檢查"


def test_move_target_updates_config_order(qtbot, tmp_path) -> None:
    config = AppConfig(
        targets=[
            TargetConfig(
                id="alpha",
                name="Alpha",
                enabled=True,
                launch=LaunchSpec(path="C:/alpha.exe"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate(),
            TargetConfig(
                id="beta",
                name="Beta",
                enabled=True,
                launch=LaunchSpec(path="C:/beta.exe"),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate(),
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)
    window._targets_table.selectRow(0)

    with qtbot.waitSignal(window.config_changed):
        window._targets_table.down_button.click()

    assert [target.id for target in config.targets] == ["beta", "alpha"]
    assert window._selected_index() == 1


def test_move_check_updates_editor_order(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    window._current_checks = [
        CheckSpec(type=CheckType.RUNTIME_PID),
        CheckSpec(type=CheckType.TCP_PORT, host="127.0.0.1", port=1234),
    ]
    window._refresh_checks_table()
    window._checks_table.selectRow(0)

    window._checks_table.down_button.click()

    assert [check.type for check in window._current_checks] == [
        CheckType.TCP_PORT,
        CheckType.RUNTIME_PID,
    ]
    assert window._selected_check_index() == 1


def test_arrow_buttons_stay_centered_when_table_is_heavily_compressed(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)
    window.resize(900, 320)
    window.show()
    qtbot.waitExposed(window)

    for table in (window._targets_table, window._checks_table):
        up_center = table._control_host.mapTo(table, table.up_button.geometry().center()).y()
        down_center = table._control_host.mapTo(table, table.down_button.geometry().center()).y()
        button_center = (up_center + down_center) / 2
        assert abs(button_center - table.rect().center().y()) <= 4


def test_add_target_from_file_prefills_target_and_selects_it(monkeypatch, qtbot, tmp_path) -> None:
    script = tmp_path / "dongle_reader.ps1"
    script.write_text("Write-Host 'ok'", encoding="utf-8")

    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    monkeypatch.setattr(
        "watchdog_app.gui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(script), ""),
    )

    with qtbot.waitSignal(window.config_changed):
        window._add_target_from_file()

    assert len(window._config.targets) == 1
    target = window._config.targets[0]
    assert target.name == "dongle_reader"
    assert target.launch.path == str(script)
    assert target.launch.working_dir == str(script.parent)
    assert target.launch.kind == LaunchKind.POWERSHELL
    assert target.enabled is False
    assert target.checks[0].type == CheckType.RUNTIME_PID
    assert window._selected_index() == 0
    assert window._name_edit.text() == "dongle_reader"
    assert window._path_edit.text() == str(script)
    enabled_widget = window._targets_table.cellWidget(0, 0)
    assert isinstance(enabled_widget, CenteredCheckboxCell)
    assert enabled_widget.is_checked() is False


def test_save_new_target_defaults_to_disabled(qtbot, tmp_path) -> None:
    window = MainWindow(
        AppConfig.default(),
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    window._name_edit.setText("New Target")
    window._path_edit.setText("C:/demo.exe")

    with qtbot.waitSignal(window.config_changed):
        window._save_target()

    assert len(window._config.targets) == 1
    assert window._config.targets[0].enabled is False
    enabled_widget = window._targets_table.cellWidget(0, 0)
    assert isinstance(enabled_widget, CenteredCheckboxCell)
    assert enabled_widget.is_checked() is False


def test_add_target_from_file_reuses_existing_target(monkeypatch, qtbot, tmp_path) -> None:
    exe_path = tmp_path / "demo.exe"
    exe_path.write_text("", encoding="utf-8")
    config = AppConfig(
        targets=[
            TargetConfig(
                id="demo",
                name="Demo",
                enabled=True,
                launch=LaunchSpec(path=str(exe_path), kind=LaunchKind.EXE, working_dir=str(tmp_path)),
                checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
            ).validate()
        ]
    ).validate()
    window = MainWindow(
        config,
        ResolvedPaths(
            bootstrap_path=tmp_path / "bootstrap.json",
            config_path=tmp_path / "config.json",
            log_directory=tmp_path / "logs",
        ),
    )
    qtbot.addWidget(window)

    monkeypatch.setattr(
        "watchdog_app.gui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(exe_path), ""),
    )
    messages: list[str] = []

    def _info(parent, title, text):
        messages.append(f"{title}:{text}")
        return 0

    monkeypatch.setattr("watchdog_app.gui.main_window.QMessageBox.information", _info)

    window._add_target_from_file()

    assert len(window._config.targets) == 1
    assert messages == ["已存在的應用程式:這個應用程式已在清單中，已切換到既有設定。"]
    assert window._selected_index() == 0

from __future__ import annotations

import ctypes
from pathlib import Path
import shlex
import subprocess
from datetime import datetime
from typing import TypeVar

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..models import (
    AppConfig,
    CheckLogic,
    CheckSpec,
    CheckType,
    ConfigValidationError,
    LaunchKind,
    LaunchSpec,
    ResolvedPaths,
    TargetConfig,
    normalize_path_text,
)
from ..monitor import MonitorEvent, TargetRuntimeState
from ..monitor import TargetStatus
from ..launchers import detect_launch_kind, infer_process_match
from .dialogs import CheckEditorDialog, ReadOnlyTextDialog


COLUMN_ENABLED = 0
COLUMN_NAME = 1
COLUMN_STATUS = 2
COLUMN_LAST_CHECK = 3
COLUMN_LAST_RESTART = 4
COLUMN_LAST_ERROR = 5
T = TypeVar("T")

STATUS_TEXT = {
    TargetStatus.DISABLED: "停用",
    TargetStatus.STOPPED: "已停止",
    TargetStatus.SCHEDULED: "已排程",
    TargetStatus.LAUNCHING: "啟動中",
    TargetStatus.RUNNING: "執行中",
    TargetStatus.UNHEALTHY: "異常",
    TargetStatus.ERROR: "錯誤",
}

LAUNCH_KIND_TEXT = {
    LaunchKind.AUTO: "自動判斷",
    LaunchKind.EXE: "執行檔",
    LaunchKind.CMD: "命令檔",
    LaunchKind.POWERSHELL: "PowerShell",
    LaunchKind.PYTHON: "Python",
}

CHECK_LOGIC_TEXT = {
    CheckLogic.ALL: "全部通過",
    CheckLogic.ANY: "任一通過",
}

CHECK_TYPE_TEXT = {
    CheckType.RUNTIME_PID: "PID 檢查",
    CheckType.PIDFILE: "PID 檔案檢查",
    CheckType.PROCESS_NAME: "名稱檢查",
    CheckType.TCP_PORT: "TCP 連接埠檢查",
    CheckType.HTTP_ENDPOINT: "HTTP 端點檢查",
}

TABLE_CONTROL_MARGIN = 40
TABLE_CONTROL_WIDTH = 32
TABLE_CONTROL_BUTTON_SIZE = 24
TABLE_CONTROL_BUTTON_SPACING = 8


def _parse_windows_command_args(command_text: str) -> list[str]:
    stripped = command_text.strip()
    if not stripped:
        return []

    if hasattr(ctypes, "windll"):
        argc = ctypes.c_int()
        command_line = f"watchdog.exe {stripped}"
        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        argv = command_line_to_argv(command_line, ctypes.byref(argc))
        if argv:
            try:
                return [argv[index] for index in range(argc.value)][1:]
            finally:
                local_free(argv)

    return shlex.split(stripped, posix=False)


class CenteredCheckboxCell(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._checkbox = QCheckBox(self)
        self._checkbox.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._checkbox.setChecked(checked)
        self._checkbox.toggled.connect(self.toggled.emit)
        layout.addWidget(self._checkbox)

    @property
    def checkbox(self) -> QCheckBox:
        return self._checkbox

    def is_checked(self) -> bool:
        return self._checkbox.isChecked()

    def set_checked(self, checked: bool) -> None:
        self._checkbox.setChecked(checked)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._checkbox.toggle()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ArrowControlTableWidget(QTableWidget):
    move_up_requested = Signal()
    move_down_requested = Signal()

    def __init__(self, rows: int, columns: int, parent: QWidget | None = None) -> None:
        super().__init__(rows, columns, parent)
        self.setDragEnabled(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setViewportMargins(0, 0, TABLE_CONTROL_MARGIN, 0)
        self._sync_row_header_width()

        self._control_host = QWidget(self)
        self._control_host.setFixedWidth(TABLE_CONTROL_WIDTH)

        self._up_button = QToolButton(self._control_host)
        self._up_button.setArrowType(Qt.ArrowType.UpArrow)
        self._up_button.setToolTip("上移")
        self._up_button.setFixedSize(TABLE_CONTROL_BUTTON_SIZE, TABLE_CONTROL_BUTTON_SIZE)
        self._up_button.clicked.connect(self.move_up_requested.emit)

        self._down_button = QToolButton(self._control_host)
        self._down_button.setArrowType(Qt.ArrowType.DownArrow)
        self._down_button.setToolTip("下移")
        self._down_button.setFixedSize(TABLE_CONTROL_BUTTON_SIZE, TABLE_CONTROL_BUTTON_SIZE)
        self._down_button.clicked.connect(self.move_down_requested.emit)

        self._position_controls()

    @property
    def up_button(self) -> QToolButton:
        return self._up_button

    @property
    def down_button(self) -> QToolButton:
        return self._down_button

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_controls()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._sync_row_header_width()
        self._position_controls()

    def _sync_row_header_width(self) -> None:
        vertical_header = self.verticalHeader()
        width = max(vertical_header.defaultSectionSize(), vertical_header.minimumSectionSize())
        vertical_header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        vertical_header.setFixedWidth(width)

    def _position_controls(self) -> None:
        frame = self.frameWidth()
        x = max(
            frame,
            self.width() - frame - TABLE_CONTROL_MARGIN + ((TABLE_CONTROL_MARGIN - TABLE_CONTROL_WIDTH) // 2),
        )
        self._control_host.setGeometry(
            x,
            frame,
            TABLE_CONTROL_WIDTH,
            max(0, self.height() - (frame * 2)),
        )
        self._position_buttons()
        self._control_host.raise_()

    def _position_buttons(self) -> None:
        host_rect = self._control_host.rect()
        total_height = (TABLE_CONTROL_BUTTON_SIZE * 2) + TABLE_CONTROL_BUTTON_SPACING
        desired_center = self.rect().center().y() - self._control_host.y()
        top = int(round(desired_center - (total_height / 2)))
        max_top = max(0, host_rect.height() - total_height)
        top = max(0, min(top, max_top))
        x = max(0, (host_rect.width() - TABLE_CONTROL_BUTTON_SIZE) // 2)
        self._up_button.move(x, top)
        self._down_button.move(x, top + TABLE_CONTROL_BUTTON_SIZE + TABLE_CONTROL_BUTTON_SPACING)


class MainWindow(QMainWindow):
    config_changed = Signal(object)
    manual_launch_requested = Signal(str)
    test_requested = Signal(str)
    system_settings_requested = Signal()
    user_exit_requested = Signal()

    def __init__(self, config: AppConfig, resolved_paths: ResolvedPaths, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._resolved_paths = resolved_paths
        self._runtime_states: dict[str, TargetRuntimeState] = {}
        self._current_target_id: str | None = None
        self._current_target_enabled = False
        self._current_checks: list[CheckSpec] = []
        self._monitoring_running = False
        self._targets_table_updating = False
        self._splitter_initialized = False
        self._editor_loading = False

        self.setWindowTitle("WatchDog 參數設定")
        self.resize(1280, 780)
        self.statusBar().showMessage("監測已停止")

        toolbar = QToolBar("更多設定", self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        self._more_settings_button = QToolButton(self)
        self._more_settings_button.setText("更多設定")
        self._more_settings_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._more_settings_menu = QMenu(self._more_settings_button)
        self._system_settings_action = self._more_settings_menu.addAction("系統設定")
        self._system_settings_action.triggered.connect(self.system_settings_requested.emit)
        self._more_settings_button.setMenu(self._more_settings_menu)
        toolbar.addWidget(self._more_settings_button)

        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        main_layout.addWidget(self._main_splitter, 1)

        list_panel = QWidget(self)
        list_panel.setMinimumWidth(0)
        list_layout = QVBoxLayout(list_panel)
        self._targets_table = ArrowControlTableWidget(0, 6, self)
        self._targets_table.setHorizontalHeaderLabels(
            ["啟用", "名稱", "狀態", "最後檢查", "最後重啟", "最後錯誤"]
        )
        self._targets_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._targets_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._targets_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._targets_table.itemSelectionChanged.connect(self._load_selected_target)
        self._targets_table.itemChanged.connect(self._handle_table_item_changed)
        self._targets_table.cellDoubleClicked.connect(self._handle_table_double_clicked)
        self._targets_table.move_up_requested.connect(lambda: self._move_target(-1))
        self._targets_table.move_down_requested.connect(lambda: self._move_target(1))
        list_layout.addWidget(self._targets_table, 1)

        list_buttons = QHBoxLayout()
        add_button = QPushButton("加入應用程式...", self)
        add_button.clicked.connect(self._add_target_from_file)
        blank_button = QPushButton("新增空白目標", self)
        blank_button.clicked.connect(self._new_target)
        remove_button = QPushButton("刪除", self)
        remove_button.clicked.connect(self._remove_target)
        list_buttons.addWidget(add_button)
        list_buttons.addWidget(blank_button)
        list_buttons.addWidget(remove_button)
        list_layout.addLayout(list_buttons)
        self._main_splitter.addWidget(list_panel)

        editor_panel = QWidget(self)
        editor_layout = QVBoxLayout(editor_panel)
        editor_group = QGroupBox("目標設定", self)
        editor_form = QFormLayout(editor_group)

        self._name_edit = QLineEdit(self)
        self._path_edit = QLineEdit(self)
        self._args_edit = QLineEdit(self)
        self._working_dir_edit = QLineEdit(self)
        self._path_edit.editingFinished.connect(self._apply_path_based_defaults)
        self._kind_combo = QComboBox(self)
        for kind in LaunchKind:
            self._kind_combo.addItem(LAUNCH_KIND_TEXT[kind], kind)
        self._startup_spin = self._make_seconds_spin(0.05)
        self._check_spin = self._make_seconds_spin(1.0)
        self._cooldown_spin = self._make_seconds_spin(1.0)
        self._logic_combo = QComboBox(self)
        self._logic_combo.addItem(CHECK_LOGIC_TEXT[CheckLogic.ALL], CheckLogic.ALL)
        self._logic_combo.addItem(CHECK_LOGIC_TEXT[CheckLogic.ANY], CheckLogic.ANY)

        editor_form.addRow("名稱", self._name_edit)
        editor_form.addRow("執行類型", self._kind_combo)
        editor_form.addRow("檔案路徑", self._with_browse(self._path_edit, directory=False))
        editor_form.addRow("參數", self._args_edit)
        editor_form.addRow("工作目錄", self._with_browse(self._working_dir_edit, directory=True))
        editor_form.addRow("啟動延遲 (秒)", self._startup_spin)
        editor_form.addRow("巡檢間隔 (秒)", self._check_spin)
        editor_form.addRow("重啟冷卻 (秒)", self._cooldown_spin)
        editor_form.addRow("檢查邏輯", self._logic_combo)
        editor_layout.addWidget(editor_group)

        checks_group = QGroupBox("檢查器", self)
        checks_layout = QVBoxLayout(checks_group)
        self._checks_table = ArrowControlTableWidget(0, 2, self)
        self._checks_table.setHorizontalHeaderLabels(["類型", "摘要"])
        self._checks_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._checks_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._checks_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._checks_table.move_up_requested.connect(lambda: self._move_check(-1))
        self._checks_table.move_down_requested.connect(lambda: self._move_check(1))
        checks_layout.addWidget(self._checks_table, 1)
        checks_buttons = QHBoxLayout()
        add_check = QPushButton("新增檢查器", self)
        add_check.clicked.connect(self._add_check)
        edit_check = QPushButton("編輯檢查器", self)
        edit_check.clicked.connect(self._edit_check)
        remove_check = QPushButton("刪除檢查器", self)
        remove_check.clicked.connect(self._remove_check)
        checks_buttons.addWidget(add_check)
        checks_buttons.addWidget(edit_check)
        checks_buttons.addWidget(remove_check)
        checks_layout.addLayout(checks_buttons)
        editor_layout.addWidget(checks_group, 1)

        action_buttons = QHBoxLayout()
        save_target = QPushButton("儲存此目標", self)
        save_target.clicked.connect(self._save_target)
        launch_now = QPushButton("立即啟動此目標", self)
        launch_now.clicked.connect(self._launch_selected)
        test_now = QPushButton("立即測試檢查", self)
        test_now.clicked.connect(self._test_selected)
        action_buttons.addWidget(save_target)
        action_buttons.addWidget(launch_now)
        action_buttons.addWidget(test_now)
        editor_layout.addLayout(action_buttons)

        self._main_splitter.addWidget(editor_panel)
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([1, 1])

        self.set_config(config)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._splitter_initialized:
            self._sync_enabled_widget_geometry()
            return
        self._splitter_initialized = True
        total = sum(self._main_splitter.sizes())
        if total > 0:
            left = total // 2
            self._main_splitter.setSizes([left, total - left])
        self._sync_enabled_widget_geometry()

    @staticmethod
    def _make_seconds_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(0.05, 86400.0)
        spin.setValue(value)
        return spin

    def _with_browse(self, line_edit: QLineEdit, directory: bool) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton("瀏覽...", row)

        def _browse() -> None:
            if directory:
                selected = QFileDialog.getExistingDirectory(self, "選擇資料夾", line_edit.text())
            else:
                selected, _ = QFileDialog.getOpenFileName(self, "選擇檔案", line_edit.text())
            if selected:
                line_edit.setText(normalize_path_text(selected))
                if line_edit is self._path_edit:
                    self._apply_path_based_defaults()

        button.clicked.connect(_browse)
        layout.addWidget(line_edit, 1)
        layout.addWidget(button)
        return row

    def set_monitoring_running(self, running: bool) -> None:
        self._monitoring_running = running
        self.statusBar().showMessage("監測執行中" if running else "監測已停止")

    def set_resolved_paths(self, resolved_paths: ResolvedPaths) -> None:
        self._resolved_paths = resolved_paths

    def set_config(self, config: AppConfig) -> None:
        self._config = config
        self.refresh_targets_table()
        self._restore_editor_selection()

    def _restore_editor_selection(self) -> None:
        if not self._config.targets:
            self._new_target()
            return

        target_id = self._current_target_id
        if not target_id or all(target.id != target_id for target in self._config.targets):
            target_id = self._config.targets[0].id

        for row, target in enumerate(self._config.targets):
            if target.id != target_id:
                continue
            self._targets_table.selectRow(row)
            self._load_selected_target()
            return

        self._new_target()

    def has_unsaved_changes(self) -> bool:
        return self._current_editor_state() != self._baseline_editor_state()

    def save_pending_changes(self) -> bool:
        if not self.has_unsaved_changes():
            return True
        return self._save_target_internal()

    def apply_monitor_event(self, event: MonitorEvent) -> None:
        self._runtime_states = event.snapshot
        self.refresh_targets_table()
        if event.message:
            self.statusBar().showMessage(event.message)

    def refresh_targets_table(self) -> None:
        selected_id = self._current_target_id
        self._targets_table_updating = True
        self._targets_table.blockSignals(True)
        self._targets_table.setRowCount(len(self._config.targets))
        for row, target in enumerate(self._config.targets):
            state = self._runtime_states.get(target.id)

            enabled_item = QTableWidgetItem()
            enabled_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            enabled_item.setData(
                Qt.ItemDataRole.TextAlignmentRole,
                int(Qt.AlignmentFlag.AlignCenter),
            )
            enabled_item.setData(Qt.ItemDataRole.UserRole, target.enabled)
            self._targets_table.setItem(row, COLUMN_ENABLED, enabled_item)
            enabled_widget = CenteredCheckboxCell(target.enabled, self._targets_table)
            enabled_widget.checkbox.clicked.connect(
                lambda _checked=False, current_row=row: self._targets_table.selectRow(current_row)
            )
            enabled_widget.toggled.connect(
                lambda _checked, current_row=row: self._targets_table.selectRow(current_row)
            )
            enabled_widget.toggled.connect(
                lambda checked, target_id=target.id: self._handle_enabled_widget_toggled(target_id, checked)
            )
            self._targets_table.setCellWidget(row, COLUMN_ENABLED, enabled_widget)

            name_item = QTableWidgetItem(target.name)
            name_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEditable
            )
            name_item.setData(
                Qt.ItemDataRole.TextAlignmentRole,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            )
            self._targets_table.setItem(row, COLUMN_NAME, name_item)

            status_item = self._readonly_item(self._status_text(state.status if state else None))
            self._targets_table.setItem(row, COLUMN_STATUS, status_item)
            self._targets_table.setItem(
                row,
                COLUMN_LAST_CHECK,
                self._readonly_item(self._format_time(state.last_check_at if state else None)),
            )
            self._targets_table.setItem(
                row,
                COLUMN_LAST_RESTART,
                self._readonly_item(self._format_time(state.last_restart_at if state else None)),
            )
            error_detail = state.last_error_detail if state else ""
            error_item = self._readonly_item(state.last_error if state else "")
            error_item.setToolTip(error_detail)
            self._targets_table.setItem(row, COLUMN_LAST_ERROR, error_item)
            if target.id == selected_id:
                self._targets_table.selectRow(row)
        self._targets_table.blockSignals(False)
        self._targets_table_updating = False
        self._targets_table.resizeColumnsToContents()
        self._sync_enabled_column_width()
        self._sync_enabled_widget_geometry()

    def _sync_enabled_column_width(self) -> None:
        row_height = self._targets_table.verticalHeader().defaultSectionSize()
        self._targets_table.setColumnWidth(
            COLUMN_ENABLED,
            max(row_height, self._targets_table.columnWidth(COLUMN_ENABLED)),
        )

    def _sync_enabled_widget_geometry(self) -> None:
        for row in range(self._targets_table.rowCount()):
            item = self._targets_table.item(row, COLUMN_ENABLED)
            widget = self._targets_table.cellWidget(row, COLUMN_ENABLED)
            if item is None or not isinstance(widget, CenteredCheckboxCell):
                continue
            rect = self._targets_table.visualItemRect(item)
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            widget.setGeometry(rect)

    @staticmethod
    def _format_time(value: float | None) -> str:
        if value is None:
            return ""
        moment = datetime.fromtimestamp(value)
        return f"{moment:%Y/%m/%d %H:%M:%S}.{int(moment.microsecond / 1000):03d}"

    @staticmethod
    def _readonly_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        item.setData(Qt.ItemDataRole.TextAlignmentRole, int(Qt.AlignmentFlag.AlignCenter))
        return item

    @staticmethod
    def _status_text(status: TargetStatus | None) -> str:
        if status is None:
            return "已停止"
        return STATUS_TEXT.get(status, status.value)

    def _selected_index(self) -> int:
        indexes = self._targets_table.selectionModel().selectedRows()
        if not indexes:
            return -1
        return indexes[0].row()

    @staticmethod
    def _enum_value(value) -> str | None:
        if value is None:
            return None
        return getattr(value, "value", value)

    def _editor_state_from_target(self, target: TargetConfig) -> dict[str, object]:
        return {
            "name": target.name,
            "path": target.launch.path,
            "args": subprocess.list2cmdline(target.launch.args),
            "working_dir": target.launch.working_dir,
            "kind": self._enum_value(target.launch.kind),
            "startup_delay_sec": target.startup_delay_sec,
            "check_interval_sec": target.check_interval_sec,
            "restart_cooldown_sec": target.restart_cooldown_sec,
            "check_logic": self._enum_value(target.check_logic),
            "enabled": target.enabled,
            "checks": [check.to_dict() for check in target.checks],
        }

    def _baseline_editor_state(self) -> dict[str, object]:
        if self._current_target_id:
            for target in self._config.targets:
                if target.id == self._current_target_id:
                    return self._editor_state_from_target(target)
        return {
            "name": "",
            "path": "",
            "args": "",
            "working_dir": "",
            "kind": self._enum_value(LaunchKind.AUTO),
            "startup_delay_sec": 0.05,
            "check_interval_sec": 1.0,
            "restart_cooldown_sec": 1.0,
            "check_logic": self._enum_value(CheckLogic.ALL),
            "enabled": False,
            "checks": [CheckSpec(type=CheckType.RUNTIME_PID).to_dict()],
        }

    def _current_editor_state(self) -> dict[str, object]:
        return {
            "name": self._name_edit.text(),
            "path": self._path_edit.text(),
            "args": self._args_edit.text(),
            "working_dir": self._working_dir_edit.text(),
            "kind": self._enum_value(self._kind_combo.currentData()),
            "startup_delay_sec": self._startup_spin.value(),
            "check_interval_sec": self._check_spin.value(),
            "restart_cooldown_sec": self._cooldown_spin.value(),
            "check_logic": self._enum_value(self._logic_combo.currentData()),
            "enabled": self._current_target_enabled if self._current_target_id else False,
            "checks": [check.to_dict() for check in self._current_checks],
        }

    def _selected_check_index(self) -> int:
        indexes = self._checks_table.selectionModel().selectedRows()
        if not indexes:
            return -1
        return indexes[0].row()

    def _handle_enabled_widget_toggled(self, target_id: str, checked: bool) -> None:
        if self._targets_table_updating:
            return
        for row, target in enumerate(self._config.targets):
            if target.id != target_id:
                continue
            target.enabled = checked
            item = self._targets_table.item(row, COLUMN_ENABLED)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, checked)
            if self._current_target_id == target.id:
                self._current_target_enabled = checked
            self.config_changed.emit(self._config)
            return

    def _handle_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._targets_table_updating:
            return
        row = item.row()
        if row < 0 or row >= len(self._config.targets):
            return

        target = self._config.targets[row]
        if item.column() == COLUMN_NAME:
            new_name = item.text().strip()
            if not new_name:
                self._targets_table_updating = True
                self._targets_table.blockSignals(True)
                item.setText(target.name)
                self._targets_table.blockSignals(False)
                self._targets_table_updating = False
                QMessageBox.warning(self, "名稱不可為空", "名稱不可為空白。")
                return
            target.name = new_name
            if self._current_target_id == target.id:
                self._name_edit.setText(new_name)
            self.config_changed.emit(self._config)

    def _handle_table_double_clicked(self, row: int, column: int) -> None:
        if column != COLUMN_LAST_ERROR:
            return
        if row < 0 or row >= len(self._config.targets):
            return
        target = self._config.targets[row]
        state = self._runtime_states.get(target.id)
        if not state or not state.last_error_detail:
            return
        dialog = ReadOnlyTextDialog(
            f"{target.name} - 錯誤詳細資訊",
            state.last_error_detail,
            self,
        )
        dialog.exec()

    def _new_target(self) -> None:
        self._editor_loading = True
        self._targets_table.clearSelection()
        self._current_target_id = None
        self._current_target_enabled = False
        self._current_checks = [CheckSpec(type=CheckType.RUNTIME_PID)]
        self._name_edit.clear()
        self._path_edit.clear()
        self._args_edit.clear()
        self._working_dir_edit.clear()
        self._kind_combo.setCurrentIndex(0)
        self._startup_spin.setValue(0.05)
        self._check_spin.setValue(1.0)
        self._cooldown_spin.setValue(1.0)
        self._logic_combo.setCurrentIndex(0)
        self._refresh_checks_table()
        self._editor_loading = False
        self._name_edit.setFocus()

    @staticmethod
    def _normalized_path_text(path_text: str) -> str:
        try:
            return str(Path(path_text).resolve()).casefold()
        except OSError:
            return str(Path(path_text)).casefold()

    def _find_target_index_by_path(self, path_text: str) -> int:
        normalized = self._normalized_path_text(path_text)
        for index, target in enumerate(self._config.targets):
            if self._normalized_path_text(target.launch.path) == normalized:
                return index
        return -1

    def _add_target_from_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "選擇要加入的應用程式",
            self._path_edit.text() or str(Path.home()),
            "應用程式與腳本 (*.exe *.cmd *.bat *.ps1 *.py);;所有檔案 (*)",
        )
        if not selected:
            return
        selected = normalize_path_text(selected)

        existing_index = self._find_target_index_by_path(selected)
        if existing_index >= 0:
            self._targets_table.selectRow(existing_index)
            QMessageBox.information(
                self,
                "已存在的應用程式",
                "這個應用程式已在清單中，已切換到既有設定。",
            )
            return

        path = Path(selected)
        target = TargetConfig(
            id="",
            name=path.stem or path.name,
            enabled=False,
            launch=LaunchSpec(
                path=normalize_path_text(path),
                args=[],
                working_dir=normalize_path_text(path.parent),
                kind=detect_launch_kind(str(path)),
            ),
            startup_delay_sec=0.05,
            check_interval_sec=1.0,
            restart_cooldown_sec=1.0,
            check_logic=CheckLogic.ALL,
            checks=[CheckSpec(type=CheckType.RUNTIME_PID)],
        ).validate()
        self._config.targets.append(target)
        self._current_target_id = target.id
        self.refresh_targets_table()
        new_row = len(self._config.targets) - 1
        self._targets_table.selectRow(new_row)
        self._load_selected_target()
        self._name_edit.setFocus()
        self._name_edit.selectAll()
        self.config_changed.emit(self._config)

    def _load_selected_target(self) -> None:
        index = self._selected_index()
        if index < 0 or index >= len(self._config.targets):
            return
        target = self._config.targets[index]
        self._editor_loading = True
        self._current_target_id = target.id
        self._current_target_enabled = target.enabled
        self._name_edit.setText(target.name)
        self._path_edit.setText(target.launch.path)
        self._args_edit.setText(subprocess.list2cmdline(target.launch.args))
        self._working_dir_edit.setText(target.launch.working_dir)
        self._kind_combo.setCurrentIndex(max(0, self._kind_combo.findData(target.launch.kind)))
        self._startup_spin.setValue(target.startup_delay_sec)
        self._check_spin.setValue(target.check_interval_sec)
        self._cooldown_spin.setValue(target.restart_cooldown_sec)
        self._logic_combo.setCurrentIndex(max(0, self._logic_combo.findData(target.check_logic)))
        self._current_checks = [CheckSpec.from_dict(check.to_dict()) for check in target.checks]
        self._refresh_checks_table()
        self._editor_loading = False

    def _collect_target(self) -> TargetConfig:
        self._apply_path_based_defaults()
        launch_path = self._path_edit.text()
        checks = [CheckSpec.from_dict(check.to_dict()) for check in self._current_checks]
        return TargetConfig(
            id=self._current_target_id or "",
            name=self._name_edit.text(),
            enabled=self._current_target_enabled if self._current_target_id else False,
            launch=LaunchSpec(
                path=launch_path,
                args=_parse_windows_command_args(self._args_edit.text()),
                working_dir=self._working_dir_edit.text(),
                kind=self._kind_combo.currentData(),
            ),
            startup_delay_sec=self._startup_spin.value(),
            check_interval_sec=self._check_spin.value(),
            restart_cooldown_sec=self._cooldown_spin.value(),
            check_logic=self._logic_combo.currentData(),
            checks=checks or [CheckSpec(type=CheckType.RUNTIME_PID)],
        ).validate()

    def _save_target(self) -> None:
        self._save_target_internal()

    def _save_target_internal(self) -> bool:
        try:
            target = self._collect_target()
        except (ConfigValidationError, ValueError) as exc:
            QMessageBox.warning(self, "目標設定錯誤", str(exc))
            return False

        replaced = False
        for index, existing in enumerate(self._config.targets):
            if existing.id == target.id:
                self._config.targets[index] = target
                replaced = True
                break
        if not replaced:
            self._config.targets.append(target)

        self._current_target_id = target.id
        self._current_target_enabled = target.enabled
        self.refresh_targets_table()
        self.config_changed.emit(self._config)
        return True

    def _remove_target(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        self._config.targets.pop(index)
        self._new_target()
        self.refresh_targets_table()
        self.config_changed.emit(self._config)

    def _move_target(self, delta: int) -> None:
        index = self._selected_index()
        if index < 0:
            return
        new_index = index + delta
        if new_index < 0 or new_index >= len(self._config.targets):
            return
        self._reinsert_row(self._config.targets, index, new_index + (1 if delta > 0 else 0))
        self.refresh_targets_table()
        self._targets_table.selectRow(new_index)
        self.config_changed.emit(self._config)

    @staticmethod
    def _reinsert_row(items: list[T], source_row: int, insert_row: int) -> int:
        if source_row < 0 or source_row >= len(items):
            return source_row

        item = items.pop(source_row)
        if insert_row > source_row:
            insert_row -= 1
        insert_row = max(0, min(insert_row, len(items)))
        items.insert(insert_row, item)
        return insert_row

    def _refresh_checks_table(self) -> None:
        self._checks_table.setRowCount(len(self._current_checks))
        for row, check in enumerate(self._current_checks):
            self._checks_table.setItem(row, 0, self._readonly_item(CHECK_TYPE_TEXT[check.type]))
            self._checks_table.setItem(row, 1, self._readonly_item(check.summary()))
        self._checks_table.resizeColumnsToContents()

    def _add_check(self) -> None:
        dialog = CheckEditorDialog(parent=self, launch_path=self._path_edit.text())
        if dialog.exec():
            check = dialog.check_spec()
            self._apply_path_defaults_to_check(self._path_edit.text(), check)
            self._current_checks.append(check)
            self._refresh_checks_table()

    def _edit_check(self) -> None:
        index = self._selected_check_index()
        if index < 0:
            QMessageBox.warning(self, "未選擇檢查器", "請先選擇要編輯的檢查器。")
            return
        dialog = CheckEditorDialog(self._current_checks[index], self, launch_path=self._path_edit.text())
        if dialog.exec():
            check = dialog.check_spec()
            self._apply_path_defaults_to_check(self._path_edit.text(), check)
            self._current_checks[index] = check
            self._refresh_checks_table()
            self._checks_table.selectRow(index)

    def _remove_check(self) -> None:
        index = self._selected_check_index()
        if index < 0:
            QMessageBox.warning(self, "未選擇檢查器", "請先選擇要刪除的檢查器。")
            return
        self._current_checks.pop(index)
        self._refresh_checks_table()
        if self._current_checks:
            self._checks_table.selectRow(min(index, len(self._current_checks) - 1))

    def _move_check(self, delta: int) -> None:
        index = self._selected_check_index()
        if index < 0:
            return
        new_index = index + delta
        if new_index < 0 or new_index >= len(self._current_checks):
            return
        self._reinsert_row(self._current_checks, index, new_index + (1 if delta > 0 else 0))
        self._refresh_checks_table()
        self._checks_table.selectRow(new_index)

    def _launch_selected(self) -> None:
        if self._current_target_id:
            self.manual_launch_requested.emit(self._current_target_id)

    def _test_selected(self) -> None:
        if self._current_target_id:
            self.test_requested.emit(self._current_target_id)

    @staticmethod
    def _default_working_dir_for_path(path_text: str) -> str:
        stripped = path_text.strip()
        if not stripped:
            return ""
        parent = Path(stripped).expanduser().parent
        parent_text = normalize_path_text(parent)
        return "" if parent_text == "." else parent_text

    @staticmethod
    def _default_pidfile_path_for_path(path_text: str) -> str:
        stripped = path_text.strip()
        if not stripped:
            return ""
        target_path = Path(stripped).expanduser()
        return normalize_path_text(target_path.with_suffix(".pid"))

    @classmethod
    def _apply_path_defaults_to_check(cls, path_text: str, check: CheckSpec) -> bool:
        changed = False
        if not path_text.strip():
            return False

        if check.type == CheckType.PIDFILE and not check.pidfile_path.strip():
            check.pidfile_path = cls._default_pidfile_path_for_path(path_text)
            changed = bool(check.pidfile_path)
        elif check.type == CheckType.PROCESS_NAME:
            inference = None
            if not check.process_name.strip() or not check.executable_path.strip():
                inference = infer_process_match(path_text)
            if not check.process_name.strip() and inference is not None:
                check.process_name = inference.process_name
                changed = True
            if not check.executable_path.strip() and inference is not None:
                check.executable_path = inference.executable_path
                changed = True
        return changed

    @classmethod
    def _apply_path_defaults_to_checks(cls, path_text: str, checks: list[CheckSpec]) -> bool:
        changed = False
        for check in checks:
            changed = cls._apply_path_defaults_to_check(path_text, check) or changed
        return changed

    def _apply_path_based_defaults(self) -> None:
        if self._editor_loading:
            return
        path_text = self._path_edit.text()
        if not path_text.strip():
            return

        if not self._working_dir_edit.text().strip():
            default_working_dir = self._default_working_dir_for_path(path_text)
            if default_working_dir:
                self._working_dir_edit.setText(default_working_dir)

        if self._apply_path_defaults_to_checks(path_text, self._current_checks):
            self._refresh_checks_table()

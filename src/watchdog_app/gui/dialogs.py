from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..models import (
    AutoStartScope,
    CheckSpec,
    CheckType,
    ConfigValidationError,
    ResolvedPaths,
    StorageMode,
    StoragePreferences,
)
from ..launchers import infer_process_match
from ..storage import log_output_root, resolve_paths


def _localize_dialog_buttons(buttons: QDialogButtonBox) -> None:
    text_map = {
        QDialogButtonBox.StandardButton.Ok: "確定",
        QDialogButtonBox.StandardButton.Cancel: "取消",
        QDialogButtonBox.StandardButton.Close: "關閉",
    }
    for button_type, text in text_map.items():
        button = buttons.button(button_type)
        if button is not None:
            button.setText(text)


def _with_browse(parent: QWidget, line_edit: QLineEdit, choose_directory: bool = False) -> QWidget:
    container = QWidget(parent)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QPushButton("瀏覽...", container)

    def _choose() -> None:
        if choose_directory:
            selected = QFileDialog.getExistingDirectory(parent, "選擇資料夾", line_edit.text())
        else:
            selected, _ = QFileDialog.getOpenFileName(parent, "選擇檔案", line_edit.text())
        if selected:
            line_edit.setText(selected)

    button.clicked.connect(_choose)
    layout.addWidget(line_edit, 1)
    layout.addWidget(button)
    return container


class StorageSetupDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("首次啟動設定")
        self.setModal(True)

        self._config_combo = QComboBox(self)
        self._config_combo.addItem(".exe 所在路徑", StorageMode.EXE)
        self._config_combo.addItem("AppData", StorageMode.APPDATA)
        self._config_combo.setCurrentIndex(1)

        self._log_combo = QComboBox(self)
        self._log_combo.addItem(".exe 所在路徑", StorageMode.EXE)
        self._log_combo.addItem("LocalAppData", StorageMode.LOCALAPPDATA)
        self._log_combo.setCurrentIndex(1)

        info = QLabel("第一次啟動請先選擇設定檔與日誌的儲存位置。", self)
        info.setWordWrap(True)

        form = QFormLayout()
        form.addRow("設定檔位置", self._config_combo)
        form.addRow("日誌位置", self._log_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        _localize_dialog_buttons(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def storage_preferences(self) -> StoragePreferences:
        return StoragePreferences(
            config_mode=self._config_combo.currentData(),
            log_mode=self._log_combo.currentData(),
        )


class SystemSettingsDialog(QDialog):
    def __init__(
        self,
        storage: StoragePreferences,
        resolved_paths: ResolvedPaths,
        scope: AutoStartScope,
        start_monitoring_on_login: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("系統設定")
        self.setModal(True)
        self.resize(560, 320)

        self._storage = storage.validate()
        self._resolved_paths = resolved_paths

        self._config_combo = QComboBox(self)
        self._config_combo.addItem(".exe 所在路徑", StorageMode.EXE)
        self._config_combo.addItem("AppData", StorageMode.APPDATA)
        self._config_combo.setCurrentIndex(max(0, self._config_combo.findData(self._storage.config_mode)))

        self._log_combo = QComboBox(self)
        self._log_combo.addItem(".exe 所在路徑", StorageMode.EXE)
        self._log_combo.addItem("LocalAppData", StorageMode.LOCALAPPDATA)
        self._log_combo.setCurrentIndex(max(0, self._log_combo.findData(self._storage.log_mode)))

        self._config_path_label = QLabel(self)
        self._config_path_label.setWordWrap(True)
        self._log_path_label = QLabel(self)
        self._log_path_label.setWordWrap(True)

        self._scope_combo = QComboBox(self)
        self._scope_combo.addItem("停用", AutoStartScope.DISABLED)
        self._scope_combo.addItem("目前使用者", AutoStartScope.CURRENT_USER)
        self._scope_combo.addItem("所有使用者", AutoStartScope.ALL_USERS)
        self._scope_combo.setCurrentIndex(max(0, self._scope_combo.findData(scope)))

        self._start_checkbox = QCheckBox("登入後自動開始監測", self)
        self._start_checkbox.setChecked(start_monitoring_on_login)

        form = QFormLayout()
        form.addRow("設定檔儲存", self._config_combo)
        form.addRow("設定檔路徑", self._config_path_label)
        form.addRow("日誌儲存", self._log_combo)
        form.addRow("日誌路徑", self._log_path_label)
        form.addRow("自動啟動範圍", self._scope_combo)
        form.addRow("", self._start_checkbox)

        button_box = QDialogButtonBox(self)
        self._save_button = button_box.addButton("儲存", QDialogButtonBox.ButtonRole.AcceptRole)
        self._cancel_button = button_box.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self._save_button.clicked.connect(self.accept)
        self._cancel_button.clicked.connect(self.reject)

        info = QLabel("調整 WatchDog 的儲存位置與自動啟動設定。", self)
        info.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(button_box)

        self._config_combo.currentIndexChanged.connect(self._refresh_path_preview)
        self._log_combo.currentIndexChanged.connect(self._refresh_path_preview)
        self._refresh_path_preview()

    def _refresh_path_preview(self) -> None:
        preview_prefs = self.storage_preferences()
        if (
            preview_prefs.config_mode == self._storage.config_mode
            and preview_prefs.log_mode == self._storage.log_mode
        ):
            preview = self._resolved_paths
        else:
            preview = resolve_paths(preview_prefs)
        self._config_path_label.setText(str(preview.config_path))
        self._log_path_label.setText(str(log_output_root(preview.log_directory)))

    def storage_preferences(self) -> StoragePreferences:
        return StoragePreferences(
            config_mode=self._config_combo.currentData(),
            log_mode=self._log_combo.currentData(),
        ).validate()

    def values(self) -> tuple[StoragePreferences, AutoStartScope, bool]:
        scope = self._scope_combo.currentData()
        if not isinstance(scope, AutoStartScope):
            scope = AutoStartScope(str(scope))
        return (
            self.storage_preferences(),
            scope,
            self._start_checkbox.isChecked(),
        )


class CheckEditorDialog(QDialog):
    def __init__(self, check: CheckSpec | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("檢查器設定")
        self.setModal(True)

        current = check or CheckSpec(type=CheckType.RUNTIME_PID)

        self._type_combo = QComboBox(self)
        self._type_combo.addItem("PID 檢查", CheckType.RUNTIME_PID)
        self._type_combo.addItem("PID 檔案檢查", CheckType.PIDFILE)
        self._type_combo.addItem("名稱檢查", CheckType.PROCESS_NAME)
        self._type_combo.addItem("TCP 連接埠檢查", CheckType.TCP_PORT)
        self._type_combo.addItem("HTTP 端點檢查", CheckType.HTTP_ENDPOINT)

        self._label_edit = QLineEdit(current.label, self)
        self._stack = QStackedWidget(self)

        self._pidfile_path = QLineEdit(current.pidfile_path, self)
        pidfile_page = QWidget(self)
        pidfile_form = QFormLayout(pidfile_page)
        pidfile_form.addRow("PID 檔案", _with_browse(self, self._pidfile_path))

        self._process_name = QLineEdit(current.process_name, self)
        self._exe_path = QLineEdit(current.executable_path, self)
        self._process_inference_note = QLabel(self)
        self._process_inference_note.setWordWrap(True)
        self._process_inference_note.hide()
        self._infer_process_button = QPushButton("由檔案路徑自動帶入...", self)
        self._infer_process_button.clicked.connect(self._infer_process_match_from_path)
        process_page = QWidget(self)
        process_form = QFormLayout(process_page)
        process_form.addRow("程序名稱", self._process_name)
        process_form.addRow("執行檔路徑", _with_browse(self, self._exe_path))
        process_form.addRow("", self._infer_process_button)
        process_form.addRow("", self._process_inference_note)

        self._tcp_host = QLineEdit(current.host, self)
        self._tcp_port = QSpinBox(self)
        self._tcp_port.setRange(1, 65535)
        self._tcp_port.setValue(current.port or 1)
        self._tcp_timeout = QDoubleSpinBox(self)
        self._tcp_timeout.setRange(0.05, 3600.0)
        self._tcp_timeout.setDecimals(2)
        self._tcp_timeout.setValue(current.timeout_sec)
        tcp_page = QWidget(self)
        tcp_form = QFormLayout(tcp_page)
        tcp_form.addRow("主機", self._tcp_host)
        tcp_form.addRow("連接埠", self._tcp_port)
        tcp_form.addRow("逾時秒數", self._tcp_timeout)

        self._http_url = QLineEdit(current.url, self)
        self._http_method = QComboBox(self)
        self._http_method.addItems(["GET", "POST", "HEAD"])
        self._http_method.setCurrentText(current.method)
        self._http_timeout = QDoubleSpinBox(self)
        self._http_timeout.setRange(0.05, 3600.0)
        self._http_timeout.setDecimals(2)
        self._http_timeout.setValue(current.timeout_sec)
        self._http_status = QSpinBox(self)
        self._http_status.setRange(100, 599)
        self._http_status.setValue(current.expected_status)
        self._http_body = QLineEdit(current.body_substring, self)
        http_page = QWidget(self)
        http_form = QFormLayout(http_page)
        http_form.addRow("URL", self._http_url)
        http_form.addRow("方法", self._http_method)
        http_form.addRow("逾時秒數", self._http_timeout)
        http_form.addRow("預期狀態碼", self._http_status)
        http_form.addRow("回應內容包含", self._http_body)

        self._stack.addWidget(QWidget(self))
        self._stack.addWidget(pidfile_page)
        self._stack.addWidget(process_page)
        self._stack.addWidget(tcp_page)
        self._stack.addWidget(http_page)
        self._type_combo.currentIndexChanged.connect(self._stack.setCurrentIndex)

        type_to_index = {
            CheckType.RUNTIME_PID: 0,
            CheckType.PIDFILE: 1,
            CheckType.PROCESS_NAME: 2,
            CheckType.TCP_PORT: 3,
            CheckType.HTTP_ENDPOINT: 4,
        }
        index = type_to_index[current.type]
        self._type_combo.setCurrentIndex(index)
        self._stack.setCurrentIndex(index)

        form = QFormLayout()
        form.addRow("類型", self._type_combo)
        form.addRow("標籤", self._label_edit)
        form.addRow("參數", self._stack)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        _localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._accept_with_validation)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._update_process_inference_note("")

    def _infer_process_match_from_path(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "選擇啟動檔案",
            self._exe_path.text(),
            "應用程式與腳本 (*.exe *.cmd *.bat *.ps1 *.py);;所有檔案 (*)",
        )
        if not selected:
            return

        inferred = infer_process_match(selected)
        self._process_name.setText(inferred.process_name)
        self._exe_path.setText(inferred.executable_path)
        self._update_process_inference_note(inferred.note)

    def _update_process_inference_note(self, note: str) -> None:
        if note:
            self._process_inference_note.setText(note)
            self._process_inference_note.show()
            return
        self._process_inference_note.clear()
        self._process_inference_note.hide()

    def _accept_with_validation(self) -> None:
        try:
            self.check_spec()
        except ConfigValidationError as exc:
            QMessageBox.warning(self, "檢查器設定錯誤", str(exc))
            return
        self.accept()

    def check_spec(self) -> CheckSpec:
        check_type = self._type_combo.currentData()
        spec = CheckSpec(
            type=check_type,
            label=self._label_edit.text(),
            pidfile_path=self._pidfile_path.text(),
            process_name=self._process_name.text(),
            executable_path=self._exe_path.text(),
            host=self._tcp_host.text(),
            port=self._tcp_port.value(),
            url=self._http_url.text(),
            method=self._http_method.currentText(),
            timeout_sec=self._http_timeout.value()
            if check_type == CheckType.HTTP_ENDPOINT
            else self._tcp_timeout.value(),
            expected_status=self._http_status.value(),
            body_substring=self._http_body.text(),
        )
        return spec.validate()


class ReadOnlyTextDialog(QDialog):
    def __init__(self, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 420)

        self._text_edit = QPlainTextEdit(self)
        self._text_edit.setReadOnly(True)
        self._text_edit.setPlainText(text)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        copy_button = buttons.addButton("複製", QDialogButtonBox.ButtonRole.ActionRole)
        copy_button.clicked.connect(self._copy_text)
        _localize_dialog_buttons(buttons)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self._text_edit, 1)
        layout.addWidget(buttons)

    def _copy_text(self) -> None:
        QApplication.clipboard().setText(self._text_edit.toPlainText())

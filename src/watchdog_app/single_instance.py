from __future__ import annotations

import hashlib

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .models import APP_NAME
from .runtime import runtime_base_dir


class SingleInstanceCoordinator(QObject):
    show_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        namespace = str(runtime_base_dir().resolve()).casefold()
        suffix = hashlib.sha1(namespace.encode("utf-8")).hexdigest()[:12]
        self._server_name = f"{APP_NAME}_PrimaryInstance_{suffix}"
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        probe = QLocalSocket(self)
        probe.connectToServer(self._server_name)
        if probe.waitForConnected(200):
            probe.write(b"SHOW")
            probe.flush()
            probe.waitForBytesWritten(200)
            probe.disconnectFromServer()
            return False

        QLocalServer.removeServer(self._server_name)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._handle_new_connection)
        if not self._server.listen(self._server_name):
            return False
        return True

    def close(self) -> None:
        if self._server:
            self._server.close()
            QLocalServer.removeServer(self._server_name)

    def _handle_new_connection(self) -> None:
        if not self._server:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        socket.waitForReadyRead(200)
        socket.readAll()
        socket.disconnectFromServer()
        self.show_requested.emit()

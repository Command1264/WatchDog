from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading

from watchdog_app.checkers import CheckContext, evaluate_check
from watchdog_app.models import CheckSpec, CheckType


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_tcp_check_passes_for_open_loopback_port() -> None:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = evaluate_check(
            CheckSpec(type=CheckType.TCP_PORT, host="127.0.0.1", port=port),
            CheckContext(),
        )
        assert result.healthy is True
    finally:
        server.shutdown()
        server.server_close()


def test_http_check_validates_expected_status_and_body() -> None:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = evaluate_check(
            CheckSpec(
                type=CheckType.HTTP_ENDPOINT,
                url=f"http://127.0.0.1:{port}/health",
                body_substring="ok",
            ),
            CheckContext(),
        )
        assert result.healthy is True
    finally:
        server.shutdown()
        server.server_close()

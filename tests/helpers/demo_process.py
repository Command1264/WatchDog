from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import os
import threading
import time


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pidfile", default="")
    parser.add_argument("--http-port", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=60.0)
    args = parser.parse_args()

    if args.pidfile:
        Path(args.pidfile).write_text(str(os.getpid()), encoding="utf-8")

    server = None
    thread = None
    if args.http_port:
        server = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    try:
        time.sleep(args.sleep)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if args.pidfile:
            Path(args.pidfile).unlink(missing_ok=True)


if __name__ == "__main__":
    main()

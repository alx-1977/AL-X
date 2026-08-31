#!/usr/bin/env python3
"""Perform the local, restart-safe Xero OAuth connection for AL/X."""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.config import XeroSettings  # noqa: E402
from alx.providers import SQLiteXeroOAuth  # noqa: E402


def _environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key.strip()] = value
    values.update(os.environ)
    return values


def main() -> int:
    environment = _environment(ROOT / ".env")
    settings = XeroSettings.from_environment(environment)
    storage_root = Path(environment.get("ALX_RUNTIME_STORAGE_ROOT", ".alx/runtime"))
    if not storage_root.is_absolute():
        storage_root = ROOT / storage_root
    oauth = SQLiteXeroOAuth(
        storage_root / "xero.sqlite3",
        settings.client_id,
        settings.client_secret,
        settings.redirect_uri,
        settings.tenant_id,
        settings.timeout_seconds,
    )
    callback = urlsplit(settings.redirect_uri)
    if callback.scheme != "http" or callback.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("XERO_REDIRECT_URI must be an http://localhost callback")
    if callback.port is None:
        raise SystemExit("XERO_REDIRECT_URI must include its callback port")
    callback_path = callback.path or "/"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTP protocol method
            parsed = urlsplit(self.path)
            if parsed.path != callback_path:
                self._reply(404, "Not found")
                return
            query = parse_qs(parsed.query)
            if query.get("error"):
                self._reply(400, "Xero authorisation was declined.")
                return
            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            try:
                tenant_name = oauth.exchange_code(code, state)
            except Exception as error:
                self._reply(400, f"Xero connection failed: {type(error).__name__}")
                return
            self._reply(200, f"Xero connected to {tenant_name}. You may close this tab.")

        def log_message(self, _format: str, *args) -> None:
            return

        def _reply(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    authorization_url = oauth.begin_authorization()
    print("Open this URL in your browser:")
    print(authorization_url)
    print(f"Waiting for Xero on {settings.redirect_uri}")
    server = HTTPServer((callback.hostname, callback.port), Handler)
    server.handle_request()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

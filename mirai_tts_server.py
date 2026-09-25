from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from voice import mirai_backend


HOST = "127.0.0.1"
PORT = int(os.getenv("MIRAI_TTS_PORT", "50150"))
MAX_BODY_BYTES = 256_000
MAX_TEXT_CHARS = 2_000


class Handler(BaseHTTPRequestHandler):
    server_version = "MiraiLocalTTS/0.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        info = mirai_backend.describe()
        self._json(
            {
                "ok": bool(mirai_backend.available()),
                "engine": "mirai-tts",
                "backend": info,
            }
        )

    def do_POST(self) -> None:
        if self.path != "/synthesize":
            self._json({"ok": False, "error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(
                {"ok": False, "error": "invalid body size"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            body = json.loads(
                self.rfile.read(length).decode("utf-8")
            )
        except Exception:
            self._json(
                {"ok": False, "error": "invalid json"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        text = str(body.get("text") or "").strip()
        if not text or len(text) > MAX_TEXT_CHARS:
            self._json(
                {"ok": False, "error": "invalid text"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        if not mirai_backend.available():
            self._json(
                {
                    "ok": False,
                    "error": "mirai native voice model not ready",
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        params = body.get("voice_params")
        if not isinstance(params, dict):
            params = {}

        try:
            wav = mirai_backend.synthesize(text, params)
            if (
                not isinstance(wav, (bytes, bytearray))
                or len(wav) < 44
                or not bytes(wav).startswith(b"RIFF")
            ):
                raise RuntimeError("backend returned invalid wav")
            raw = bytes(wav)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except Exception as exc:
            self._json(
                {"ok": False, "error": str(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def run() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"Mirai Local TTS listening on http://{HOST}:{PORT}"
    )
    server.serve_forever()


if __name__ == "__main__":
    run()

"""Мок OpenAI-совместимого `/v1/chat/completions` для тестов `llm_client` (llm_plam.md §4/§8).

Голый `http.server` -- не тянуть `requests`/Flask на серверную сторону
теста ради собственного HTTP-клиента, который и тестируем. Guardrail-сценарии
(несуществующая локация, инструмент вне `tools_allowed`, невалидный tool-call)
уже покрыты `test_validate.py` на уровне семантики -- этот мок отвечает
только за HTTP-механику бэкенда: обычный SSE-ответ, медленный (для abort),
зависший (для read timeout), HTTP-ошибка.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["MockLlmServer"]


class MockLlmServer:
    """Настраиваемый мок: обычный SSE-ответ / медленный / зависший / HTTP-ошибка."""

    MODE_OK = "ok"
    MODE_SLOW = "slow"  # чанки с паузой между ними -- для теста abort
    MODE_HANG = "hang"  # не отвечает вовсе -- для теста read timeout
    MODE_HTTP_ERROR = "http_error"

    def __init__(self) -> None:
        """Поднять сервер на свободном порту; поток не стартует -- см. `start()`."""
        self.mode = self.MODE_OK
        self.chunks: list[str] = ["Привет", ", ", "мир", "."]
        self.chunk_delay_s = 0.05
        self.hang_s = 10.0
        self.http_status = 500
        self.last_request_body: dict | None = None

        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, log_format: str, *args: object) -> None:
                del log_format, args  # тихо -- не мусорить в тестовый вывод

            def do_POST(self) -> None:  # noqa: N802 -- имя метода диктует http.server
                outer._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        # Без этого поток на MODE_HANG (спит hang_s) -- недемон, и может
        # держать процесс pytest живым после теста дольше, чем нужно.
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """Базовый URL в форме, которую ждёт `BackendConfig.base_url` (с `/v1`)."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        """Запустить обработку запросов в фоновом (демон-) потоке."""
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Остановить сервер и дождаться потока приёма соединений."""
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length)
        try:
            self.last_request_body = json.loads(body) if body else None
        except json.JSONDecodeError:
            self.last_request_body = None

        if self.mode == self.MODE_HTTP_ERROR:
            handler.send_response(self.http_status)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(b'{"error": "mock error"}')
            return

        if self.mode == self.MODE_HANG:
            time.sleep(self.hang_s)
            return

        # HTTP/1.1 + Transfer-Encoding: chunked -- без этого `http.client`
        # читает close-delimited ("identity") тело через `io.BufferedReader`,
        # который блокируется до заполнения запрошенного urllib3 буфера ИЛИ
        # EOF: маленькие SSE-чанки просто копятся до конца соединения, и
        # клиент видит их все разом на закрытии, а не по мере отправки --
        # тест abort тогда меряет не задержку до первого чанка, а время до
        # конца всего ответа. Chunked-фрейминг явно объявляет границу
        # каждого чанка, поэтому `http.client` отдаёт его сразу, как
        # реальный llama.cpp server со стримингом.
        handler.protocol_version = "HTTP/1.1"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()

        def _write_chunk(data: bytes) -> None:
            handler.wfile.write(f"{len(data):x}\r\n".encode())
            handler.wfile.write(data)
            handler.wfile.write(b"\r\n")
            handler.wfile.flush()

        delay = self.chunk_delay_s if self.mode == self.MODE_SLOW else 0.0
        for piece in self.chunks:
            event = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
            _write_chunk(f"data: {json.dumps(event)}\n\n".encode())
            if delay:
                time.sleep(delay)
        final = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        _write_chunk(f"data: {json.dumps(final)}\n\n".encode())
        _write_chunk(b"data: [DONE]\n\n")
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()

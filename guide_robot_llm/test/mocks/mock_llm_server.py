"""Мок OpenAI-совместимого `/v1/chat/completions` для тестов `llm_client` (llm_plam.md §4/§8).

Голый `http.server` -- не тянуть `requests`/Flask на серверную сторону
теста ради собственного HTTP-клиента, который и тестируем. Guardrail-сценарии
(несуществующая локация, инструмент вне `tools_allowed`, невалидный tool-call)
уже покрыты `test_validate.py` на уровне семантики -- этот мок отвечает
только за HTTP-механику бэкенда: обычный SSE-ответ, медленный (для abort),
зависший (для read timeout), HTTP-ошибка.

Таiga #3: валидация формы `messages.content` (строка или content-массив с
`image_url` data-URL'ами), fault-режимы для атрибуции отказов
(битый JSON, обрыв до ответа, задержка первого токена, обрыв серединой
стрима) и хранение редгированных метаданных запроса (`last_request_meta` /
`requests_log`) -- `Authorization` и base64-payload'ы кадров в них не
попадают по построению (см. `llm_client.redact`).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from guide_robot_llm.llm_client.redact import redact_headers, redact_value

__all__ = ["MockLlmServer", "validate_content"]


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Без traceback'ов в stderr на штатных обрывах fault-режимов (Taiga #3)."""

    daemon_threads = True

    # noqa: N803 -- сигнатура socketserver.handle_error
    def handle_error(self, request, client_address) -> None:  # noqa: N803
        del request, client_address  # disconnect/mid_stream-обрыв -- ожидаемое поведение мока


def validate_content(body: dict | None) -> dict:
    """Проверить форму `messages` в теле запроса (текст или content-массив).

    Возвращает `{"ok", "kinds", "error"}`: `kinds` -- последовательность
    `text`/`image_url` по всем сообщениям (порядок сохранения, чтобы тест
    мог проверить, что кадр приехал в нужном месте). Не data-URL
    `image_url` (например, сетевой URL) считается ошибкой: контракт #3 --
    кадры шлются data-URL'ами, не ссылками.
    """
    result: dict = {"ok": True, "kinds": [], "error": None}
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        result["ok"] = False
        result["error"] = "в теле нет messages"
        return result
    for index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            result["kinds"].append("text")
            continue
        if not isinstance(content, list):
            result["ok"] = False
            result["error"] = f"message {index}: content не строка и не массив"
            return result
        for part in content:
            part_type = part.get("type") if isinstance(part, dict) else None
            if part_type == "text" and isinstance(part.get("text"), str):
                result["kinds"].append("text")
            elif part_type == "image_url":
                url = (part.get("image_url") or {}).get("url")
                if isinstance(url, str) and url.startswith("data:"):
                    result["kinds"].append("image_url")
                else:
                    result["ok"] = False
                    result["error"] = f"message {index}: image_url не data-URL"
                    return result
            else:
                result["ok"] = False
                result["error"] = f"message {index}: неизвестный тип part {part_type!r}"
                return result
    return result


class MockLlmServer:
    """Настраиваемый мок: SSE-ответы, медленный/зависший/HTTP-ошибка, fault-режимы #3."""

    MODE_OK = "ok"
    MODE_SLOW = "slow"  # чанки с паузой между ними -- для теста abort
    MODE_HANG = "hang"  # не отвечает вовсе -- для теста read timeout
    MODE_HTTP_ERROR = "http_error"
    MODE_MALFORMED_JSON = "malformed_json"  # битый JSON в SSE -- отказ стадии generation
    MODE_DISCONNECT = "disconnect"  # обрыв соединения до ответа -- отказ стадии network
    MODE_DELAYED_FIRST_TOKEN = "delayed_first_token"  # ok + пауза до первого чанка (TTFT)
    MODE_MID_STREAM_FAILURE = "mid_stream_failure"  # N чанков, потом обрыв -- generation

    def __init__(self) -> None:
        """Поднять сервер на свободном порту; поток не стартует -- см. `start()`."""
        self.mode = self.MODE_OK
        self.chunks: list[str] = ["Привет", ", ", "мир", "."]
        # DIALOG_REWORK_PLAN.md: фаза 1 (без grammar в теле запроса) и фаза 2
        # (с grammar) -- один и тот же мок-сервер должен уметь отвечать
        # по-разному на каждую, иначе e2e-тест dialog_agent не может
        # заскриптовать реалистичный ход (текст ответа отдельно от tool-call
        # JSON). `None` -- используется `self.chunks` для обеих фаз (старое
        # поведение, тесты llm_client/backend.py его не трогают).
        self.chunks_no_grammar: list[str] | None = None
        self.chunks_with_grammar: list[str] | None = None
        self.chunk_delay_s = 0.05
        self.hang_s = 10.0
        self.http_status = 500
        self.last_request_body: dict | None = None
        # Таiga #3: редгированные метаданные последнего запроса и журнал всех.
        self.last_request_meta: dict | None = None
        self.requests_log: list[dict] = []
        self.request_count = 0
        # Taiga #3: fault-режимы.
        self.first_token_delay_s = 0.5  # MODE_DELAYED_FIRST_TOKEN: пауза до 1-го чанка
        self.fail_after_chunks = 2  # MODE_MID_STREAM_FAILURE: сколько чанков дойти

        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, log_format: str, *args: object) -> None:
                del log_format, args  # тихо -- не мусорить в тестовый вывод

            def do_POST(self) -> None:  # noqa: N802 -- имя метода диктует http.server
                outer._handle(self)

        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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

    def _record_meta(self, handler: BaseHTTPRequestHandler, validation: dict) -> None:
        """Записать редгированные метаданные запроса (`Authorization`/кадры не светятся)."""
        body_redacted = (
            redact_value(json.dumps(self.last_request_body, ensure_ascii=False))
            if self.last_request_body is not None
            else None
        )
        meta = {
            "path": handler.path,
            "headers": redact_headers(handler.headers),
            "body_redacted": body_redacted,
            "content_validation": validation,
        }
        self.last_request_meta = meta
        self.requests_log.append(meta)
        self.request_count += 1

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length)
        try:
            self.last_request_body = json.loads(body) if body else None
        except json.JSONDecodeError:
            self.last_request_body = None

        self._record_meta(handler, validate_content(self.last_request_body))

        if self.mode == self.MODE_DISCONNECT:
            # Обрыв до ответа: клиент видит connection reset -- отказ стадии network.
            handler.close_connection = True
            handler.connection.close()
            return

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

        has_grammar = bool(self.last_request_body and self.last_request_body.get("grammar"))
        if has_grammar and self.chunks_with_grammar is not None:
            chunks = self.chunks_with_grammar
        elif not has_grammar and self.chunks_no_grammar is not None:
            chunks = self.chunks_no_grammar
        else:
            chunks = self.chunks

        if self.mode == self.MODE_MALFORMED_JSON:
            # Битый JSON в первом же SSE-событии: ответ уже начался (есть
            # первый байт), токена ещё нет -- отказ атрибутируется в generation.
            _write_chunk(b"data: {broken-json\n\n")
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
            return

        if self.mode == self.MODE_MID_STREAM_FAILURE:
            # N нормальных чанков, потом резкий обрыв (без завершающего
            # 0-чанка): клиент получает первый токен, а затем ChunkedEncodingError
            # -- отказ стадии generation, как и у реального обрыва SSE.
            for piece in chunks[: self.fail_after_chunks]:
                event = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
                _write_chunk(f"data: {json.dumps(event)}\n\n".encode())
            handler.close_connection = True
            handler.connection.close()
            return

        delay = self.chunk_delay_s if self.mode == self.MODE_SLOW else 0.0
        first_delay = (
            self.first_token_delay_s if self.mode == self.MODE_DELAYED_FIRST_TOKEN else 0.0
        )
        for i, piece in enumerate(chunks):
            if i == 0 and first_delay:
                # Пауза ДО первого чанка: у реального VLM-сервера генерация
                # (prefill) идёт до первого токена -- TTFT меряется именно сюда.
                time.sleep(first_delay)
            event = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
            _write_chunk(f"data: {json.dumps(event)}\n\n".encode())
            if delay:
                time.sleep(delay)
        final = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        _write_chunk(f"data: {json.dumps(final)}\n\n".encode())
        _write_chunk(b"data: [DONE]\n\n")
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()

"""Маскирование секретов в строках ошибок и логах (Taiga #3).

Требование issue #3: "authorization headers и image payloads никогда не
появляются в error strings". Здесь -- единственный инструмент для этого:
`backend`/`ladder` пропускают через `redact_value`/`redact_headers` каждое
сообщение об ошибке, которое может нести данные запроса, а мок-сервер
хранит `last_request_meta` в редгированном виде (см. `mock_llm_server`).

Только строка маскируется, объект не мутируется: вызывающий передаёт
`str(error)`, `headers` и `messages` -- и дальше использует их как обычно.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

__all__ = ["redact_value", "redact_headers", "redact_messages", "redact_record_for_export"]

# `data:<mime>;base64,<payload>` -- маска с сохранением префикса и длины
# payload (длина полезна для диагностики: "кадр 2 МБ не ушёл" -- видно,
# не раскрывая содержимое кадра).
_DATA_URL_RE = re.compile(r"(data:[A-Za-z0-9.+\-]+/[A-Za-z0-9.+\-]+;base64,)([A-Za-z0-9+/=]+)")
_BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)

# Ключи заголовков, у которых маскаруется значение целиком (в любом
# регистре): именно эти несёт запрос к внешнему эндпоинту.
_SENSITIVE_HEADER_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-auth",
        "token",
        "session",
        "cookie",
        "set-cookie",
        "x-amz-security-token",
    }
)


def redact_value(value: str) -> str:
    """Заменить в строке Bearer-токены и base64-payload'ы data-URL'ов.

    Всё остальное (статусы, причины, обрезанные тела серверных ответов)
    возвращается как есть -- диагностичность ошибки не страдает.
    """
    masked = _BEARER_RE.sub("Bearer <redacted>", value)
    masked = _DATA_URL_RE.sub(
        lambda match: f"{match.group(1)}<<REDACTED {len(match.group(2))} bytes>>",
        masked,
    )
    return masked


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Копия заголовков запроса с замаскированными чувствительными значениями.

    Ключ чувствителен по регистру как в `Mapping`, сравнение -- нижним
    регистром. Незначимые заголовки (`Content-Type` и т.п.) проходят
    через `redact_value` -- на них маскирование не сработает, но если бы
    значение и несло секрет, он бы замаскировался.
    """
    masked: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADER_KEYS:
            masked[key] = "<redacted>"
        else:
            masked[key] = redact_value(str(value))
    return masked


def redact_messages(messages: list[dict]) -> list[dict]:
    """Редгированная копия списка OpenAI-`messages` для логирования.

    Строковый `content` и parts content-массива (`text`,
    `image_url.url`) проходят через `redact_value`; структура сообщений
    сохраняется 1:1, чтобы лог можно было сопоставить с запросом.
    Вход не мутируется.
    """
    redacted: list[dict] = []
    for message in messages:
        masked_message = dict(message)
        content = masked_message.get("content")
        if isinstance(content, str):
            masked_message["content"] = redact_value(content)
        elif isinstance(content, list):
            masked_parts: list[dict] = []
            for part in content:
                masked_part = dict(part)
                part_type = masked_part.get("type")
                if part_type == "text" and isinstance(masked_part.get("text"), str):
                    masked_part["text"] = redact_value(masked_part["text"])
                elif part_type == "image_url" and isinstance(masked_part.get("image_url"), dict):
                    image_url = dict(masked_part["image_url"])
                    if isinstance(image_url.get("url"), str):
                        image_url["url"] = redact_value(image_url["url"])
                    masked_part["image_url"] = image_url
                masked_parts.append(masked_part)
            masked_message["content"] = masked_parts
        redacted.append(masked_message)
    return redacted


def redact_record_for_export(record: dict, *, redact_utterance: bool = True) -> dict:
    """Копия записи лога для выгрузки бенчмарка (issue #8).

    При `redact_utterance` маскирует реплику посетителя (`utterance`) --
    PII не уходит в общий датасет. `llm_messages` уже редгированы на самой
    записи (base64/секреты, `redact_messages`), поэтому здесь не трогаются.
    Метрики, тайминги и id сохраняются для воспроизводимости прогона.
    Вход не мутируется.
    """
    exported = dict(record)
    if redact_utterance and "utterance" in exported:
        exported["utterance"] = "<redacted>"
    return exported

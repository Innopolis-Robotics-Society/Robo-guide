"""Транспорт до ESP32-S3/RC522 через CDC-сериал -- без rclpy (design E4).

Протокол -- построчный JSON, один объект на строку:

    -> {"cmd":"challenge","nonce":"<64 hex>"}
    <- {"ok":true,"resp":"<hmac hex>","card":"op_mook"}
    <- {"ok":false,"err":"no_card"}

Граница доверия: секрет не покидает ESP, по проводу едет только подпись
(`resp`) -- не UID, не содержимое сектора. HMAC считается и сверяется на
стороне узла (`RfidBackend` в lib/auth.py), этот модуль только гоняет
JSON туда-обратно и парсит ответ до известной схемы (`ChallengeResult`):
что бы ESP ни прислал лишнего, наружу уходят ровно `ok`/`resp`/`card`/`err`
(design E4, критерий 17 -- ни в одном поле не должно быть UID).

Транспорт за интерфейсом (`SerialPort`), по образцу
`guide_robot_voice/lib/sink.py`'s `Emitter`/`MemoryEmitter` -- фейк живёт
здесь же, в библиотеке, не в test/, чтобы им могли пользоваться и другие
тесты пакета.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

__all__ = ["ChallengeResult", "MemorySerialPort", "PySerialPort", "RfidLink", "SerialPort"]


class SerialPort(Protocol):
    """Строка за строкой поверх последовательного порта."""

    def write_line(self, line: str) -> None:
        """Отправить одну строку (перевод строки добавляет реализация)."""

    def read_line(self, timeout_s: float) -> str | None:
        """Дождаться одной строки до timeout_s; None -- таймаут, без исключения."""

    def close(self) -> None:
        """Освободить порт."""


class PySerialPort:
    """Реальный порт через pyserial."""

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        """Открыть порт. Бросает OSError при отсутствии устройства -- ловит вызывающий."""
        import serial  # локальный импорт: модуль нужен только реальному порту, не фейку/тестам

        self._serial = serial.Serial(port, baudrate=baudrate, timeout=0)

    def write_line(self, line: str) -> None:
        """Отправить строку с завершающим переводом строки."""
        self._serial.write((line + "\n").encode("utf-8"))

    def read_line(self, timeout_s: float) -> str | None:
        """readline() с точечным таймаутом на этот вызов, не на весь порт."""
        self._serial.timeout = timeout_s
        raw = self._serial.readline()  # b"" -- ничего не пришло за timeout_s
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").strip()

    def close(self) -> None:
        """Закрыть порт."""
        self._serial.close()


class MemorySerialPort:
    """Фейковый порт для тестов: очередь заранее заданных строк-ответов."""

    def __init__(self) -> None:
        """Создать порт без единого подготовленного ответа."""
        self.written: list[str] = []
        self.responses: list[str | None] = []  # следующий read_line() берёт из головы очереди

    def write_line(self, line: str) -> None:
        """Запомнить отправленную строку -- тест может её проверить."""
        self.written.append(line)

    def read_line(self, timeout_s: float) -> str | None:
        """Отдать следующий подготовленный ответ; пустая очередь -- имитация таймаута."""
        del timeout_s
        if not self.responses:
            return None
        return self.responses.pop(0)

    def close(self) -> None:
        """Фейковому порту нечего закрывать."""


@dataclass(frozen=True)
class ChallengeResult:
    """Разобранный ответ ESP -- ровно эти четыре поля, никогда UID."""

    ok: bool
    resp: str  # HMAC hex; пусто при ok=False
    card: str  # логическое имя оператора; пусто при ok=False
    err: str  # причина отказа; пусто при ok=True


class RfidLink:
    """Один запрос challenge/response на вызов; состояния между вызовами не хранит."""

    def __init__(self, port: SerialPort, *, timeout_s: float = 0.3) -> None:
        """Запомнить порт и таймаут ответа (design E4, `rfid_timeout_s`, дефолт 0.3 с)."""
        self._port = port
        self._timeout_s = timeout_s

    def challenge(self, nonce: str) -> ChallengeResult:
        """Отправить nonce, дождаться ответа до timeout_s.

        Не ответил / не JSON / нет "ok" -- ChallengeResult(ok=False, ...),
        НИКОГДА исключение: единственный потребитель (`RfidBackend.verify`)
        обязан трактовать любой из этих исходов как "бэкенд недоступен",
        падение на следующий бэкенд в списке -- не ошибку кода (design E4:
        "не ответил -- недоступен, никаких ожиданий в UI").
        """
        try:
            self._port.write_line(json.dumps({"cmd": "challenge", "nonce": nonce}))
        except OSError:
            return ChallengeResult(ok=False, resp="", card="", err="write_failed")

        line = self._port.read_line(self._timeout_s)
        if line is None:
            return ChallengeResult(ok=False, resp="", card="", err="timeout")
        try:
            data = json.loads(line)
        except ValueError:
            return ChallengeResult(ok=False, resp="", card="", err="bad_response")
        if not isinstance(data, dict) or "ok" not in data:
            return ChallengeResult(ok=False, resp="", card="", err="bad_response")
        if not data["ok"]:
            return ChallengeResult(ok=False, resp="", card="", err=str(data.get("err", "")))
        return ChallengeResult(
            ok=True, resp=str(data.get("resp", "")), card=str(data.get("card", "")), err=""
        )

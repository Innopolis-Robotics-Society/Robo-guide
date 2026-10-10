#!/usr/bin/env python3
"""Изолированный TTS-пилот «Фирая»: по умолчанию только план, без сети и оплаты.

Запускать на хосте: ROS, Docker и дополнительные Python-пакеты не нужны.
--auto-voices запрашивает доступные русские голоса аккаунта (нужен ключ).
--execute разрешает платные запросы только с явным --max-characters.
Ключ берётся из ELEVENLABS_API_KEY или вводится скрыто, .env не читается.

Важно: стандартная политика ElevenLabs 9(k-l) запрещает использование Output
для ML. Частное использование этого не меняет; отдельные условия нужно
согласовать с провайдером. Скрипт не подтверждает наличие такого разрешения.
Все WAV остаются review_required, автоматически в обучение не попадают.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.elevenlabs.io"
OUTPUT = Path(__file__).resolve().parents[1] / (
    "datasets/wakeword/firaya_v1/synthetic/elevenlabs_pilot_01"
)
POLICY = "https://elevenlabs.io/use-policy"
MAX_AUDIO_BYTES = 16000 * 2 * 60
POSITIVE = (
    "Фирая.",
    "Фирая!",
    "Фирая, расскажи про картину.",
    "Скажи, Фирая, где выход?",
    "Привет, Фирая.",
    "Мы проверяем микрофон. Фирая, ты здесь?",
)
NEGATIVE = (
    "Феррари.",
    "Филлер.",
    "Фонвизин.",
    "Фидер.",
    "Фак.",
    "Фирма.",
    "Фигура.",
    "Фиалка.",
    "Финал.",
    "Фильтр.",
    "Февраль.",
    "Я видел красную феррари.",
    "Нужно заменить фильтр.",
    "Проверим фидер и модель.",
    "Сегодня много людей, и все разговаривают.",
    "Расскажи про эту картину и покажи дорогу к выходу.",
)


def json_bytes(value: object) -> bytes:
    """Сериализовать план без секретов в стабильном порядке."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def write_new(path: Path, data: bytes) -> None:
    """Создать новый файл, никогда не перезаписывая существующий."""
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def build_plan(voices: list[str], repeats: int, model: str) -> dict:
    """Использовать одинаковые голоса, настройки и повторы для обеих меток."""
    if repeats < 1 or not voices or len(set(voices)) != len(voices):
        raise ValueError("нужны уникальные голоса и положительное число повторов")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", voice) for voice in voices):
        raise ValueError("некорректный voice ID")
    samples = []
    for voice in voices:
        for variant, stability in enumerate((0.45, 0.7)):
            settings = {
                "stability": stability,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            }
            for take in range(repeats):
                for kind, texts in (("positive", POSITIVE), ("negative", NEGATIVE)):
                    for text in texts:
                        payload = {
                            "text": text,
                            "model_id": model,
                            "voice_settings": settings,
                            "seed": 1337 + variant * 100 + take,
                        }
                        digest = hashlib.sha256(json_bytes([voice, payload])).hexdigest()
                        samples.append(
                            {
                                "id": digest,
                                "kind": kind,
                                "voice_id": voice,
                                "payload": payload,
                                "characters": len(text),
                                "file": f"{kind}/{voice}/{digest}.wav",
                            }
                        )
    return {
        "schema_version": 1,
        "provider": "elevenlabs",
        "output_format": "pcm_16000",
        "purpose": "pronunciation_review_pilot",
        "split": "train_only",
        "review_required": True,
        "training_ready": False,
        "policy_url": POLICY,
        "policy_warning": "Стандартные условия 9(k-l) запрещают использование Output для ML.",
        "billing_warning": "Символы текста — не цена и не количество кредитов.",
        "samples": samples,
        "characters": sum(row["characters"] for row in samples),
    }


def api_request(path: str, key: str, payload: dict | None = None) -> tuple[bytes, dict]:
    """Сделать один запрос без повторов, редиректов и вывода тела ошибок."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    headers = {
        "xi-api-key": key,
        "Accept": "application/json" if payload is None else "application/octet-stream",
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(
        API + path,
        data=json_bytes(payload) if payload is not None else None,
        headers=headers,
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=90) as response:
            data = response.read(MAX_AUDIO_BYTES + 1)
            if len(data) > MAX_AUDIO_BYTES:
                raise ValueError("ответ слишком большой; платный запрос не будет повторён")
            return data, dict(response.headers)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"ElevenLabs HTTP {error.code}; запрос не повторён") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("сетевая ошибка; запрос мог быть оплачен и НЕ будет повторён") from None


def read_key() -> str:
    """Получить ключ без вывода, сохранения на диск и аргументов командной строки."""
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        if not sys.stdin.isatty():
            raise ValueError("нет ELEVENLABS_API_KEY; запустите скрипт в обычном терминале")
        key = getpass.getpass("Ключ ElevenLabs API (ввод скрыт, не сохраняется): ").strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("пустой или некорректный ключ API")
    return key


def available_voices(key: str) -> list[dict]:
    """Прочитать русские голоса, не создавая клонов и не меняя библиотеку."""
    token = None
    seen_tokens = set()
    voices = {}
    while True:
        query = {"page_size": 100, "language": "ru", "include_total_count": "false"}
        if token:
            query["next_page_token"] = token
        data, _ = api_request("/v2/voices?" + urllib.parse.urlencode(query), key)
        page = json.loads(data)
        for voice in page["voices"]:
            voices[voice["voice_id"]] = {
                "voice_id": voice["voice_id"],
                "name": voice.get("name", ""),
                "category": voice.get("category", ""),
                "labels": voice.get("labels", {}),
            }
        if not page.get("has_more"):
            break
        token = page.get("next_page_token")
        if not token or token in seen_tokens:
            raise ValueError("некорректная пагинация списка голосов")
        seen_tokens.add(token)
    return sorted(voices.values(), key=lambda row: (row["name"], row["voice_id"]))


def choose_voices(voices: list[dict]) -> list[str]:
    """Выбрать два русских голоса, по возможности с разными gender-метками."""
    if len(voices) < 2:
        raise ValueError("в аккаунте меньше двух русских голосов; укажите --voice вручную")
    first = voices[0]
    gender = first["labels"].get("gender")
    second = next(
        (
            row
            for row in voices[1:]
            if gender and row["labels"].get("gender") and row["labels"]["gender"] != gender
        ),
        voices[1],
    )
    for row in (first, second):
        print(f"Голос: {row['name']} ({row['voice_id']}); произношение нужно прослушать")
    return [first["voice_id"], second["voice_id"]]


def pcm_to_wav(pcm: bytes, headers: dict) -> bytes:
    """Оборачивать PCM16 LE mono 16k в WAV без добавленной тишины и нормализации."""
    content_type = next(
        (value for key, value in headers.items() if key.lower() == "content-type"), ""
    ).lower()
    allowed = {"audio/pcm", "audio/x-pcm", "audio/raw", "application/octet-stream"}
    if (content_type and content_type.split(";")[0].strip() not in allowed) or (
        pcm.startswith((b"RIFF", b"ID3"))
    ):
        raise ValueError("получен не raw PCM; ответ не будет помечен обучающим примером")
    if len(pcm) < 3200 or len(pcm) > MAX_AUDIO_BYTES or len(pcm) % 2:
        raise ValueError("неожиданный размер PCM16; запрос не повторяем")
    if not any(pcm):
        raise ValueError("получена цифровая тишина; запрос не повторяем")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return buffer.getvalue()


def generate(plan: dict, output: Path, key: str, limit: int, resume: bool) -> int:
    """Сохранить WAV отдельно; незавершённый платный запрос никогда не повторять."""
    if limit < 1 or plan["characters"] > limit:
        raise ValueError(
            f"план требует {plan['characters']} символов, лимит {limit}; API не вызван"
        )
    if output.exists() and not resume:
        raise ValueError("папка уже существует; используйте --resume или новую папку")
    if output.is_symlink():
        raise ValueError("папка вывода не должна быть символьной ссылкой")
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".generation.lock"
    write_new(lock, b"single writer\n")
    try:
        saved_plan = output / "plan.json"
        if saved_plan.exists():
            if json.loads(saved_plan.read_text()) != plan:
                raise ValueError("изменён план существующего пилота; платные запросы не выполнены")
        else:
            write_new(saved_plan, json_bytes(plan))
        metadata = output / "metadata"
        metadata.mkdir(exist_ok=True)
        done = {}
        for sample in plan["samples"]:
            identity = sample["id"]
            record_file = metadata / f"{identity}.json"
            wav = output / sample["file"]
            attempt = metadata / f"{identity}.attempt.json"
            if record_file.exists():
                record = json.loads(record_file.read_text())
                if (
                    record["sample"] != sample
                    or not wav.is_file()
                    or (hashlib.sha256(wav.read_bytes()).hexdigest() != record["sha256"])
                ):
                    raise ValueError(f"повреждён результат: {sample['file']}; не перезаписываем")
                done[identity] = record
            elif attempt.exists() or wav.exists():
                raise ValueError(
                    f"незавершённый запрос {identity}: он мог быть оплачен; "
                    "автоповтор заблокирован. Сохраните папку для разбора."
                )
        pending = [row for row in plan["samples"] if row["id"] not in done]
        attempts = [json.loads(path.read_text()) for path in metadata.glob("*.attempt.json")]
        reserved = sum(row["characters"] for row in attempts)
        if reserved + sum(row["characters"] for row in pending) > limit:
            raise ValueError("с учётом прежних попыток превышен лимит; API не вызван")
        for index, sample in enumerate(pending, 1):
            identity = sample["id"]
            target = output / sample["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            write_new(
                metadata / f"{identity}.attempt.json",
                json_bytes(
                    {
                        "characters": sample["characters"],
                        "sample_id": identity,
                        "issued_at": datetime.now(timezone.utc).isoformat(),
                        "billing_status": "possibly_charged",
                        "automatic_retry": False,
                    }
                ),
            )
            pcm, headers = api_request(
                f"/v1/text-to-speech/{sample['voice_id']}?output_format=pcm_16000",
                key,
                sample["payload"],
            )
            data = pcm_to_wav(pcm, headers)
            write_new(target, data)
            record = {
                "sample": sample,
                "sha256": hashlib.sha256(data).hexdigest(),
                "seconds": len(pcm) / 32000,
                "sample_rate": 16000,
                "channels": 1,
                "provider": "elevenlabs",
                "split": "train_only",
                "review_required": True,
                "label_verified": False,
                "request_id": next(
                    (value for name, value in headers.items() if name.lower() == "request-id"),
                    None,
                ),
            }
            write_new(metadata / f"{identity}.json", json_bytes(record))
            done[identity] = record
            print(
                f"{index}/{len(pending)} {sample['kind']}: {sample['payload']['text']}",
                flush=True,
            )
        manifest = output / "manifest.jsonl"
        contents = b"".join(
            json.dumps(done[row["id"]], ensure_ascii=False, sort_keys=True).encode() + b"\n"
            for row in plan["samples"]
        )
        if manifest.exists():
            if manifest.read_bytes() != contents:
                raise ValueError("manifest изменён; существующий файл не перезаписываем")
        else:
            write_new(manifest, contents)
        return len(pending)
    finally:
        lock.unlink()


def main(argv: list[str] | None = None) -> int:
    """Показать план или выполнить явно разрешённый, ограниченный по объёму пилот."""
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--voice", action="append", help="voice ID; можно повторять")
    choice.add_argument("--auto-voices", action="store_true", help="GET API: выбрать 2 ru-голоса")
    parser.add_argument(
        "--list-voices", action="store_true", help="GET API: русские голоса аккаунта"
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--model", default="eleven_multilingual_v2")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--execute", action="store_true", help="разрешить платную генерацию")
    parser.add_argument("--max-characters", type=int, help="обязательный лимит всего пилота")
    parser.add_argument("--resume", action="store_true", help="пропустить проверенные готовые WAV")
    args = parser.parse_args(argv)
    try:
        if args.execute and (args.max_characters is None or args.max_characters < 1):
            raise ValueError("для --execute обязателен положительный --max-characters")
        key = None
        if args.list_voices or args.auto_voices:
            key = read_key()
            voices = available_voices(key)
            if args.list_voices:
                print(json.dumps(voices, ensure_ascii=False, indent=2))
                return 0
            ids = choose_voices(voices)
        else:
            ids = args.voice or ["PREVIEW_VOICE_1", "PREVIEW_VOICE_2"]
        plan = build_plan(ids, args.repeats, args.model)
        print(f"План: {len(plan['samples'])} WAV; {plan['characters']} символов текста")
        print("По меткам:", dict(Counter(row["kind"] for row in plan["samples"])))
        print("Символы текста не равны кредитам. Папка:", args.output)
        print("Предупреждение: стандартная политика ElevenLabs 9(k-l) запрещает Output для ML.")
        if args.max_characters is not None and plan["characters"] > args.max_characters:
            raise ValueError("план превышает лимит символов; платные запросы не выполнены")
        if not args.execute:
            print("Предпросмотр: WAV не создавались, платных запросов нет.")
            return 0
        if any(voice.startswith("PREVIEW_") for voice in ids):
            raise ValueError("задайте --voice либо --auto-voices; заглушки не отправляются в API")
        if key is None:
            key = read_key()
        count = generate(plan, args.output, key, args.max_characters, args.resume)
        print(f"Готово: новых WAV {count}. Прослушайте произношение до обучения.")
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

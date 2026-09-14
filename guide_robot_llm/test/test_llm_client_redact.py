"""`llm_client.redact` -- маскирование секретов в строках ошибок и логах (Taiga #3)."""

from __future__ import annotations

from guide_robot_llm.llm_client.redact import redact_headers, redact_messages, redact_value

_PAYLOAD = "QUJD" * 300  # 1200 байт base64


def test_bearer_token_masked() -> None:
    assert redact_value("Authorization: Bearer abc.secret-123") == (
        "Authorization: Bearer <redacted>"
    )


def test_bearer_token_masked_case_insensitive() -> None:
    assert redact_value("bearer XYZ789") == "Bearer <redacted>"


def test_data_url_payload_masked_with_length() -> None:
    url = f"data:image/jpeg;base64,{_PAYLOAD}"

    assert redact_value(url) == f"data:image/jpeg;base64,<<REDACTED {len(_PAYLOAD)} bytes>>"


def test_plain_error_text_unchanged() -> None:
    text = 'HTTP 500: {"error": "internal"}'
    assert redact_value(text) == text


def test_redact_headers_masks_sensitive_keys_only() -> None:
    headers = {
        "Authorization": "Bearer secret-token",
        "X-Api-Key": "api-secret",
        "Content-Type": "application/json",
        "X-Client": "guide_robot_llm",
    }

    masked = redact_headers(headers)

    assert masked["Authorization"] == "<redacted>"
    assert masked["X-Api-Key"] == "<redacted>"
    assert masked["Content-Type"] == "application/json"
    assert masked["X-Client"] == "guide_robot_llm"
    assert headers["Authorization"] == "Bearer secret-token"  # вход не мутируется


def test_redact_messages_string_content() -> None:
    raw_content = "Bearer abc и data:image/png;base64,QUJD"
    messages = [{"role": "user", "content": raw_content}]

    masked = redact_messages(messages)

    expected = "Bearer <redacted> и data:image/png;base64,<<REDACTED 4 bytes>>"
    assert masked[0]["content"] == expected
    assert messages[0]["content"] == raw_content


def test_redact_messages_multimodal_content_array() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "что за картина?"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_PAYLOAD}"}},
            ],
        },
    ]

    masked = redact_messages(messages)

    parts = masked[0]["content"]
    assert parts[0] == {"type": "text", "text": "что за картина?"}
    masked_url = parts[1]["image_url"]["url"]
    assert masked_url == f"data:image/jpeg;base64,<<REDACTED {len(_PAYLOAD)} bytes>>"
    # Исходный payload остался в входных сообщениях -- копия, а не мутация.
    assert messages[0]["content"][1]["image_url"]["url"].endswith(_PAYLOAD)

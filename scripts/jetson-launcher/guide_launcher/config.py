"""Конфиг guide-launcher: дефолты + слияние с YAML, проверки старта."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

EXIT_CONFIG = 2
MIN_PIN_LEN = 8

_REPO = "~/Desktop/Projects/Robo-guide"


class ConfigError(Exception):
    """Конфиг не позволяет стартовать (сервис завершается с кодом 2)."""


@dataclass
class Config:
    """Все ключи /etc/guide-launcher/config.yaml с дефолтами."""

    bind_host: str = "127.0.0.1"
    http_port: int = 8089
    container: str = "robo-guide-jetson-1"
    start_cmd: str = "/home/fabian/ros2_ws/src/scripts/jetson-launcher/start_stack.sh"
    compose_file: str = f"{_REPO}/compose.yaml"
    compose_service: str = "jetson"
    stack_log: str = "/tmp/stack.log"
    bridge_url: str = "http://127.0.0.1:8091"
    bridge_token_file: str = f"{_REPO}/.guide_launcher/bridge_token"
    autostart_stack: bool = False
    start_timeout_s: float = 120.0
    poll_interval_s: float = 2.0
    promo_dir: str = f"{_REPO}/reference_ui/promo"
    promo_interval_s: float = 10.0
    slide_interval_s: float = 8.0
    always_promo: bool = True
    # Кнопки «Да» / «Стоп» / «Задать вопрос» на экране, пока робот спрашивает «Идём дальше?».
    visitor_confirm_buttons: bool = True
    default_tour: str = "expo_one"
    operator_pin: str = ""
    auth_backends: tuple[str, ...] = ("rfid", "pin")
    rfid_port: str = "/dev/rfid0"
    rfid_secret_file: str = ""
    session_ttl_s: float = 600.0
    state_dir: str = "~/.guide_robot/launcher"
    web_dir: str = ""
    audio_socket: str = ""
    wifi_socket: str = ""
    power_control_enabled: bool = False
    serve_ui: bool = False
    public_tours: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    @property
    def stack_control(self) -> bool:
        """Управление стеком включено, только если задан контейнер."""
        return bool(self.container)

    def bridge_token(self) -> str:
        """Прочитать секрет моста; пустой или отсутствующий файл -- ConfigError."""
        path = Path(self.bridge_token_file)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"bridge_token_file {path}: не прочитан ({exc})") from exc
        if not token:
            raise ConfigError(f"bridge_token_file {path}: пуст")
        return token


def _expand(value: str) -> str:
    return os.path.expanduser(value) if value else value


def load_config(path: str | Path | None) -> Config:
    """Прочитать YAML (если задан), наложить на дефолты, проверить. Бросает ConfigError."""
    raw: dict[str, Any] = {}
    if path is not None:
        try:
            loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"{path}: не прочитан ({exc})") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: корень должен быть отображением")
        raw = loaded

    known = {f.name for f in fields(Config)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"неизвестные ключи конфига: {', '.join(unknown)}")

    cfg = Config(**{k: (tuple(v) if k == "auth_backends" else v) for k, v in raw.items()})
    for name in (
        "bridge_token_file",
        "compose_file",
        "promo_dir",
        "state_dir",
        "rfid_secret_file",
        "web_dir",
        "audio_socket",
        "wifi_socket",
    ):
        setattr(cfg, name, _expand(getattr(cfg, name)))

    if not isinstance(cfg.power_control_enabled, bool):
        raise ConfigError("power_control_enabled must be boolean")
    for name in ("audio_socket", "wifi_socket"):
        value = getattr(cfg, name)
        if not isinstance(value, str) or (value and not Path(value).is_absolute()):
            raise ConfigError(f"{name} must be an absolute Unix socket path")

    if len(str(cfg.operator_pin)) < MIN_PIN_LEN:
        raise ConfigError(f"operator_pin короче {MIN_PIN_LEN} символов")
    cfg.operator_pin = str(cfg.operator_pin)
    from urllib.parse import urlparse

    if cfg.bind_host not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError("API must bind to loopback")
    for field in ("allowed_origins", "public_tours"):
        values = getattr(cfg, field)
        if not isinstance(values, (list, tuple)) or not all(isinstance(x, str) for x in values):
            raise ConfigError(f"{field} must be a list of strings")
    bridge = urlparse(cfg.bridge_url)
    if (
        bridge.scheme != "http"
        or bridge.hostname not in ("localhost", "127.0.0.1", "::1")
        or bridge.username
    ):
        raise ConfigError("Bridge must be a local HTTP service")
    for origin in cfg.allowed_origins:
        parsed = urlparse(origin)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ConfigError("Only explicit loopback origins are supported")
    cfg.bridge_token()
    return cfg

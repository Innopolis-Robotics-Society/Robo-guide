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
    stack_log: str = "/tmp/stack.log"
    bridge_url: str = "http://127.0.0.1:8091"
    bridge_token_file: str = f"{_REPO}/.guide_launcher/bridge_token"
    autostart_stack: bool = False
    start_timeout_s: float = 120.0
    poll_interval_s: float = 2.0
    promo_dir: str = f"{_REPO}/guide_robot_operator_ui/promo"
    promo_interval_s: float = 10.0
    slide_interval_s: float = 8.0
    always_promo: bool = True
    default_tour: str = "expo_one"
    operator_pin: str = ""
    auth_backends: tuple[str, ...] = ("rfid", "pin")
    rfid_port: str = "/dev/rfid0"
    rfid_secret_file: str = ""
    session_ttl_s: float = 600.0
    state_dir: str = "~/.guide_robot/launcher"
    web_dir: str = ""

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
    for name in ("bridge_token_file", "promo_dir", "state_dir", "rfid_secret_file", "web_dir"):
        setattr(cfg, name, _expand(getattr(cfg, name)))

    if len(str(cfg.operator_pin)) < MIN_PIN_LEN:
        raise ConfigError(f"operator_pin короче {MIN_PIN_LEN} символов")
    cfg.operator_pin = str(cfg.operator_pin)
    cfg.bridge_token()
    return cfg

"""Точка входа: python3 -m guide_launcher --config /etc/guide-launcher/config.yaml."""

from __future__ import annotations

import argparse
import logging
import sys

from aiohttp import web

from .config import EXIT_CONFIG, ConfigError, load_config
from .server import create_app


def main(argv: list[str] | None = None) -> int:
    """Прочитать конфиг и запустить HTTP-сервер; ошибки конфига -- код 2."""
    parser = argparse.ArgumentParser(prog="guide_launcher")
    parser.add_argument("--config", default="/etc/guide-launcher/config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"guide-launcher: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    web.run_app(create_app(cfg), host=cfg.bind_host, port=cfg.http_port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

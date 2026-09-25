"""Правка конфига supervisor'а на лету, без второй копии YAML."""

import copy
import pathlib

import yaml

SCAN_WATCHDOG = "scan_rate"


def without_scan_topic(cfg: dict, topic: str) -> dict:
    """Вернуть копию конфига, где watchdog scan_rate не следит за topic.

    Нужен для режима с одним лидаром: watchdog scan_rate входит в предусловия группы safety,
    и пока правый /scan_right молчит, supervisor не поднимет ни одну группу.
    """
    out = copy.deepcopy(cfg)
    for watchdog in out.get("supervisor", {}).get("watchdogs", []):
        if watchdog.get("name") == SCAN_WATCHDOG:
            params = watchdog.setdefault("params", {})
            params["topics"] = [t for t in params.get("topics", []) if t != topic]
            if not params["topics"]:
                raise ValueError(f"{SCAN_WATCHDOG}: после удаления {topic} не осталось топиков")
            return out
    raise ValueError(f"в конфиге нет watchdog {SCAN_WATCHDOG!r}")


def write_config_without_scan_topic(src: str, dst: str, topic: str) -> str:
    """Записать копию src без topic в scan_rate в dst и вернуть путь dst."""
    cfg = yaml.safe_load(pathlib.Path(src).read_text())
    patched = without_scan_topic(cfg, topic)
    pathlib.Path(dst).write_text(yaml.safe_dump(patched, sort_keys=False, allow_unicode=True))
    return dst

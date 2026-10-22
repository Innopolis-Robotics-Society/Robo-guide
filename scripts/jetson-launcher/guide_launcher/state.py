"""Текущий тур и его настройки: state_dir/state.json переживает перезапуск ROS и launcher."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

TOUR_ID_RE = re.compile(r"^[\w-]{1,64}$")


def valid_tour_id(value: object) -> bool:
    """tour_id -- строка из букв/цифр/`_`/`-` длиной 1..64."""
    return isinstance(value, str) and TOUR_ID_RE.fullmatch(value) is not None


class TourState:
    """Хранит id текущего тура (иначе default_tour) и флаг «Идём дальше?» (по умолчанию выкл)."""

    def __init__(self, state_dir: str | Path, default_tour: str) -> None:
        """`state_dir` создаётся при первой записи."""
        self._path = Path(state_dir).expanduser() / "state.json"
        self._default = default_tour

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, **changes: Any) -> None:
        """Обновить поля атомарно: tmp-файл рядом + os.replace."""
        data = {**self._read(), **changes}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self._path)

    def current(self) -> tuple[str, str]:
        """(tour_id, source): source = "state" или "default"."""
        tour_id = self._read().get("tour_id")
        if valid_tour_id(tour_id):
            return tour_id, "state"
        return self._default, "default"

    def set(self, tour_id: str) -> None:
        """Сделать тур текущим."""
        self._write(tour_id=tour_id)

    def confirm_between_stops(self) -> bool:
        """Спрашивать «Идём дальше?» между остановками; по умолчанию нет."""
        return self._read().get("confirm_between_stops") is True

    def set_confirm_between_stops(self, enabled: bool) -> None:
        """Включить/выключить вопрос «Идём дальше?»."""
        self._write(confirm_between_stops=enabled)

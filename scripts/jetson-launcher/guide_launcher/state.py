"""Текущий тур: state_dir/state.json переживает перезапуск ROS и launcher."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

TOUR_ID_RE = re.compile(r"^[\w-]{1,64}$")


def valid_tour_id(value: object) -> bool:
    """tour_id -- строка из букв/цифр/`_`/`-` длиной 1..64."""
    return isinstance(value, str) and TOUR_ID_RE.fullmatch(value) is not None


class TourState:
    """Хранит id текущего тура; если не задан или файл битый -- берёт default_tour."""

    def __init__(self, state_dir: str | Path, default_tour: str) -> None:
        """`state_dir` создаётся при первой записи."""
        self._path = Path(state_dir).expanduser() / "state.json"
        self._default = default_tour

    def current(self) -> tuple[str, str]:
        """(tour_id, source): source = "state" или "default"."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._default, "default"
        tour_id = data.get("tour_id") if isinstance(data, dict) else None
        if valid_tour_id(tour_id):
            return tour_id, "state"
        return self._default, "default"

    def set(self, tour_id: str) -> None:
        """Записать атомарно: tmp-файл рядом + os.replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"tour_id": tour_id}), encoding="utf-8")
        os.replace(tmp, self._path)

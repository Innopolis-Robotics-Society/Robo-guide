"""Учёт произнесённого по чанкам одного нарратива (design §3.3).

Чанки здесь -- не результат собственного разбиения текста: narration_server
не чанкует (guide_robot_mission_design.md §0.5, §4.1). Один чанк -- один
элемент GetExhibitContent.chunks, уже написанный и провалидированный
ревьюером (guide_robot_semantic_map/lib/content_io.py); один чанк -- один
Say goal. Более мелкое деление на клаузы для потоковой отмены живёт внутри
tts_node и наружу не просачивается: spoken_text/spoken_chars из Say.result
уже посчитаны по границам клауз, ChunkPlan их только агрегирует.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from guide_robot_mission_control.resume import ResumeToken

__all__ = ["ChunkPlan", "ChunkState"]


class ChunkState(IntEnum):
    """Состояние одного чанка внутри плана."""

    PENDING = 0    # ещё не отправлен в Say
    SENT = 1        # goal принят, но озвучка не началась (lookahead)
    SPEAKING = 2     # пришёл started (SpeakingStatus.speaking для этого goal_id)
    DONE = 3          # Say result COMPLETED
    CUT = 4            # Say result отменён/оборван, spoken_chars < len(text)
    SKIPPED = 5         # resume_policy=continue_next признал чанк потерянным;
                        # не спутать с DONE -- в spoken_text() и chunks_spoken()
                        # не учитывается, хотя для is_complete()/resume_token()
                        # уже "закрыт" и повторной отправки не требует.


@dataclass
class _ChunkRecord:
    text: str
    state: ChunkState = ChunkState.PENDING
    spoken_chars: int = 0


class ChunkPlan:
    """Прогресс одного Narrate goal по элементам GetExhibitContent.chunks."""

    def __init__(  # noqa: PLR0913 -- один конструктор значения, не команда с побочными эффектами
        self,
        exhibit_id: str,
        version: str,
        chunks: list[str],
        start_idx: int = 0,
        skipped: set[int] | frozenset[int] | None = None,
        lookahead: int = 1,
    ) -> None:
        """Построить план.

        Чанки [0, start_idx) считаются уже закрытыми предыдущими Narrate
        goal-ами этой же сессии (resume): DONE, кроме индексов из `skipped`
        -- тех, что resume_policy=continue_next признал потерянными за всю
        сессию, а не только на последнем прерывании. Это обязан быть
        накопленный набор: каждый новый ChunkPlan строится с нуля по полному
        списку chunks, никакой памяти о предыдущих экземплярах у него нет,
        поэтому забыть один из ранее пропущенных индексов означает заново
        пометить его DONE и приписать ему текст, которого никто не говорил.
        """
        if not chunks:
            raise ValueError("chunks должен быть непустым")
        if not 0 <= start_idx <= len(chunks):
            raise ValueError(f"start_idx={start_idx} вне диапазона [0, {len(chunks)}]")
        skipped = skipped or frozenset()
        for idx in skipped:
            if not 0 <= idx < start_idx:
                msg = f"skipped idx={idx} должен быть внутри [0, start_idx={start_idx})"
                raise ValueError(msg)
        self.exhibit_id = exhibit_id
        self.version = version
        self.lookahead = lookahead
        self._records = [_ChunkRecord(text=text) for text in chunks]
        for idx, record in enumerate(self._records[:start_idx]):
            if idx in skipped:
                record.state = ChunkState.SKIPPED
            else:
                record.state = ChunkState.DONE
                record.spoken_chars = len(record.text)

    @property
    def chunk_total(self) -> int:
        """Общее число чанков в плане."""
        return len(self._records)

    def chunk_text(self, idx: int) -> str:
        """Исходный текст чанка idx (для отправки в Say)."""
        return self._records[idx].text

    def state_of(self, idx: int) -> ChunkState:
        """Текущее состояние чанка idx."""
        return self._records[idx].state

    def next_to_send(self) -> int | None:
        """Вернуть первый PENDING чанк, если в полёте (SENT+SPEAKING) <= lookahead, иначе None."""
        inflight_states = (ChunkState.SENT, ChunkState.SPEAKING)
        inflight = sum(1 for r in self._records if r.state in inflight_states)
        if inflight > self.lookahead:
            return None
        for idx, record in enumerate(self._records):
            if record.state == ChunkState.PENDING:
                return idx
        return None

    def mark(self, idx: int, state: ChunkState, spoken_chars: int = 0) -> None:
        """Записать новое состояние чанка idx. spoken_chars значим только для DONE/CUT."""
        record = self._records[idx]
        record.state = state
        if state == ChunkState.DONE:
            record.spoken_chars = len(record.text)
        elif state == ChunkState.CUT:
            record.spoken_chars = min(spoken_chars, len(record.text))

    def resume_token(self) -> str:
        """Токен для следующего вызова: первый не-DONE чанк, либо конец, если всё DONE.

        Намеренно не пустая строка, когда план завершён: пустой resume_token
        по грамматике §3.1 значит "с начала", а chunk_idx == chunk_total --
        "уже закончено" (resolve_resume, шаг 3). Смешивать эти два случая
        нельзя, иначе завершённый нарратив при повторном Narrate стартует
        заново вместо немедленного COMPLETED.
        """
        for idx, record in enumerate(self._records):
            if record.state not in (ChunkState.DONE, ChunkState.SKIPPED):
                char_off = record.spoken_chars if record.state == ChunkState.CUT else 0
                return ResumeToken(self.exhibit_id, self.version, idx, char_off).format()
        return ResumeToken(self.exhibit_id, self.version, len(self._records), 0).format()

    def spoken_text(self) -> str:
        """Конкатенация DONE-чанков целиком и префикса CUT-чанков."""
        parts: list[str] = []
        for record in self._records:
            if record.state == ChunkState.DONE:
                parts.append(record.text)
            elif record.state == ChunkState.CUT:
                parts.append(record.text[: record.spoken_chars])
        return " ".join(part for part in parts if part)

    def chunks_spoken(self) -> int:
        """Число полностью произнесённых (DONE) чанков -- для Narrate.result.chunks_spoken."""
        return sum(1 for r in self._records if r.state == ChunkState.DONE)

    def progress(self) -> float:
        """Доля произнесённого текста по символам, [0..1]."""
        total = sum(len(r.text) for r in self._records)
        if total == 0:
            return 0.0

        def _spoken_len(record: _ChunkRecord) -> int:
            if record.state == ChunkState.DONE:
                return len(record.text)
            if record.state == ChunkState.CUT:
                return record.spoken_chars
            return 0

        return sum(_spoken_len(r) for r in self._records) / total

    def is_complete(self) -> bool:
        """Вернуть True, если по всем чанкам решение принято (DONE или SKIPPED)."""
        return all(r.state in (ChunkState.DONE, ChunkState.SKIPPED) for r in self._records)

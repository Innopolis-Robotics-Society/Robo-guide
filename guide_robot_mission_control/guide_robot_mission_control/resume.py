"""Грамматика resume_token и решение, откуда продолжать нарратив (design §3.1-3.2).

Чистые функции, без ROS -- намеренно: и narration_server, и mission_fsm,
и test_narration_resume.py должны получать один и тот же ответ на вопрос
"откуда продолжать", а единственный способ гарантировать это -- не давать
логике решения жить внутри узла.

Формат: v1|<exhibit_id>|<version>|<chunk_idx>|<char_off>.

  exhibit_id -- ключ контента (GetExhibitContent.exhibit_id).
  version    -- GetExhibitContent.version; несовпадение значит "контент
                переиздан, старые офсеты не валидны".
  chunk_idx  -- индекс первого не завершённого полностью чанка
                (ChunkPlan.resume_token()).
  char_off   -- сколько символов chunk_idx реально прозвучало
                (Say.result.spoken_chars). Используется только для отчёта:
                при resume_policy=repeat_chunk (дефолт) точка старта -- всегда
                начало chunk_idx целиком, не char_off.

Пустая строка -- отдельный валидный случай "с начала", а не ошибка формата.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto

__all__ = [
    "ResumeDecision",
    "ResumeOutcome",
    "ResumePolicy",
    "ResumeToken",
    "TokenError",
    "apply_resume_policy",
    "resolve_resume",
]

_VERSION_TAG = "v1"
_FIELD_COUNT = 5


class TokenError(ValueError):
    """resume_token не соответствует грамматике v1|...."""


@dataclass(frozen=True)
class ResumeToken:
    """Разобранный resume_token."""

    exhibit_id: str
    version: str
    chunk_idx: int
    char_off: int

    def format(self) -> str:
        """Собрать обратно в строку грамматики."""
        return f"{_VERSION_TAG}|{self.exhibit_id}|{self.version}|{self.chunk_idx}|{self.char_off}"

    @staticmethod
    def parse(raw: str) -> ResumeToken | None:
        """Разобрать строку. Пустая строка -> None ("с начала", не ошибка)."""
        if raw == "":
            return None
        parts = raw.split("|")
        if len(parts) != _FIELD_COUNT or parts[0] != _VERSION_TAG:
            raise TokenError(f"битый resume_token: {raw!r}")
        _, exhibit_id, version, chunk_idx_s, char_off_s = parts
        if not exhibit_id or not version:
            raise TokenError(f"битый resume_token: {raw!r}")
        try:
            chunk_idx = int(chunk_idx_s)
            char_off = int(char_off_s)
        except ValueError as error:
            raise TokenError(f"битый resume_token: {raw!r}") from error
        if chunk_idx < 0 or char_off < 0:
            raise TokenError(f"битый resume_token: {raw!r}")
        return ResumeToken(
            exhibit_id=exhibit_id, version=version, chunk_idx=chunk_idx, char_off=char_off
        )


class ResumeOutcome(Enum):
    """Куда ведёт валидация resume_token (design §3.1, шаги 1-3)."""

    START = auto()             # пустой токен, либо version разошлась -- начать с 0
    RESUME = auto()             # продолжить с token.chunk_idx
    ALREADY_COMPLETE = auto()   # chunk_idx за концом -- нарратив уже закончен
    REJECTED = auto()            # чужой exhibit_id -- звонок не туда


@dataclass(frozen=True)
class ResumeDecision:
    """Результат resolve_resume()/apply_resume_policy(): что делать narration_server-у."""

    outcome: ResumeOutcome
    start_chunk_idx: int
    char_off: int
    detail: str
    skipped_chunk_idx: int | None = None
    """Индекс чанка, признанного потерянным (только continue_next). ChunkPlan
    обязан пометить его ChunkState.SKIPPED, а не ChunkState.DONE -- иначе
    spoken_text() соврёт, что чанк прозвучал целиком."""


def resolve_resume(
    token: ResumeToken | None, *, exhibit_id: str, version: str, chunk_count: int
) -> ResumeDecision:
    """Применить шаги 1-3 из design §3.1 к уже разобранному токену."""
    if token is None:
        return ResumeDecision(ResumeOutcome.START, 0, 0, "")
    if token.exhibit_id != exhibit_id:
        detail = f"resume_token для {token.exhibit_id!r}, ожидался {exhibit_id!r}"
        return ResumeDecision(ResumeOutcome.REJECTED, 0, 0, detail)
    if token.version != version:
        return ResumeDecision(ResumeOutcome.START, 0, 0, "content_rev_changed")
    if token.chunk_idx >= chunk_count:
        return ResumeDecision(ResumeOutcome.ALREADY_COMPLETE, chunk_count, 0, "")
    return ResumeDecision(ResumeOutcome.RESUME, token.chunk_idx, token.char_off, "")


class ResumePolicy(str, Enum):
    """`narration_server.resume_policy` (config/mission.yaml, design §3.2)."""

    REPEAT_CHUNK = "repeat_chunk"
    CONTINUE_NEXT = "continue_next"
    OVERLAP_1 = "overlap_1"


def apply_resume_policy(
    decision: ResumeDecision, policy: ResumePolicy, *, chunk_count: int
) -> ResumeDecision:
    """Переопределить точку старта у RESUME-решения по политике (design §3.2).

    Не трогает START/ALREADY_COMPLETE/REJECTED -- политика имеет смысл только
    когда есть реальный прерванный чанк, с которого можно решать, начинать
    ли заново, пропускать или перекрывать.
    """
    if decision.outcome is not ResumeOutcome.RESUME:
        return decision
    if policy is ResumePolicy.CONTINUE_NEXT:
        lost_idx = decision.start_chunk_idx
        next_idx = lost_idx + 1
        if next_idx >= chunk_count:
            return ResumeDecision(
                ResumeOutcome.ALREADY_COMPLETE, chunk_count, 0, "", skipped_chunk_idx=lost_idx
            )
        return replace(decision, start_chunk_idx=next_idx, char_off=0, skipped_chunk_idx=lost_idx)
    if policy is ResumePolicy.OVERLAP_1:
        return replace(decision, start_chunk_idx=max(0, decision.start_chunk_idx - 1), char_off=0)
    # repeat_chunk (дефолт): точка старта -- начало chunk_idx целиком; char_off
    # из resolve_resume не трогаем, он остаётся только для отчётности.
    return decision

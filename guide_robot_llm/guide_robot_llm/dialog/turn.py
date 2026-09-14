"""Ход dialog_agent: действие (фаза 1, GBNF, контракт ADR-0001) -> исполнение -> реплика (фаза 2).

Порядок фаз ИНВЕРТИРОВАН против DIALOG_REWORK_PLAN.md §0/§5 (живой баг:
реплика «отвожу вас к кафе» + действие noop в том же ходу): сначала модель
под GBNF выдаёт действие контракта -- `{"tool", "args", "confidence",
"abstain"}` (ровно 4 поля, `think` из контракта УБРАН, ADR-0001 §3) --
детерминированный валидатор (`tools.validate.verify_action`) решает,
допускать ли его в `tool_broker` (abstain модели, confidence ниже порога,
недоступный инструмент, чужие id -- никуда не исполняются), затем
инструмент исполняется, и только потом генерируется свободная реплика,
которая видит действие и его РЕАЛЬНЫЙ итог. Воздержание (abstention)
заканчивается safe fallback: короткая реплика-уточнение, никогда не
мутирующее действие (ADR-0001 §4).

Чистая логика без rclpy -- backend'ы, `speak`/`execute_tool` инжектируются
как callable, тестируется на фейках без ROS и без HTTP
(`dialog_agent_node.py` -- единственный потребитель из rclpy-контекста).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from guide_robot_llm.dialog.sanitize import sanitize_answer
from guide_robot_llm.llm_client import CompletionResult, build_action_grammar, build_content
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError
from guide_robot_llm.matching import looks_like_chit_chat, match_start_tour
from guide_robot_llm.pointing import (
    STATUS_AMBIGUOUS,
    STATUS_NO_CANDIDATE,
    STATUS_RESOLVED,
    STATUS_UNUSABLE_FRAMES,
    PointingBaseContext,
    PointingContext,
    PointingEvidence,
    resolve_pointing,
)
from guide_robot_llm.tools.validate import (
    MOTION_TOOLS,
    REASON_ABSTAIN_FROM_MODEL,
    REASON_AMBIGUOUS_TARGET,
    REASON_LOW_CONFIDENCE,
    REASON_NO_CANDIDATE,
    REASON_STALE_FRAMES,
    parse_action,
    verify_action,
)
from guide_robot_llm.visual_context import (
    POINTING_YES,
    Observation,
    ObservationRequest,
    parse_observation,
    render_observation,
)

# Taiga #7: статус pointing.resolve_pointing -> код причины (validate.REASONS).
# Все статусы кроме `resolved` означают «не исполняем, уточняем»: действие
# формально валидно (catalog/состояние/аргументы прошли), но визуальный ввод
# не позволяет уверенно выбрать экспонат.
_POINTING_STATUS_TO_REASON = {
    STATUS_UNUSABLE_FRAMES: REASON_STALE_FRAMES,
    STATUS_NO_CANDIDATE: REASON_NO_CANDIDATE,
    STATUS_AMBIGUOUS: REASON_AMBIGUOUS_TARGET,
}

__all__ = [
    "ToolCallRecord",
    "ToolResultLike",
    "TurnResult",
    "render_action_outcome",
    "run_answer_phase",
    "run_turn",
]

# Repair-инструкция на malformed-вывод: форма контракта повторяется
# дословно, чтобы модель не «докладывала» про формат.
_MALFORMED_REPAIR_HINT = (
    "Предыдущий ответ не соответствует контракту. Ответь ТОЛЬКО одним "
    'JSON-объектом {"tool": "<имя>", "args": {...}, '
    '"confidence": <число 0..1>, "abstain": true|false} -- ровно эти 4 '
    "поля, без чужих ключей и без текста до или после JSON."
)


class ToolResultLike(Protocol):
    """Поля результата вызова инструмента (см. `tool_broker_node.ToolResult`)."""

    ok: bool
    message: str
    data: dict


@dataclass
class ToolCallRecord:
    """Действие, выбранное фазой действия, и его исход (включая `reply`).

    `think` -- диагностическая пометка (код причины / override), не поле
    модели: из контракта `think` убран (ADR-0001 §3).
    """

    name: str
    args: dict
    result_ok: bool
    result_message: str
    result_data: dict
    think: str = ""
    read_only: bool = False


@dataclass
class TurnResult:
    """Итог одного хода «действие -> реплика».

    `messages` -- ПОЛНЫЙ накопленный диалог с ЛЛМ на момент окончания хода:
    system prompt, история, реплика посетителя, сырой tool-call текст на
    каждой попытке починки, инструкция и текст фазы реплики. Единственный
    источник, где виден буквально весь обмен с моделью за ход
    (`interaction_log` кладёт его в запись как есть).

    `answer_raw_text`/`action_raw_text` -- то, что модель выдала ДО
    постобработки (`sanitize_answer` для реплики, `parse_action` для
    действия). `action_raw_text` -- текст ПОСЛЕДНЕЙ попытки фазы действия
    (включая случай `action_parse_error`, когда `action` остаётся `None`,
    но модель что-то всё-таки прислала). `*_finish_reason` -- как сервер
    объяснил остановку генерации, пусто, если бэкенд не ответил вовсе.

    `action_first_attempt_valid` / `action_reason_code` -- метрики
    валидности контракта (ADR-0001 §Research evidence): была ли ПЕРВАЯ
    попытка схемы-валидна и какой код причины закрыл ход (пусто = обычный
    `ok`, либо код из `tools.validate.REASONS` при safe fallback).
    Измеряются раздельно: raw-валидность (первая попытка) и repaired
    (исход после починки, `repair_used`).

    `observation_raw_text`/`observation_text`/`observation_error` -- фаза
    наблюдения (Taiga #4, observe_then_decide): сырой вывод модели, его
    отрендеренный (host-отфильтрованный) вариант и причина деградации
    (`""` = фаза не прогонялась либо прошла чисто; `malformed` /
    `backend_error`). Наблюдение -- side-channel: его провал НИКОГДА не
    рвёт ход, фаза действия идёт без него.

    Семантика `stopped_reason` при инвертированном порядке фаз:
    `action_backend_error` -- бэкенд упал на ПЕРВОЙ фазе, не произошло
    вообще ничего (ни действия, ни речи); `answer_backend_error` -- действие
    могло уже ИСПОЛНИТЬСЯ, но реплика не сгенерировалась и ничего не
    сказано; `action_parse_error` -- malformed-вывод действия и починка
    исчерпана, ход обрывается до исполнения и до речи; `action_invalid` --
    инструмент исполнён с ошибкой и починка исчерпана (реплика при этом
    генерируется и честно сообщает о провале); `aborted` --
    barge-in/новая реплика посетителя.
    """

    messages: list[dict] = field(default_factory=list)
    answer_text: str = ""
    answer_raw_text: str = ""
    answer_finish_reason: str = ""
    action_raw_text: str = ""
    action_finish_reason: str = ""
    say_ok: bool = False
    # stage3 C2: STATUS_PREEMPTED -- законный барж-ин, не сбой (say_ok
    # остаётся True), но история/фаза реплики следующего хода обязаны
    # знать, что реплику НЕ дослушали -- см. `dialog_agent_node._run_turn`.
    say_preempted: bool = False
    action: ToolCallRecord | None = None
    repair_used: bool = False
    # Код причины safe fallback (abstain/low_confidence/исчерпанная
    # починка) либо "" -- обычный ход.
    action_reason_code: str = ""
    # True, если ПЕРВАЯ попытка фазы действия была схема-валидной.
    action_first_attempt_valid: bool = False
    action_confidence: float | None = None
    action_abstain: bool | None = None
    # Фаза наблюдения (Taiga #4): сырой вывод / рендер для промпта /
    # причина деградации (см. docstring).
    observation_raw_text: str = ""
    observation_text: str = ""
    observation_error: str = ""
    # "ok" | "answer_backend_error" | "action_backend_error"
    # | "action_parse_error" | "action_invalid" | "aborted"
    stopped_reason: str = "ok"


def render_action_outcome(record: ToolCallRecord | None) -> str:
    """Отрендерить итог действия для промпта фазы реплики.

    Для read_only-инструментов (`lookup_content`/`search_content`/
    `resolve_location`/`describe_scene`/старый read-only каталог) -- полный
    найденный текст (`chunks`/`hits`/`candidates`/`visual_context`), а не
    `выполнено: name(...)` (CLAUDE_CODE_TASK_stage1_knowledge.md п.7.2): фаза реплики обязана
    видеть сами факты, не только имя вызова. Для остальных (мутирующих)
    инструментов -- прежняя короткая строка; та же строка используется
    `dialog_agent_node._action_event_text` как событие истории, поэтому
    она обязана совпадать побайтово в обоих местах -- в отличие от
    read_only, где история короче найденного текста (см. `_action_event_text`).
    """
    if record is None or record.name == "reply":
        return (
            "Действий не требуется — просто ответь посетителю на его "
            "последнюю реплику, как живой собеседник."
        )
    if record.name == "ask_visitor":
        # stage2 C2: фаза реплики обязана озвучить сам вопрос -- "выполнена"
        # для ask_visitor значит "вопрос принят", а не "уже что-то сделано".
        if not record.result_ok:
            return f"не удалось: ask_visitor — {record.result_message}"
        return f"задай вопрос: {record.args.get('question', '')}"
    if record.read_only:
        if not record.result_ok:
            return f"не удалось: {record.name} — {record.result_message}"
        return _render_read_only_result(record.result_data)
    args_str = ", ".join(f"{key}={value!r}" for key, value in record.args.items())
    call = f"{record.name}({args_str})"
    if record.result_ok:
        return f"выполнено: {call}"
    return f"не удалось: {call} — {record.result_message}"


def _render_read_only_result(data: dict) -> str:
    """Полный текст, найденный read_only-инструментом -- без обрезки по длине.

    Порядок проверок -- по форме `data`, не по имени инструмента: `dialog/
    turn.py` не знает имён инструментов сверх того, что пришло в записи.
    """
    if "chunks" in data:
        if not data["chunks"]:
            return "ничего не найдено"
        title = data.get("title", "")
        prefix = f"{title}: " if title else ""
        return prefix + " ".join(data["chunks"])
    if "hits" in data:
        if not data["hits"]:
            return "ничего не найдено"
        return " ".join(
            f"[{hit.get('kind', '')}: {hit.get('title', '')}] {hit.get('text', '')}"
            for hit in data["hits"]
        )
    if "candidates" in data:
        if not data["candidates"]:
            return "локация не найдена"
        return "возможные локации: " + ", ".join(
            candidate.get("id", "") for candidate in data["candidates"]
        )
    if "visual_context" in data:
        visual_context = data.get("visual_context", "").strip()
        if not visual_context:
            if data.get("quality") == "none":
                return "не удалось: describe_scene — нет замороженных кадров"
            return "визуальный контекст недоступен"
        parts = [visual_context]
        quality = data.get("quality", "")
        if quality and quality != "ok":
            parts.append(f"качество кадров: {quality}")
        candidates = data.get("exhibit_candidates", ())
        if candidates:
            parts.append("видимые экспонаты: " + ", ".join(candidates))
        # Taiga #6: observation_instruction построен в _tool_describe_scene
        # (dialog_agent_node) и обязан дойти до фазы реплики, иначе это
        # мёртвые данные (раньше здесь терялся).
        instruction = str(data.get("observation_instruction", "")).strip()
        if instruction:
            parts.append(instruction)
        return " ".join(parts)
    return "готово"


def run_turn(
    *,
    system_prompt: str,
    history_messages: list[dict],
    user_content: str,
    complete_answer: Callable[..., CompletionResult],
    complete_action: Callable[..., CompletionResult],
    speak: Callable[[str], ToolResultLike],
    execute_tool: Callable[[str, dict], ToolResultLike],
    tool_names: Sequence[str],
    action_instruction: str,
    answer_instruction: str,
    repair_attempts: int = 1,
    check_aborted: Callable[[], bool] = lambda: False,
    answer_max_chars: int = 400,
    read_only_tools: frozenset[str] = frozenset(),
    on_action_resolved: Callable[[ToolCallRecord], None] | None = None,
    utterance: str = "",
    default_tour_id: str = "",
    confidence_threshold: float = 0.5,
    known_location_ids: frozenset[str] = frozenset(),
    known_tour_ids: frozenset[str] = frozenset(),
    # Таiga #7: каталог id публичных экспонатов для валидации resolve_pointing
    # (content_id вне каталога -> unknown_id до брокера).
    known_exhibit_ids: frozenset[str] = frozenset(),
    # Таiga #4: визуальный контекст хода (см. docstring ниже).
    action_frames: Sequence[str] = (),
    visual_suffix: str = "",
    observation_request: ObservationRequest | None = None,
    complete_observation: Callable[..., CompletionResult] | None = None,
    answer_frames: Sequence[str] = (),
    answer_phase_images: bool = False,
    # Таiga #7: замороженный контекст жеста-указания (кандидаты видимой карты,
    # поза робота, геометрия камеры, указание, реплика, качество кадров).
    # `None` = визуального контекста хода нет -- resolve_pointing невозможен.
    pointing_base: PointingBaseContext | None = None,
) -> TurnResult:
    """Прогнать один ход диалога (контракт действия ADR-0001).

    `on_action_resolved` (если задан) зовётся с финальным `record` СРАЗУ
    после исполнения действия, ДО фазы реплики и её `speak()` -- вызывающий
    обязан успеть закоммитить следствия хода (слот `ask_visitor`,
    грейс-таймер и т.п.) ДО того, как посетитель услышит первый звук.
    Иначе окно между стартом озвучки и фиксацией состояния (миллисекунды)
    позволяет барж-ин поверх ещё звучащей реплики попасть в состояние ДО
    того, как оно обновилось -- то же семейство гонки, что и в
    `_run_turn`'s истории/грейс-таймере.

    `complete_answer`/`complete_action` уже связаны вызывающим
    (`dialog_agent_node.py`) с конкретными бэкендами/`abort_event`/
    max_tokens/temperature/грамматикой (только `complete_action` идёт под
    грамматикой). `BackendAborted` (barge-in) НЕ перехватывается ни в одной
    из фаз -- пробрасывается наружу как есть, вызывающий поток обязан
    отличить намеренное прерывание от реального отказа бэкенда. Остальные
    `BackendError` (timeout/HTTP/сеть) -- оборванный ход здесь штатный
    случай деградации, не повод поднимать исключение вызывающему.

    `check_aborted()` зовётся в двух точках: перед `execute_tool()` (barge-in
    не должен исполнить уже неактуальное действие) и перед `speak()` (если
    посетитель успел отменить до того, как робот начал говорить, начинать
    нельзя) -- в обоих случаях ход останавливается со
    `stopped_reason="aborted"`.

    Валидатор (ADR-0001 §2, `tools.validate.verify_action`) стоит МЕЖДУ
    выходом модели и брокером: malformed-вывод (chужие ключи, NaN,
    out-of-range confidence, хвост после JSON) и отклонённые действия
    (недоступный инструмент, чужие id, кривые аргументы) чинятся repair-
    попытками и НИКОГДА не доходят до `execute_tool`. Явное
    `abstain=true` и confidence ниже `confidence_threshold` -- без
    починки сразу safe fallback: ни один инструмент не исполняется
    (включая моторные), реплика -- короткое уточнение/неопределённость.
    `confidence` никогда не используется как доверие для авторизации:
    инструмент исполняется только если каталог, состояние миссии
    (`tool_names`) и аргументы все прошли. Починка происходит ДО фазы
    реплики -- посетитель её не слышит и не замечает.

    `read_only_tools` -- имена инструментов, чей результат `render_action_
    outcome()` показывает фазе реплики целиком (`chunks`/`hits`/
    `candidates`), а не короткой строкой `выполнено: name(...)`
    (`tools.schema.ToolSpec.read_only`, CLAUDE_CODE_TASK_stage1_knowledge.md
    п.7.2). Пустой набор по умолчанию -- вызывающий код без каталога
    инструментов (тесты на голых фейках) не обязан его знать.

    `utterance` (stage5 п.2) -- голый текст реплики посетителя (после ASR,
    без обрезки, тот же, что ушёл в `user_content`), приклеивается фазе
    реплики РЯДОМ с местом генерации, а не только в статусной строке фазы
    1 -- живой баг: без этого якоря фаза 2 в `ANSWERING` видела только
    «действий не требуется» и хвост собственных прошлых ответов, отвечала
    на позапрошлый вопрос вместо последнего. Пусто по умолчанию -- фейковые
    тесты без реального утторанса не обязаны его знать.

    `confidence_threshold`/`known_location_ids`/`known_tour_ids` --
    параметры валидатора (ADR-0001 §5): порог safe abstention и живые
    каталоги id (пустой каталог = членство не проверяется, как в
    `tools.validate.validate_call`).

    Таига #4 (визуальный контекст): `action_frames` -- data-URL'ы замороженных
    кадров, прикрепляются к волатильному сообщению визуального контекста фазы
    действия (`build_content`: без кадров сообщение остаётся строкой и ход
    байт-в-байт прежний). `visual_suffix` -- волатильный текст хода
    (`visual_context.render_visual_context`: реплика, метаданные кадров БЕЗ
    base64, кандидаты-экспонаты). `observation_request` + `complete_observation`
    -- фаза наблюдения observe_then_decide: ПЕРЕД фазой действия модель под
    GBNF-грамматикой выдаёт структурированное наблюдение (люди/экспонаты/
    жест/факты), которое host режет (id вне кандидатов выбрасываются) и
    рендерит в `observation_text` -- его добавляет к волатильному сообщению
    после `visual_suffix`. Наблюдение -- side-channel: `BackendError`/
    malformed-вывод НЕ рвёт ход (метрика `observation_error`), фаза действия
    идёт без наблюдения. `answer_frames` + `answer_phase_images` -- кадры в
    фазе реплики: прикрепляются только если флаг включен И выбранное действие
    не `reply` (выбирающего skill'а у reply нет, будущие визуальные skill'ы
    #6/#7 определят своё).

    Таига #7 (resolve_pointing): `pointing_base` -- статический базис
    контекста жеста, замороженный на границе хода (`dialog_agent_node`):
    кандидаты видимой карты с координатами, поза робота, геометрия камеры,
    реплика, качество кадров. Динамическая часть -- сам жест (pointing_box)
    и реально видимые id -- сообщает наблюдение VLM, которое считается
    ВНУТРИ run_turn ДО фазы действия; host складывает базис и наблюдение в
    полный `PointingContext` один раз (наблюдение не прогоняется дважды --
    оно же рендерится в промпт). Когда фаза действия выбирает
    `resolve_pointing` и действие прошло валидатор (content_id из каталога),
    host ДО `execute_tool` детерминированно сверяет выбор с геометрией
    (`pointing.resolve_pointing`): неоднозначно / нет правдоподобных / кадры
    устарели -> safe abstention с кодом качества ввода (stale_frames /
    no_candidate / ambiguous_target), инструмент НЕ исполняется, фаза
    реплики уточняет у посетителя. `pointing_base is None` (нет кадров/позы)
    -- resolve_pointing невозможен: тот же safe fallback со `stale_frames`.
    Чистая логика -- бэкенды и базис инжектируются, ROS здесь не нужен.
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        *history_messages,
        {"role": "user", "content": user_content},
        {"role": "user", "content": action_instruction},
    ]

    grammar = build_action_grammar(list(tool_names))
    repair_used = False
    attempts_left = repair_attempts
    record: ToolCallRecord | None = None
    action_raw_text = ""
    action_finish_reason = ""
    action_reason_code = ""
    action_confidence: float | None = None
    action_abstain: bool | None = None
    first_attempt_valid = False

    # Таига #4: фаза наблюдения ПЕРЕД фазой действия (observe_then_decide).
    # Только когда есть и запрос, и бэкенд, и кадры (без кадров наблюдать
    # нечего -- text-only вариант стратегии). `parsed_observation` вынесен на
    # уровень функции: кроме рендера в промпт он нужен геометрическому гейту
    # resolve_pointing (Taiga #7) -- жест/бокс и видимые id.
    observation_raw_text = ""
    observation_text = ""
    observation_error = ""
    parsed_observation: Observation | None = None
    if (
        observation_request is not None
        and complete_observation is not None
        and observation_request.frames
    ):
        observation_messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
            {"role": "user", "content": observation_request.instruction},
            {
                "role": "user",
                "content": build_content(
                    observation_request.context_text, observation_request.frames
                ),
            },
        ]

        def _observation_stop_when(text: str) -> bool:
            return (
                parse_observation(
                    text,
                    candidate_ids=observation_request.candidate_ids,
                    max_chars=observation_request.max_chars,
                )
                is not None
            )

        try:
            observation_completion = complete_observation(
                observation_messages,
                observation_request.grammar,
                stop_when=_observation_stop_when,
            )
        except BackendAborted:
            raise
        except BackendError:
            observation_error = "backend_error"
            observation_completion = None
        if observation_completion is not None:
            observation_raw_text = observation_completion.text
            parsed_observation = parse_observation(
                observation_raw_text,
                candidate_ids=observation_request.candidate_ids,
                max_chars=observation_request.max_chars,
            )
            if parsed_observation is None:
                observation_error = "malformed"
            else:
                observation_text = render_observation(
                    parsed_observation,
                    quality=observation_request.quality,
                    max_chars=observation_request.max_chars,
                )

    # Таига #7: складываем полный PointingContext из статического базиса
    # (pointing_base) и наблюдения (жест/бокс + видимые id). Если наблюдения
    # нет (текстовый ход / наблюдение сломалось) -- жест отсутствует
    # (present=False, box=None), видимых id нет: геометрический гейт даст
    # no_candidate/устарело и уточнит, не угадывая. `pointing_base is None`
    # (нет базиса) -- pointing_context=None, гейт сразу stale_frames.
    pointing_context: PointingContext | None = None
    if pointing_base is not None:
        pointing = PointingEvidence(present=False, box=None)
        visible_ids: frozenset[str] = frozenset()
        if parsed_observation is not None:
            pointing = PointingEvidence(
                present=(parsed_observation.pointing_evidence == POINTING_YES),
                box=parsed_observation.pointing_box,
            )
            visible_ids = frozenset(parsed_observation.exhibit_candidates)
        pointing_context = PointingContext(
            candidates=pointing_base.candidates,
            robot_pose=pointing_base.robot_pose,
            camera=pointing_base.camera,
            pointing=pointing,
            utterance=pointing_base.utterance,
            frame_quality=pointing_base.frame_quality,
            visible_ids=visible_ids,
        )

    # Таига #4: волатильное визуальное сообщение фазы действия -- ПОСЛЕ
    # стабильной инструкции (кэш-префикс не страдает). Пусто -- нет
    # сообщений вообще, ход байт-в-байт прежний (text-only).
    visual_message: dict | None = None
    visual_text_only = ""
    if visual_suffix or action_frames or observation_text:
        visual_parts = [part for part in (visual_suffix, observation_text) if part]
        visual_text_only = "\n\n".join(visual_parts)
        visual_message = {
            "role": "user",
            "content": build_content(visual_text_only, tuple(action_frames)),
        }
        messages = [*messages, visual_message]

    def _abstain_record(reason: str) -> ToolCallRecord:
        # Safe fallback (ADR-0001 §4): действие не исполняется, в запись
        # кладётся reply-заглушка с кодом причины -- фаза реплики по коду
        # переключается на короткое уточнение.
        return ToolCallRecord(
            name="reply",
            args={},
            result_ok=True,
            result_message="",
            result_data={},
            think=reason,
        )

    while True:
        try:
            action_completion = complete_action(messages, grammar, stop_when=_action_stop_when)
        except BackendAborted:
            raise
        except BackendError:
            return TurnResult(
                messages=messages,
                action_raw_text=action_raw_text,
                action_finish_reason=action_finish_reason,
                repair_used=repair_used,
                observation_raw_text=observation_raw_text,
                observation_text=observation_text,
                observation_error=observation_error,
                stopped_reason="action_backend_error",
            )

        action_raw_text = action_completion.text
        action_finish_reason = action_completion.finish_reason
        # Сырой текст попадает в messages ДО проверки парсинга -- иначе
        # `action_parse_error` (модель прислала не ту форму) теряет
        # единственную улику, что она вообще ответила, и чем именно.
        messages = [*messages, {"role": "assistant", "content": action_raw_text}]

        parsed = parse_action(action_raw_text)
        if parsed is None:
            # Malformed-вывод (чужие ключи, NaN, хвост после JSON...):
            # одна-две repair-попытки, затем обрыв хода -- ничего не
            # исполнять и не говорить (см. `action_parse_error`).
            if attempts_left > 0:
                attempts_left -= 1
                repair_used = True
                messages = [*messages, {"role": "user", "content": _MALFORMED_REPAIR_HINT}]
                continue
            return TurnResult(
                messages=messages,
                action_raw_text=action_raw_text,
                action_finish_reason=action_finish_reason,
                repair_used=repair_used,
                action_first_attempt_valid=first_attempt_valid,
                observation_raw_text=observation_raw_text,
                observation_text=observation_text,
                observation_error=observation_error,
                stopped_reason="action_parse_error",
            )
        # Метрика «была ли ПЕРВАЯ попытка схема-валидна»: после repair
        # любая успешная попытка уже не первая.
        if not first_attempt_valid and not repair_used:
            first_attempt_valid = True

        name = parsed.tool
        args = parsed.args
        action_confidence = parsed.confidence
        action_abstain = parsed.abstain
        overridden = False

        # «начни экскурсию» + reply от модели -- детерминированное
        # host-side решение (не действие модели): confidence/abstain
        # модели на reply сюда не распространяются.
        if (
            name == "reply"
            and utterance
            and match_start_tour(utterance)
            and "start_tour" in tool_names
            and default_tour_id
        ):
            name = "start_tour"
            args = {"tour_id": default_tour_id}
            overridden = True

        if not overridden:
            verdict = verify_action(
                parsed,
                tools_allowed=tool_names,
                known_location_ids=known_location_ids,
                known_tour_ids=known_tour_ids,
                known_exhibit_ids=known_exhibit_ids,
                confidence_threshold=confidence_threshold,
            )
            if verdict.reason in (REASON_ABSTAIN_FROM_MODEL, REASON_LOW_CONFIDENCE):
                # Явное воздержание / низкое confidence: без починки
                # (синтаксис не лечит), ни один инструмент не исполняется.
                record = _abstain_record(verdict.reason)
                action_reason_code = verdict.reason
                break
            if not verdict.ok:
                # Illegal state / unknown id / invalid args: чиним ДО
                # брокера (брокер их никогда не видит).
                if attempts_left > 0:
                    attempts_left -= 1
                    repair_used = True
                    messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                f"Действие отклонено: {verdict.message}. "
                                "Выбери другое доступное действие или reply."
                            ),
                        },
                    ]
                    continue
                record = _abstain_record(verdict.reason)
                action_reason_code = verdict.reason
                break

        # Таига #7: resolve_pointing -- host-side сверка выбора модели с
        # геометрией (pointing.resolve_pointing) ДО execute_tool. Action
        # уже прошёл валидатор (content_id из каталога); дальше -- только
        # «видимо ли, однозначно ли, не устарели ли кадры». Любое «нет» --
        # safe abstention с кодом качества ввода, инструмент не исполняется
        # (ADR-0001 §4), фаза реплики уточняет у посетителя.
        if name == "resolve_pointing":
            if pointing_context is None:
                # Нет базиса хода (кадров/позы нет, не заморозить): геометрию
                # сверить невозможно -- не угадываем.
                record = _abstain_record(REASON_STALE_FRAMES)
                action_reason_code = REASON_STALE_FRAMES
                break
            resolution = resolve_pointing(
                pointing_context, model_content_id=args.get("content_id") or ""
            )
            if resolution.status != STATUS_RESOLVED:
                reason = _POINTING_STATUS_TO_REASON[resolution.status]
                record = _abstain_record(reason)
                action_reason_code = reason
                break

        if name == "reply":
            record = ToolCallRecord(
                name=name,
                args=args,
                result_ok=True,
                result_message="",
                result_data={},
            )
            break

        # Модель иногда жмёт guide_to на «привет». Не гоняем моторы.
        if name in MOTION_TOOLS and utterance and looks_like_chit_chat(utterance):
            record = ToolCallRecord(
                name="reply",
                args={},
                result_ok=True,
                result_message="",
                result_data={},
                think=f"override:{name}->reply",
            )
            break

        if check_aborted():
            return TurnResult(
                messages=messages,
                action_raw_text=action_raw_text,
                action_finish_reason=action_finish_reason,
                repair_used=repair_used,
                action_reason_code=action_reason_code,
                action_first_attempt_valid=first_attempt_valid,
                observation_raw_text=observation_raw_text,
                observation_text=observation_text,
                observation_error=observation_error,
                stopped_reason="aborted",
            )

        result = execute_tool(name, args)
        record = ToolCallRecord(
            name=name,
            args=args,
            result_ok=result.ok,
            result_message=result.message,
            result_data=dict(result.data),
            read_only=name in read_only_tools,
        )

        if result.ok or attempts_left <= 0:
            break

        attempts_left -= 1
        repair_used = True
        messages = [*messages, {"role": "user", "content": result.message}]

    if on_action_resolved is not None:
        on_action_resolved(record)

    if (
        visual_message is not None
        and isinstance(visual_message["content"], list)
        and not (answer_phase_images and record.name != "reply")
    ):
        # Контракт фазы реплики (Taiga #4): кадры видит только при
        # `answer_phase_images=true` И action != reply. Наследуемое от
        # фазы действия визуальное сообщение содержит те же кадры --
        # убираем из него image-parts, оставляя текст (кандидаты,
        # наблюдение) -- иначе флаг/исключение reply не имеют силы.
        old_visual_message = visual_message
        visual_message = {"role": "user", "content": visual_text_only}
        messages = [visual_message if m is old_visual_message else m for m in messages]

    return run_answer_phase(
        messages=messages,
        record=record,
        answer_instruction=answer_instruction,
        complete_answer=complete_answer,
        speak=speak,
        check_aborted=check_aborted,
        answer_max_chars=answer_max_chars,
        action_raw_text=action_raw_text,
        action_finish_reason=action_finish_reason,
        repair_used=repair_used,
        utterance=utterance,
        action_reason_code=action_reason_code,
        action_first_attempt_valid=first_attempt_valid,
        action_confidence=action_confidence,
        action_abstain=action_abstain,
        observation_raw_text=observation_raw_text,
        observation_text=observation_text,
        observation_error=observation_error,
        answer_frames=answer_frames,
        answer_phase_images=answer_phase_images,
    )


def run_answer_phase(
    *,
    messages: list[dict],
    record: ToolCallRecord,
    answer_instruction: str,
    complete_answer: Callable[..., CompletionResult],
    speak: Callable[[str], ToolResultLike],
    check_aborted: Callable[[], bool] = lambda: False,
    answer_max_chars: int = 400,
    action_raw_text: str = "",
    action_finish_reason: str = "",
    repair_used: bool = False,
    utterance: str = "",
    action_reason_code: str = "",
    action_confidence: float | None = None,
    action_abstain: bool | None = None,
    action_first_attempt_valid: bool = False,
    observation_raw_text: str = "",
    observation_text: str = "",
    observation_error: str = "",
    answer_frames: Sequence[str] = (),
    answer_phase_images: bool = False,
) -> TurnResult:
    """Фаза реплики: итог действия -> ЛЛМ -> `sanitize` -> один `speak()`.

    Один Say на весь ответ (ранний TTS первого предложения давал разрыв
    между двумя goal). При safe fallback (`action_reason_code` задан)
    итог действия заменяется инструкцией на короткое уточнение --
    мутирующего действия не было, и реплика это обязана отражать.

    Таига #4: `answer_frames` (data-URL'ы тех же замороженных кадров, что
    фаза действия) прикрепляются к сообщению фазы реплики ТОЛЬКО если
    `answer_phase_images` включен И выбранное действие не `reply` --
    «только если выбранный skill' их требует»: у reply визуального
    skill'а нет, будущие #6/#7 определят своё.
    """
    action_stopped_reason = "ok" if record.result_ok else "action_invalid"

    if action_reason_code:
        outcome_line = (
            f"Действие НЕ было исполнено: модель воздержалась "
            f"(причина: {action_reason_code}). Ответь коротким уточняющим "
            "вопросом или признай неопределённость; не утверждай, что "
            "что-то уже сделано."
        )
    else:
        outcome_line = render_action_outcome(record)
    answer_message = f"{answer_instruction}\n\nИтог действия: {outcome_line}"
    if utterance:
        answer_message += f"\n\nРеплика посетителя: «{utterance}»\nОтветь именно на неё."
    answer_image_frames = (
        tuple(answer_frames) if (answer_phase_images and record.name != "reply") else ()
    )
    messages = [
        *messages,
        {"role": "user", "content": build_content(answer_message, answer_image_frames)},
    ]

    try:
        answer_completion = complete_answer(messages)
    except BackendAborted:
        raise
    except BackendError:
        return TurnResult(
            messages=messages,
            action_raw_text=action_raw_text,
            action_finish_reason=action_finish_reason,
            action=record,
            repair_used=repair_used,
            action_reason_code=action_reason_code,
            action_first_attempt_valid=action_first_attempt_valid,
            action_confidence=action_confidence,
            action_abstain=action_abstain,
            observation_raw_text=observation_raw_text,
            observation_text=observation_text,
            observation_error=observation_error,
            stopped_reason="answer_backend_error",
        )

    answer_raw_text = answer_completion.text
    answer_finish_reason = answer_completion.finish_reason
    answer_text = sanitize_answer(answer_raw_text, max_chars=answer_max_chars)
    messages = [*messages, {"role": "assistant", "content": answer_raw_text}]

    say_ok = False
    say_preempted = False
    if answer_text:
        if check_aborted():
            return TurnResult(
                messages=messages,
                answer_text=answer_text,
                answer_raw_text=answer_raw_text,
                answer_finish_reason=answer_finish_reason,
                action_raw_text=action_raw_text,
                action_finish_reason=action_finish_reason,
                action=record,
                repair_used=repair_used,
                action_reason_code=action_reason_code,
                action_first_attempt_valid=action_first_attempt_valid,
                action_confidence=action_confidence,
                action_abstain=action_abstain,
                observation_raw_text=observation_raw_text,
                observation_text=observation_text,
                observation_error=observation_error,
                stopped_reason="aborted",
            )
        say_result = speak(answer_text)
        say_ok = say_result.ok
        say_preempted = bool(say_result.data.get("preempted"))

    return TurnResult(
        messages=messages,
        answer_text=answer_text,
        answer_raw_text=answer_raw_text,
        answer_finish_reason=answer_finish_reason,
        action_raw_text=action_raw_text,
        action_finish_reason=action_finish_reason,
        say_ok=say_ok,
        say_preempted=say_preempted,
        action=record,
        repair_used=repair_used,
        action_reason_code=action_reason_code,
        action_first_attempt_valid=action_first_attempt_valid,
        action_confidence=action_confidence,
        action_abstain=action_abstain,
        observation_raw_text=observation_raw_text,
        observation_text=observation_text,
        observation_error=observation_error,
        stopped_reason=action_stopped_reason,
    )


def _action_stop_when(text: str) -> bool:
    """Рвать стрим, как только полное JSON действия стало схема-валидным.

    Раннего stop на `"tool":"reply"` НЕТ намеренно: `confidence`/`abstain`
    идут в конце объекта, и обрыв раньше них скрал бы сигнал воздержания
    (ADR-0001 §2/§4) -- сэкономленные токены не стоят потери abstain.
    """
    return parse_action(text) is not None

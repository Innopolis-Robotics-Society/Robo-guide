"""Мост между ROS (executor-потоки, action-goal) и потоком, исполняющим тур.

design §5.3: "Коллбэки подписок кладут запрос в queue.Queue и взводят
Event; FSM забирает." Один `FsmContext` -- один активный `RunTour`-goal
(design §2.4: "Один активный RunTour goal"), создаётся заново в начале
`execute_callback` и уничтожается по его завершении -- как
`_ActiveExecution` в narration_server_node.py, только на уровне целого
тура, а не одного чанка.

Не является частью design §5.3 буквально -- он описывает МЕХАНИЗМ, не
называя объект, который его держит. `FsmContext` -- осознанное расширение,
чтобы состояния (`fsm/states/*.py`) не тянули сырые `rclpy.node.Node`.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

__all__ = ["FsmContext"]


class FsmContext:
    """Очереди/флаги/клиенты, общие для всех состояний одного прогона тура."""

    def __init__(  # noqa: PLR0913 -- поля одного контекста, не команда с побочными эффектами
        self,
        *,
        now_ns: Callable[[], int],
        goal_handle: object,
        narrate_client: object,
        say_client: object,
        nav_client: object,
        resolve_pose: Callable[[str], object],
        home_pose: Callable[[], object],
        safety_hold_event: threading.Event,
        deactivating_event: threading.Event,
        greeting_text: str,
        confirm_question_text: str,
        confirm_timeout_s: float,
        confirm_repeat_max: int,
        answer_max_s: float,
        nav_stop_timeout_s: float,
        pause_timeout_s: float,
        held_max_s: float,
        poll_period_s: float,
        hard_stop_result_timeout_s: float,
        on_state_changed: Callable[[str, object], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        """Собрать контекст одного `RunTour`-goal.

        `safety_hold_event`/`deactivating_event` -- общие для ВСЕХ туров
        объекты узла (не пересоздаются на каждый goal), остальное --
        специфично для текущего прогона.
        """
        self.now_ns = now_ns
        self.goal_handle = goal_handle
        self.narrate_client = narrate_client
        self.say_client = say_client
        self.nav_client = nav_client
        self.resolve_pose = resolve_pose
        self.home_pose = home_pose
        self.safety_hold_event = safety_hold_event
        self.deactivating_event = deactivating_event
        self.greeting_text = greeting_text
        self.confirm_question_text = confirm_question_text
        self.confirm_timeout_s = confirm_timeout_s
        self.confirm_repeat_max = confirm_repeat_max
        self.answer_max_s = answer_max_s
        self.nav_stop_timeout_s = nav_stop_timeout_s
        self.pause_timeout_s = pause_timeout_s
        self.held_max_s = held_max_s
        self.poll_period_s = poll_period_s
        self.hard_stop_result_timeout_s = hard_stop_result_timeout_s
        self._on_state_changed = on_state_changed
        self._log = log

        self.barge_in_event = threading.Event()
        self._answer_queue: queue.Queue[str] = queue.Queue()
        self._confirm_queue: queue.Queue[bool] = queue.Queue()
        self._pause_queue: queue.Queue[bool] = queue.Queue()
        self._resume_queue: queue.Queue[bool] = queue.Queue()

    def is_cancel_requested(self) -> bool:
        """Вернуть True, если клиент запросил отмену текущего `RunTour`-goal-а."""
        return bool(self.goal_handle.is_cancel_requested)  # type: ignore[attr-defined]

    # -- тестовые/CLI хуки (design §5.4 п.6, §5.2 ANSWERED/YES/NO/RESUMED --
    #    без реального ASR/LLM-сигнала в v1, см. докстринги состояний) -----

    def submit_answer(self, text: str) -> None:
        """Положить текст ответа посетителя -- заберёт `AnsweringState.poll()`."""
        self._answer_queue.put(text)

    def take_answer(self) -> str | None:
        """Забрать текст ответа, если он уже был положен, иначе None."""
        try:
            return self._answer_queue.get_nowait()
        except queue.Empty:
            return None

    def submit_confirm(self, *, is_yes: bool) -> None:
        """Положить да/нет-ответ на «Идём дальше?» -- заберёт `AwaitingConfirmState.poll()`."""
        self._confirm_queue.put(is_yes)

    def take_confirm_answer(self) -> bool | None:
        """Забрать да/нет-ответ, если он уже был положен, иначе None."""
        try:
            return self._confirm_queue.get_nowait()
        except queue.Empty:
            return None

    def request_pause(self) -> None:
        """Запросить паузу тура -- заберёт `NarratingState.poll()`."""
        self._pause_queue.put(True)

    def take_pause_request(self) -> bool:
        """Забрать запрос на паузу, если он был; True -- только один раз на запрос."""
        try:
            self._pause_queue.get_nowait()
            return True
        except queue.Empty:
            return False

    def request_resume(self) -> None:
        """Запросить возобновление из PAUSED -- заберёт `PausedState.poll()`."""
        self._resume_queue.put(True)

    def take_resume_request(self) -> bool:
        """Забрать запрос на возобновление, если он был; True -- только один раз на запрос."""
        try:
            self._resume_queue.get_nowait()
            return True
        except queue.Empty:
            return False

    def consume_barge_in(self) -> bool:
        """Вернуть True и сбросить флаг, если с прошлого вызова было хотя бы одно barge-in."""
        if self.barge_in_event.is_set():
            self.barge_in_event.clear()
            return True
        return False

    def on_state_changed(self, name: str, blackboard: object) -> None:
        """Оповестить узел -- публикация /mission/state и feedback RunTour (design §2.4)."""
        if self._on_state_changed is not None:
            self._on_state_changed(name, blackboard)

    def log(self, message: str) -> None:
        """Записать ПРИЧИНУ неочевидного решения состояния, не сам факт перехода.

        Факт перехода уже виден по `/mission/state` -- а вот ПОЧЕМУ (истёк
        какой именно таймаут, что вернул Narrate/nav) без этого не виден
        вообще. Без явного лога тут молчаливое решение (например,
        `confirm_timeout_s` истёк -> NO) неотличимо в логе от реального
        ответа посетителя -- ровно то, что запутало оператора вживую при
        отладке шага 8.
        """
        if self._log is not None:
            self._log(message)

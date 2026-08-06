"""`mission_container` -- `mission_fsm` + `narration_server` в одном процессе (design §1).

Дефолт в `launch/mission.launch.py`. Причина -- не удобство деплоя, а
латентность: `mission_fsm` ждёт результата `Narrate`-goal-а, который сам
внутри ждёт результатов `Say`-goal-ов; если это два процесса, каждый шаг
барже-ин -> пауза нарратива идёт через межпроцессный DDS RTPS путь дважды
(mission_fsm -> narration_server, narration_server -> tts_node) поверх и
так неизбежного пути до `tts_node`. Один процесс с общим
`MultiThreadedExecutor` убирает сериализацию ТОЛЬКО между этими двумя
узлами -- `tts_node` всё равно отдельный процесс. `presence_monitor`
специально не здесь: он не в горячем пути прерывания, отдельный процесс
для него дешевле в отладке (design §1: "отдельные узлы остаются доступны
для отладки" -- относится и к этому: `mission_fsm`/`narration_server`
можно поднять раздельно теми же `console_scripts`, `mission_container` --
опциональная оптимизация, не единственный путь запуска).
"""

from __future__ import annotations

import rclpy
from rclpy.executors import MultiThreadedExecutor

from guide_robot_mission_control.mission_fsm_node import MissionFsmNode
from guide_robot_mission_control.narration_server_node import NarrationServerNode

__all__ = ["main"]


def main(args: list[str] | None = None) -> None:
    """Точка входа. Один executor, оба узла -- barge-in -> пауза без межпроцессного скачка."""
    rclpy.init(args=args)
    fsm_node = MissionFsmNode()
    narration_node = NarrationServerNode()
    executor = MultiThreadedExecutor(num_threads=8)
    executor.add_node(fsm_node)
    executor.add_node(narration_node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        fsm_node.destroy_node()
        narration_node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()

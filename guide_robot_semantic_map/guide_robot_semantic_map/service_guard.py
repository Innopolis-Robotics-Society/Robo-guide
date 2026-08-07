"""Общий гард сервисов lifecycle-нод семантической карты (design.md §1).

rclpy не глушит create_service() сам, когда нода не в состоянии active --
это поведение только у create_lifecycle_publisher(). Без явной проверки
запрос сервиса, пришедший до configure/activate, либо упадёт с
исключением (ресурсы ещё не собраны), либо получит default-конструированный
ответ без единого слова объяснения. design.md §1 требует "явную ошибку,
а не молчание", а не просто отсутствие краша.

Три ноды пакета (content_server, location_server, route_planner) отвечают
на запросы плоскими response-сообщениями без выделенного кода ошибки --
единственный канал сообщить "вызов вне active" это лог + SystemEvent,
response при этом остаётся default-constructed (пустые chunks/candidates
и т.п.), что уже согласуется с design.md §1.3: "нет данных -- пустой
результат, решение принимает mission".
"""

from __future__ import annotations

from guide_robot_msgs.msg import SystemEvent

__all__ = ["ServiceGuardMixin"]


class ServiceGuardMixin:
    """Подмешивается в LifecycleNode-наследники этого пакета.

    Несущий класс обязан выставлять self._active: bool в on_activate/
    on_deactivate и, до создания сервисов в on_configure, self._event_pub
    = None. Публикация события не критична для самого гарда: если
    паблишер ещё не поднят, вызов всё равно отклоняется и логируется.
    """

    _active: bool = False
    _event_pub: object | None = None

    def _require_active(self, service_name: str) -> bool:
        """Проверить, что нода активна; иначе гард уже отреагировал сам.

        Возвращает True, если запрос можно обслуживать.
        """
        if self._active:
            return True
        detail = f"{service_name}: вызван вне ACTIVE"
        self.get_logger().error(detail)  # type: ignore[attr-defined]
        self._publish_system_event(f"{self.get_name()}.inactive_call", SystemEvent.ERROR, detail)  # type: ignore[attr-defined]
        return False

    def _publish_system_event(self, event_id: str, severity: int, detail: str) -> None:
        """Опубликовать SystemEvent, если lifecycle-паблишер уже поднят on_configure'ом."""
        if self._event_pub is None:
            return
        event = SystemEvent(id=event_id, severity=severity, detail=detail)
        event.header.stamp = self.get_clock().now().to_msg()  # type: ignore[attr-defined]
        self._event_pub.publish(event)  # type: ignore[attr-defined]

"""Нода текстов экспонатов (design.md §1.3).

Единственная ответственность: отдать заранее написанные чанки текста по
exhibit_id/mode/language. content/*.yaml грузится целиком в память на
on_configure -- на Orin диск это latency и точка отказа, а объём (десятки
КБ) укладывается в память без вопросов. Никакого чтения с диска в рантайме.

Инвариант, который держит вся нода: ни одной кодовой ветки, порождающей
текст. Нет контента для exhibit_id/языка -- пустой chunks[] и version="",
разбираться с этим (переспросить, промолчать, извиниться) -- дело mission,
не этой ноды.
"""

from __future__ import annotations

import rclpy
from ament_index_python.packages import get_package_share_directory
from guide_robot_msgs.msg import SystemEvent
from guide_robot_msgs.srv import GetExhibitContent
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_semantic_map.lib.content_io import (
    VALID_LEVELS,
    ExhibitContent,
    load_content_dir,
    pick_language,
    select_chunks,
)
from guide_robot_semantic_map.lib.qos import QOS_SYSTEM_EVENT
from guide_robot_semantic_map.service_guard import ServiceGuardMixin


class ContentServerNode(ServiceGuardMixin, LifecycleNode):
    """Lifecycle-нода `~/get_exhibit_content`."""

    def __init__(self) -> None:
        """Объявить параметры. Контент грузится в on_configure."""
        super().__init__("content_server")

        self.declare_parameter("content_dir", "")
        self.declare_parameter("default_language", "ru")

        self._content: dict[tuple[str, str], ExhibitContent] = {}
        self._active = False
        self._event_pub = None
        self._stage = "инициализация"

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Загрузить content/*.yaml целиком. Любая ошибка данных -- отказ активации.

        Тело целиком в try: исключение, вылетевшее из колбэка перехода
        lifecycle, машина состояний поглощает без объяснений наружу --
        причину логируем сами, по self._stage.
        """
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался на шаге '{self._stage}': {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        self._stage = "загрузка контента"
        content_dir = str(self.get_parameter("content_dir").value)
        if not content_dir:
            content_dir = f"{get_package_share_directory('guide_robot_semantic_map')}/content"
        self._content, warnings = load_content_dir(content_dir)
        for warning in warnings:
            self.get_logger().warning(warning)

        self._stage = "интерфейсы ROS"
        self._event_pub = self.create_lifecycle_publisher(
            SystemEvent, "/system_event", QOS_SYSTEM_EVENT
        )
        self._service = self.create_service(
            GetExhibitContent, "~/get_exhibit_content", self._on_get_exhibit_content
        )

        self._stage = "готово"
        self.get_logger().info(
            f"content_server сконфигурирован: {len(self._content)} записей "
            f"контента из {content_dir}"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Начать отвечать на сервис."""
        self._active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Перестать отвечать на сервис явным отказом, не молчанием."""
        self._active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Освободить загруженный контент."""
        del state
        self._content = {}
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """То же, что cleanup."""
        return self.on_cleanup(state)

    # -- сервис -----------------------------------------------------------

    def _on_get_exhibit_content(
        self, request: GetExhibitContent.Request, response: GetExhibitContent.Response
    ) -> GetExhibitContent.Response:
        if not self._require_active("get_exhibit_content"):
            return response

        mode = request.mode if request.mode in VALID_LEVELS else "short"
        if request.mode not in VALID_LEVELS:
            self.get_logger().warning(
                f"get_exhibit_content: неизвестный mode={request.mode!r} для "
                f"exhibit_id={request.exhibit_id!r}, использую 'short'"
            )

        default_language = str(self.get_parameter("default_language").value)
        available = {
            language
            for (exhibit_id, language) in self._content
            if exhibit_id == request.exhibit_id
        }
        language = pick_language(available, request.language, default_language)

        if language is None:
            self.get_logger().warning(
                f"get_exhibit_content: нет контента для exhibit_id={request.exhibit_id!r} "
                f"(запрошен язык {request.language!r}, default {default_language!r})"
            )
            return response

        if request.language and language != request.language:
            # Фолбэк на другой язык -- разрешён (design.md §1.3), но не
            # молча: если бы отдали ru вместо запрошенного en без следа,
            # узнать об этом можно было бы только на слух у посетителя.
            detail = (
                f"exhibit_id={request.exhibit_id} requested_language={request.language!r} "
                f"used_language={language!r}"
            )
            self.get_logger().warning(f"get_exhibit_content: языковой фолбэк -- {detail}")
            self._publish_system_event(
                "semantic_map.content_language_fallback", SystemEvent.WARN, detail
            )

        content = self._content[(request.exhibit_id, language)]
        response.chunks = select_chunks(content, mode)
        response.version = content.version
        return response


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = ContentServerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()

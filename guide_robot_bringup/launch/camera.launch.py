"""Запуск камеры для визуального пайплайна диалога (Taiga #2, epic #14).

`v4l2_camera` публикует сырой поток `/camera/image_raw`; плагин
`compressed_image_transport` (установлен в образе) автоматически
дублирует его в `/camera/image_raw/compressed` — тот самый топик, на который
подписывается диалоговый агент (`guide_robot_llm`, блок `vision.*` в
`config/llm.yaml`, `QOS_VISION_COMPRESSED` в `lib/qos.py`).

Запуск:
    ros2 launch guide_robot_bringup camera.launch.py
    # с явными параметрами:
    ros2 launch guide_robot_bringup camera.launch.py \
        camera_device:=/dev/video0 image_width:=1280 image_height:=720

ВНИМАНИЕ: на включение камеры в диалог этого недостаточно — нужно ещё
`vision.enabled:=true` (по умолчанию false, робот без камеры работает
text-only). На машине без камеры этот файл просто не запускается.

Камера в robot_description отсутствует (опциональное железо):
`frame_id=camera` — метаданные кадра, TF `camera → base_footprint`
добавляется вместе с реальным кронштейном. Визуальный пайплайн диалога
к TF не привязан (потребляет пиксели, а не геометрию).

Ручной fallback, если сжатый транспорт почему-то не поднялся
(image_tools в образ не входит, ставится отдельно):
    ros2 run image_tools image_raw_compressed --ros-args \
        -r image_raw:=/camera/image_raw \
        -r image_raw_compressed:=/camera/image_raw/compressed
"""

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    """v4l2_camera + (автоматически) compressed transport."""
    return LaunchDescription(
        [
            Node(
                package="v4l2_camera",
                executable="v4l2_camera_node",
                name="camera",
                output="screen",
                parameters=[
                    {
                        # /dev/video0 -- типичный номер USB-камеры; укажите свой.
                        "camera_device": "/dev/video0",
                        "image_width": 1280,
                        "image_height": 720,
                        # Имя камеры/кадра: v4l2_camera использует его как frame_id.
                        "camera_name": "camera",
                        "frame_id": "camera",
                        "publish_raw": True,
                    }
                ],
            )
        ]
    )

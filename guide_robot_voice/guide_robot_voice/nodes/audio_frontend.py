"""Захват аудио и публикация кадров.

Про AEC. Его здесь нет, и это осознанно. Программный AEC поверх ROS --
худший из вариантов: он требует reference-сигнала, синхронного с захватом
с точностью до сэмпла, а всё, что проходит через DDS, эту синхронность
теряет. AEC решается уровнем ниже: аппаратно на XVF3800 либо в PipeWire
через module-echo-cancel. Фронтенд открывает уже очищенный source.

Что фронтенд обязан делать вместо этого -- мерить и логировать:

  * разрывы в first_sample. Пропуск буфера для AEC означает скачок задержки,
    то есть расхождение фильтра. Это ошибка, а не предупреждение;
  * уровень сигнала. Уползание шумового пола -- первый признак того,
    что источник оказался не тем устройством или что AGC веб-камеры
    вмешался туда, куда не должен;
  * трёхканальная запись raw / reference / после AEC. 16 кГц x 16 бит x 3 --
    96 КБ/с, то есть бесплатно. Без неё разбор "почему робот проснулся
    на слове экспоната" невозможен в принципе.

ЗАМЕЧАНИЕ О ПРОИЗВОДИТЕЛЬНОСТИ. Публикация PCM в топик -- транспорт периода
разработки. Гонять 16 кГц через сериализацию DDS на каждом хопе три раза
(фронтенд -> VAD -> wakeword) расточительно, а rclpy под GIL и GC добавляет
джиттер в десятки миллисекунд к аудиоколбэку. Целевая форма -- C++
composable nodes в одном контейнере с intra-process comms и lock-free
кольцевым буфером между потоком ALSA и ROS. Переносить сейчас преждевременно:
сначала железо и измеренный джиттер, потом оптимизация. Контракт наружу
(/asr/*, /speech/*) при переносе не меняется.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from guide_robot_msgs.msg import AudioChunk
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.qos import qos_profile_sensor_data

from guide_robot_voice.audio.sources import DeviceSource, FileSource

EPSILON = 1e-9

# Ниже этого уровня шумовой пол означает не тишину, а неверный источник:
# закрытый микрофон, немой канал или выбранное по ошибке устройство.
SUSPICIOUS_FLOOR_DBFS = -70.0


class AudioFrontend(LifecycleNode):
    """Захват аудио, выбор канала, публикация кадров и диагностика."""

    def __init__(self) -> None:
        """Объявить параметры."""
        super().__init__("audio_frontend")

        self.declare_parameter("source", "device")
        self.declare_parameter("device_name", "XVF3800")
        self.declare_parameter("wav_path", "")
        self.declare_parameter("wav_realtime", True)
        self.declare_parameter("sample_rate", 16000)
        self.declare_parameter("channels", 2)
        self.declare_parameter("block_ms", 20)
        self.declare_parameter("asr_channel", 1)
        self.declare_parameter("monitor_channel", 0)
        self.declare_parameter("frame_id", "mic_array")
        self.declare_parameter("record_path", "")

        self._source = None
        self._recorder: wave.Wave_write | None = None
        self._expected_sample = 0
        self._gaps = 0
        self._level_dbfs = -120.0

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Создать источник и интерфейсы."""
        del state
        kind = str(self.get_parameter("source").value)
        block_ms = int(self.get_parameter("block_ms").value)

        # Всё тело в try: исключение из колбэка перехода lifecycle
        # поглощается машиной состояний, и наружу приходит голое
        # "Transitioning failed" без причины.
        try:
            if kind == "device":
                self._source = DeviceSource(
                    device=str(self.get_parameter("device_name").value),
                    sample_rate=int(self.get_parameter("sample_rate").value),
                    channels=int(self.get_parameter("channels").value),
                    block_ms=block_ms,
                    asr_channel=int(self.get_parameter("asr_channel").value),
                    monitor_channel=int(self.get_parameter("monitor_channel").value),
                )
            elif kind == "file":
                self._source = FileSource(
                    path=str(self.get_parameter("wav_path").value),
                    block_ms=block_ms,
                    realtime=bool(self.get_parameter("wav_realtime").value),
                )
            else:
                raise ValueError(f"source должен быть device или file, получено {kind!r}")
        except Exception as error:
            self.get_logger().error(f"не удалось открыть источник аудио: {error}")
            return TransitionCallbackReturn.FAILURE

        self._chunk_pub = self.create_lifecycle_publisher(
            AudioChunk, "/audio/chunk", qos_profile_sensor_data
        )
        self._diag_pub = self.create_lifecycle_publisher(DiagnosticArray, "/diagnostics", 10)

        record_path = str(self.get_parameter("record_path").value)
        if record_path:
            Path(record_path).parent.mkdir(parents=True, exist_ok=True)
            # Контекстный менеджер неприменим: файл живёт от on_configure
            # до on_cleanup, а не в пределах одного вызова.
            self._recorder = wave.open(record_path, "wb")  # noqa: SIM115
            self._recorder.setnchannels(int(self.get_parameter("channels").value))
            self._recorder.setsampwidth(2)
            self._recorder.setframerate(int(self.get_parameter("sample_rate").value))
            self.get_logger().info(f"запись сырого аудио: {record_path}")

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Запустить захват и цикл чтения."""
        self._source.start()
        self._read_timer = self.create_timer(0.001, self._pump)
        self._diag_timer = self.create_timer(1.0, self._publish_diagnostics)
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Погасить микрофон. Вызывается при постановке на зарядку."""
        self._source.stop()
        self.destroy_timer(self._read_timer)
        self.destroy_timer(self._diag_timer)
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Закрыть источник и файл записи."""
        del state
        if self._source is not None:
            self._source.stop()
            self._source = None
        if self._recorder is not None:
            self._recorder.close()
            self._recorder = None
        return TransitionCallbackReturn.SUCCESS

    def _pump(self) -> None:
        """Прочитать доступные кадры и опубликовать."""
        frame = self._source.read(timeout=0.0)
        if frame is None:
            return

        if frame.first_sample != self._expected_sample and self._expected_sample:
            missing = frame.first_sample - self._expected_sample
            self._gaps += 1
            # Не warning: пропуск буфера ломает сходимость AEC.
            self.get_logger().error(
                f"разрыв аудиопотока: пропущено {missing} сэмплов "
                f"({missing / self._source.sample_rate * 1e3:.1f} мс)"
            )
        self._expected_sample = frame.first_sample + frame.pcm.shape[0]

        if self._recorder is not None:
            self._recorder.writeframes(frame.pcm.tobytes())

        pcm = (
            self._source.select_asr(frame)
            if isinstance(self._source, DeviceSource)
            else self._flatten(frame.pcm)
        )
        self._level_dbfs = self._dbfs(pcm)

        msg = AudioChunk()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = str(self.get_parameter("frame_id").value)
        msg.sample_rate = int(self._source.sample_rate)
        msg.channels = 1
        msg.first_sample = int(frame.first_sample)
        msg.data = pcm.tolist()
        self._chunk_pub.publish(msg)

    @staticmethod
    def _flatten(pcm: np.ndarray) -> np.ndarray:
        return pcm if pcm.ndim == 1 else np.ascontiguousarray(pcm[:, 0])

    @staticmethod
    def _dbfs(pcm: np.ndarray) -> float:
        """RMS кадра в dBFS."""
        rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64) / 32768.0))))
        return 20.0 * float(np.log10(rms + EPSILON))

    def _publish_diagnostics(self) -> None:
        """Опубликовать состояние захвата."""
        overflows = getattr(self._source, "overflows", 0)
        level = DiagnosticStatus.OK
        message = "capturing"
        if self._gaps:
            level = DiagnosticStatus.ERROR
            message = "разрывы потока: AEC не сойдётся"
        elif self._level_dbfs < SUSPICIOUS_FLOOR_DBFS:
            level = DiagnosticStatus.WARN
            message = "шумовой пол подозрительно низкий: тот ли это источник?"

        diag = DiagnosticArray()
        diag.header.stamp = self.get_clock().now().to_msg()
        diag.status.append(
            DiagnosticStatus(
                name="voice/audio_frontend",
                hardware_id=str(self.get_parameter("device_name").value),
                level=level,
                message=message,
                values=[
                    KeyValue(key="level_dbfs", value=f"{self._level_dbfs:.1f}"),
                    KeyValue(key="stream_gaps", value=str(self._gaps)),
                    KeyValue(key="overflows", value=str(overflows)),
                ],
            )
        )
        self._diag_pub.publish(diag)


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = AudioFrontend()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()

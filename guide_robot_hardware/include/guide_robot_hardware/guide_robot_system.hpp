#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "hardware_interface/handle.hpp"
#include "hardware_interface/hardware_info.hpp"
#include "hardware_interface/system_interface.hpp"
#include "hardware_interface/types/hardware_interface_return_values.hpp"
#include "rclcpp/rclcpp.hpp"
#include "rclcpp_lifecycle/state.hpp"

namespace guide_robot_hardware {

class GuideRobotSystem : public hardware_interface::SystemInterface
{
public:
  RCLCPP_SHARED_PTR_DEFINITIONS(GuideRobotSystem)

  ~GuideRobotSystem() override;

  hardware_interface::CallbackReturn on_init(
    const hardware_interface::HardwareInfo & info) override;

  hardware_interface::CallbackReturn on_configure(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_activate(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_deactivate(
    const rclcpp_lifecycle::State & previous_state) override;

  // Порт открывается в on_configure, поэтому закрывается СИММЕТРИЧНО здесь,
  // а не в on_deactivate: deactivate→activate — штатная операция
  // controller_manager, после неё драйвер обязан продолжать работать.
  hardware_interface::CallbackReturn on_cleanup(
    const rclcpp_lifecycle::State & previous_state) override;

  // ERROR из read()/write() ведёт компонент в unconfigured МИНУЯ on_cleanup,
  // поэтому освобождать порт приходится и здесь — иначе следующий on_configure
  // откроет второй fd на тот же порт, а первый утечёт.
  hardware_interface::CallbackReturn on_error(
    const rclcpp_lifecycle::State & previous_state) override;

  std::vector<hardware_interface::StateInterface> export_state_interfaces() override;
  std::vector<hardware_interface::CommandInterface> export_command_interfaces() override;

  hardware_interface::return_type read(
    const rclcpp::Time & time, const rclcpp::Duration & period) override;

  hardware_interface::return_type write(
    const rclcpp::Time & time, const rclcpp::Duration & period) override;

private:
  /// Прочитать hardware_parameters с внятной диагностикой на каждый параметр.
  /// Возвращает false, если обязательный параметр отсутствует или не парсится.
  bool loadParameters();

  /// Отправить запрос чтения регистра 0xa0 у мотора left_wheel_id_.
  /// Пакет: ff ff <id> 04 03 a0 05 <chk>, контрольная сумма считается по факту.
  /// Вызывать с захваченным serial_mutex_.
  void sendEncoderRequestLocked();

  /// Выгрести доступные байты в rx_buffer_. При budget_ms > 0 ждёт через poll(),
  /// пока не наберётся expect_bytes новых байт или не кончится бюджет; при 0 —
  /// читает то, что уже лежит в порте, и возвращается немедленно.
  /// Вызывать с захваченным serial_mutex_.
  void drainPortLocked(int budget_ms, size_t expect_bytes);

  /// Собрать и отправить 18-байтный пакет скоростей (слоты адресуются по позиции).
  /// Сам берёт serial_mutex_. Возвращает false, если пакет ушёл не полностью.
  bool writeSpeedPacket(double slot1_cmd, double slot2_cmd);

  /// Заголовок и контрольная сумма ENC_FRAME_LEN байт по адресу frame.
  bool isEncoderFrame(const uint8_t * frame) const;

  /// Обновить позиции и скорости колёс по проверенному кадру энкодеров.
  /// Кадр, не прошедший correctTickDelta(), отбрасывается целиком: состояние
  /// не меняется вообще, для верхнего стека это «кадра не было».
  void applyEncoderFrame(const uint8_t * frame, double period_s);

  /// Привести сырую дельту тиков к физически возможной: прошивка FURO иногда
  /// теряет перенос счётчика позиции, и та уезжает ровно на 2^16 тиков навсегда
  /// (контрольная сумма кадра при этом верна — врёт сам драйвер).
  ///
  /// Дельта в пределах полупериода счётчика принимается как есть. Вышедшая за
  /// него сверяется с тремя кандидатами (сама дельта и она же +-wrap) по
  /// конверту max_wheel_velocity_ * dt; ни одного правдоподобного — кадр
  /// отвергается, несколько — берётся ближайший к prev_rate (тиков/с на прошлом
  /// ПРИНЯТОМ кадре): разрыв означал бы скачок скорости, невозможный для базы.
  ///
  /// false — кадр непригоден, позиции трогать нельзя.
  bool correctTickDelta(
    int64_t raw_delta, double dt, double prev_rate, const char * slot_name, int64_t & out_delta);

  /// рад/с → motor units с учётом знака конкретного слота.
  /// Клампит по max_wheel_velocity_ — независимо от лимитера контроллера.
  int16_t toMotorUnits(double omega, double sign) const;

  /// Сторожевой таймер: шлёт нулевые скорости, если write() не вызывался
  /// дольше cmd_timeout_ (зависший или убитый управляющий цикл).
  void watchdogLoop();

  /// Возвращает false, если сторож запустить невозможно (cmd_timeout_ <= 0).
  /// Активация без сторожа запрещена: см. on_activate.
  bool startWatchdog();
  void stopWatchdog();

  /// Остановить моторы и закрыть порт. Идемпотентно.
  void closePort();

  int serial_fd_{-1};
  std::string serial_port_;
  int baud_rate_{115200};

  // Единственный мьютекс на ВЕСЬ обмен по порту: и ::write, и ::read/poll.
  // watchdogLoop() шлёт стоп-пакеты из своего потока параллельно с
  // read()/write() управляющего цикла, а RS-485 полудуплексный — окно приёма
  // ответа энкодеров нельзя разрывать чужой посылкой. read() держит мьютекс на
  // всю транзакцию «запрос → ожидание → чтение» (не дольше ENC_RESPONSE_BUDGET_MS).
  std::mutex serial_mutex_;

  // Параметры моторов из URDF
  int left_wheel_id_{46};
  int right_wheel_id_{47};
  bool swap_drives_{false};
  double left_sign_{1.0};
  double right_sign_{-1.0};
  double speed_coefficient_{0.0001706};
  double wheel_radius_{0.1026};

  // Обязательные параметры без безопасного дефолта: отсутствие любого из них —
  // отказ on_init, а не тихая работа с «нулём» (см. loadParameters()).
  double cmd_timeout_{0.0};         // с, таймаут сторожа
  double max_wheel_velocity_{0.0};  // рад/с на колесо, независимый клэмп

  // Ускорение в единицах прошивки FURO. Смысл и масштаб вендором не
  // задокументированы; 1000 — значение, на котором база откатана.
  // Не связано с limits.max_linear_accel из robot_params.yaml.
  uint16_t motor_accel_{1000};

  // Сколько секунд подряд read() может не разобрать ни одного валидного кадра
  // энкодеров, прежде чем компонент уйдёт в ошибку. 0 — проверка выключена.
  double encoder_timeout_{1.0};

  // Ход времени для RCLCPP_*_THROTTLE: монотонный, чтобы прыжок системных
  // часов не заблокировал диагностику на произвольный срок.
  //
  // mutable: в Humble rclcpp::Clock::now() не const (константным он стал только
  // в более поздних дистрибутивах), а RCLCPP_*_THROTTLE зовёт его из лямбды.
  // Без mutable макрос не собирается в const-методах вроде toMotorUnits().
  // Логирование — не часть наблюдаемого состояния, так что const-контракт
  // методов важнее.
  mutable rclcpp::Clock clock_{RCL_STEADY_TIME};

  // Команды (пишет controller_manager)
  double left_vel_cmd_{0.0};   // рад/с
  double right_vel_cmd_{0.0};  // рад/с

  // Состояния (читает controller_manager)
  double left_position_{0.0};
  double right_position_{0.0};
  double left_velocity_{0.0};
  double right_velocity_{0.0};

  double ticks_per_rev_{131072.0};

  // Граница счётчика драйвера, на которой он теряет перенос/заём (см.
  // correctTickDelta). 65536 = 16 бит; 0 выключает ремонт разрывов, и тогда
  // разрыв уедет в одометрию как настоящее перемещение.
  double encoder_wrap_ticks_{65536.0};

  // Через сколько циклов управления уходит запрос энкодеров. Темп кадров им не
  // управляется: прошивка отдаёт позицию раз в ~215 мс независимо от опроса.
  int encoder_poll_divider_{5};

  std::vector<uint8_t> rx_buffer_;
  bool initialized_encoders_{false};
  int enc_request_counter_{0};

  // Подряд идущие неудачные записи в порт. Одиночная — это EAGAIN, а не потеря
  // связи; серия означает, что команды до драйвера не доходят, а верхний стек
  // об этом не знает (см. WRITE_FAILURE_LIMIT).
  int write_failures_{0};

  // Сырые тики прошлого кадра и развёрнутый (починенный) счёт по каждому слоту.
  // Позиция считается из развёрнутого счёта, а не из сырого: после ремонта
  // разрыва сырое значение драйвера остаётся смещённым навсегда.
  int32_t prev_raw_slot1_{0};
  int32_t prev_raw_slot2_{0};
  int64_t unwrapped_slot1_{0};
  int64_t unwrapped_slot2_{0};

  // Скорость слота (тиков/с) на прошлом ПРИНЯТОМ кадре. Разрешает выбор между
  // физически достижимыми кандидатами в correctTickDelta по непрерывности.
  double prev_slot1_rate_{0.0};
  double prev_slot2_rate_{0.0};

  // Диагностика: сколько разрывов починено и сколько кадров отвергнуто.
  // Растущий счётчик разрывов — это про деградацию железа/прошивки, а не про
  // норму, поэтому он должен быть виден, а не молча компенсироваться.
  uint64_t enc_wrap_repairs_{0};
  uint64_t enc_frames_rejected_{0};

  // Время, накопленное с прошлого УСПЕШНО разобранного пакета энкодеров.
  // Кадр приходит раз в encoder_poll_divider_ циклов, поэтому делить дельту
  // позиции на период одного цикла нельзя — скорость завысится во столько же раз.
  double enc_elapsed_{0.0};

  // То же время, но НЕ обнуляемое ничем, кроме валидного кадра: детектор
  // отказа энкодер-каскада. enc_elapsed_ для этого не годится — он участвует
  // в расчёте скорости и имеет собственные фолбэки.
  double enc_silence_{0.0};
  bool enc_fault_{false};

  std::thread watchdog_thread_;
  std::atomic<bool> watchdog_running_{false};
  std::atomic<int64_t> last_write_ns_{0};  // steady_clock, момент последнего write()
};

}  // namespace guide_robot_hardware

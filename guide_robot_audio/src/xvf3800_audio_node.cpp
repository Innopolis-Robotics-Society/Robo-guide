#include <alsa/asoundlib.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "guide_robot_msgs/action/play_pcm.hpp"
#include "guide_robot_msgs/msg/audio_chunk.hpp"
#include "guide_robot_msgs/msg/playback_state.hpp"
#include "guide_robot_msgs/srv/begin_playback.hpp"
#include "guide_robot_msgs/srv/fence_playback.hpp"
#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"
#include "rclcpp_lifecycle/lifecycle_node.hpp"
#include "rclcpp_lifecycle/lifecycle_publisher.hpp"

namespace guide_robot_audio {

using CallbackReturn = rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn;
using AudioChunk = guide_robot_msgs::msg::AudioChunk;
using PlaybackState = guide_robot_msgs::msg::PlaybackState;
using BeginPlayback = guide_robot_msgs::srv::BeginPlayback;
using FencePlayback = guide_robot_msgs::srv::FencePlayback;
using PlayPcm = guide_robot_msgs::action::PlayPcm;
using PlayPcmGoalHandle = rclcpp_action::ServerGoalHandle<PlayPcm>;

struct TimelineSegment {
  std::uint64_t output_first{0};
  std::uint64_t sample_count{0};
};

struct PlaybackBlock {
  std::vector<std::int16_t> mono;
  std::vector<std::pair<std::size_t, std::size_t>> audio_runs;
  std::uint64_t stream_id{0};
  std::uint64_t generation{0};
};

struct PlaybackSnapshot {
  std::string goal_id;
  std::string reason;
  std::uint64_t stream_id{0};
  std::uint64_t generation{0};
  std::uint64_t submitted_samples{0};
  std::uint64_t presented_samples{0};
  std::uint32_t buffered_samples{0};
  std::uint8_t state{PlaybackState::STATE_IDLE};
};

struct ActionWorker {
  std::thread thread;
  std::shared_ptr<std::atomic<bool>> finished;
};

class Xvf3800AudioNode final : public rclcpp_lifecycle::LifecycleNode
{
public:
  Xvf3800AudioNode() : rclcpp_lifecycle::LifecycleNode("xvf3800_audio_node")
  {
    declare_parameter<std::string>("expected_usb_serial", "");
    declare_parameter<std::string>("capture_device", "hw:CARD=Array,DEV=0");
    declare_parameter<std::string>("playback_device", "hw:CARD=Array,DEV=0");
    declare_parameter<int>("device_rate", 16000);
    declare_parameter<int>("capture_channels", 2);
    declare_parameter<int>("playback_channels", 2);
    declare_parameter<std::string>("sample_format", "S16_LE");
    declare_parameter<int>("processed_channel", 0);
    declare_parameter<bool>("publish_stereo_debug", false);
    declare_parameter<int>("frame_ms", 16);
    declare_parameter<int>("alsa_latency_us", 48000);
    declare_parameter<int>("max_playback_queue_ms", 600);
    declare_parameter<int>("max_pcm_block_ms", 2000);
    declare_parameter<int>("playback_fade_ms", 20);
    declare_parameter<double>("playback_state_hz", 20.0);
    declare_parameter<std::string>("frame_id", "mic_array");
  }

  ~Xvf3800AudioNode() override { close_audio(); }

private:
  CallbackReturn on_configure(const rclcpp_lifecycle::State &) override
  {
    try {
      load_parameters();
      open_audio();
      validate_identity();

      auto qos = rclcpp::QoS(rclcpp::KeepLast(20));
      qos.best_effort();
      qos.durability_volatile();
      mic_publisher_ = create_publisher<AudioChunk>("/audio/mic", qos);
      if (publish_stereo_debug_) {
        mic_stereo_publisher_ = create_publisher<AudioChunk>("/audio/mic_stereo", qos);
      }

      auto state_qos = rclcpp::QoS(rclcpp::KeepLast(1));
      state_qos.reliable();
      state_qos.transient_local();
      playback_state_publisher_ =
        create_publisher<PlaybackState>("/audio/playback_state", state_qos);
      begin_playback_service_ = create_service<BeginPlayback>(
        "/audio/begin_playback", std::bind(
                                   &Xvf3800AudioNode::handle_begin_playback, this,
                                   std::placeholders::_1, std::placeholders::_2));
      fence_playback_service_ = create_service<FencePlayback>(
        "/audio/fence_playback", std::bind(
                                   &Xvf3800AudioNode::handle_fence_playback, this,
                                   std::placeholders::_1, std::placeholders::_2));
      play_pcm_server_ = rclcpp_action::create_server<PlayPcm>(
        this, "/audio/play_pcm",
        std::bind(
          &Xvf3800AudioNode::handle_play_pcm_goal, this, std::placeholders::_1,
          std::placeholders::_2),
        std::bind(&Xvf3800AudioNode::handle_play_pcm_cancel, this, std::placeholders::_1),
        std::bind(&Xvf3800AudioNode::handle_play_pcm_accepted, this, std::placeholders::_1));

      const auto state_period = std::chrono::duration<double>(1.0 / playback_state_hz_);
      playback_state_timer_ = create_wall_timer(
        std::chrono::duration_cast<std::chrono::nanoseconds>(state_period),
        std::bind(&Xvf3800AudioNode::publish_playback_state, this));

      device_session_id_ =
        actual_usb_serial_ + "-" +
        std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());

      RCLCPP_INFO(
        get_logger(),
        "XVF3800 сконфигурирован: serial=%s, capture=%s, playback=%s, "
        "%u Гц, %u capture channels, processed_channel=%u, stereo_debug=%s, "
        "frame=%u samples",
        actual_usb_serial_.c_str(), capture_device_.c_str(), playback_device_.c_str(), device_rate_,
        capture_channels_, processed_channel_, publish_stereo_debug_ ? "on" : "off",
        frame_samples_);
      return CallbackReturn::SUCCESS;
    } catch (const std::exception & error) {
      RCLCPP_ERROR(get_logger(), "configure не удался: %s", error.what());
      close_audio();
      mic_publisher_.reset();
      mic_stereo_publisher_.reset();
      return CallbackReturn::FAILURE;
    }
  }

  CallbackReturn on_activate(const rclcpp_lifecycle::State &) override
  {
    if (capture_pcm_ == nullptr || playback_pcm_ == nullptr || mic_publisher_ == nullptr) {
      RCLCPP_ERROR(get_logger(), "activate вызван до успешного configure");
      return CallbackReturn::FAILURE;
    }

    const int prepare_error = snd_pcm_prepare(capture_pcm_);
    if (prepare_error < 0) {
      RCLCPP_ERROR(get_logger(), "не удалось подготовить capture: %s", snd_strerror(prepare_error));
      return CallbackReturn::FAILURE;
    }
    const int playback_prepare_error = snd_pcm_prepare(playback_pcm_);
    if (playback_prepare_error < 0) {
      RCLCPP_ERROR(
        get_logger(), "не удалось подготовить playback: %s", snd_strerror(playback_prepare_error));
      return CallbackReturn::FAILURE;
    }
    const int link_error = snd_pcm_link(playback_pcm_, capture_pcm_);
    if (link_error < 0) {
      RCLCPP_ERROR(
        get_logger(), "не удалось синхронно связать playback/capture: %s",
        snd_strerror(link_error));
      return CallbackReturn::FAILURE;
    }
    streams_linked_ = true;

    first_sample_ = 0;
    stop_requested_.store(false);
    node_active_.store(true);
    reset_playback_runtime();
    mic_publisher_->on_activate();
    if (mic_stereo_publisher_ != nullptr) {
      mic_stereo_publisher_->on_activate();
    }
    playback_state_publisher_->on_activate();
    // XVF3800 использует синхронный full-duplex USB-тракт. Открытый, но
    // простаивающий playback приводит к EIO на capture; в тишине владелец
    // устройства обязан непрерывно подавать нулевой reference для AEC.
    playback_thread_ = std::thread(&Xvf3800AudioNode::playback_loop, this);
    for (int attempt = 0; attempt < 200; ++attempt) {
      if (
        snd_pcm_state(playback_pcm_) == SND_PCM_STATE_RUNNING &&
        snd_pcm_state(capture_pcm_) == SND_PCM_STATE_RUNNING) {
        break;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    if (snd_pcm_state(capture_pcm_) != SND_PCM_STATE_RUNNING) {
      RCLCPP_ERROR(get_logger(), "linked capture не перешёл в RUNNING после запуска playback");
      stop_streams();
      mic_publisher_->on_deactivate();
      if (mic_stereo_publisher_ != nullptr) {
        mic_stereo_publisher_->on_deactivate();
      }
      return CallbackReturn::FAILURE;
    }
    capture_thread_ = std::thread(&Xvf3800AudioNode::capture_loop, this);
    RCLCPP_INFO(get_logger(), "capture активирован, /audio/mic публикуется непрерывно");
    return CallbackReturn::SUCCESS;
  }

  CallbackReturn on_deactivate(const rclcpp_lifecycle::State &) override
  {
    node_active_.store(false);
    fence_current_playback("deactivate", 0);
    stop_streams();
    if (mic_publisher_ != nullptr) {
      mic_publisher_->on_deactivate();
    }
    if (mic_stereo_publisher_ != nullptr) {
      mic_stereo_publisher_->on_deactivate();
    }
    if (playback_state_publisher_ != nullptr) {
      playback_state_publisher_->on_deactivate();
    }
    return CallbackReturn::SUCCESS;
  }

  CallbackReturn on_cleanup(const rclcpp_lifecycle::State &) override
  {
    stop_streams();
    close_audio();
    mic_publisher_.reset();
    mic_stereo_publisher_.reset();
    playback_state_publisher_.reset();
    begin_playback_service_.reset();
    fence_playback_service_.reset();
    play_pcm_server_.reset();
    playback_state_timer_.reset();
    return CallbackReturn::SUCCESS;
  }

  CallbackReturn on_shutdown(const rclcpp_lifecycle::State &) override
  {
    stop_streams();
    close_audio();
    return CallbackReturn::SUCCESS;
  }

  void load_parameters()
  {
    capture_device_ = get_parameter("capture_device").as_string();
    playback_device_ = get_parameter("playback_device").as_string();
    expected_usb_serial_ = get_parameter("expected_usb_serial").as_string();
    frame_id_ = get_parameter("frame_id").as_string();

    const auto rate = get_parameter("device_rate").as_int();
    const auto capture_channels = get_parameter("capture_channels").as_int();
    const auto playback_channels = get_parameter("playback_channels").as_int();
    const auto processed_channel = get_parameter("processed_channel").as_int();
    publish_stereo_debug_ = get_parameter("publish_stereo_debug").as_bool();
    const auto frame_ms = get_parameter("frame_ms").as_int();
    const auto latency_us = get_parameter("alsa_latency_us").as_int();
    const auto max_playback_queue_ms = get_parameter("max_playback_queue_ms").as_int();
    const auto max_pcm_block_ms = get_parameter("max_pcm_block_ms").as_int();
    const auto playback_fade_ms = get_parameter("playback_fade_ms").as_int();
    const auto playback_state_hz = get_parameter("playback_state_hz").as_double();
    const auto format = get_parameter("sample_format").as_string();

    if (format != "S16_LE") {
      throw std::invalid_argument("на первом этапе поддерживается только sample_format=S16_LE");
    }
    if (
      rate <= 0 || capture_channels <= 0 || playback_channels <= 0 || frame_ms <= 0 ||
      latency_us <= 0) {
      throw std::invalid_argument("частота, каналы, frame_ms и alsa_latency_us должны быть > 0");
    }
    if (
      max_playback_queue_ms <= 0 || max_pcm_block_ms <= 0 || playback_fade_ms < 0 ||
      playback_state_hz <= 0.0) {
      throw std::invalid_argument("параметры playback-очереди имеют недопустимое значение");
    }
    if (processed_channel < 0 || processed_channel >= capture_channels) {
      throw std::invalid_argument("processed_channel вне диапазона capture_channels");
    }
    if ((rate * frame_ms) % 1000 != 0) {
      throw std::invalid_argument("device_rate * frame_ms должен давать целое число сэмплов");
    }

    device_rate_ = static_cast<unsigned int>(rate);
    capture_channels_ = static_cast<unsigned int>(capture_channels);
    playback_channels_ = static_cast<unsigned int>(playback_channels);
    processed_channel_ = static_cast<unsigned int>(processed_channel);
    frame_ms_ = static_cast<unsigned int>(frame_ms);
    latency_us_ = static_cast<unsigned int>(latency_us);
    frame_samples_ = device_rate_ * frame_ms_ / 1000U;
    max_playback_queue_samples_ =
      device_rate_ * static_cast<unsigned int>(max_playback_queue_ms) / 1000U;
    max_pcm_block_samples_ = device_rate_ * static_cast<unsigned int>(max_pcm_block_ms) / 1000U;
    max_playback_fade_ms_ = static_cast<unsigned int>(playback_fade_ms);
    playback_state_hz_ = playback_state_hz;
  }

  static void check_alsa(int result, const std::string & operation)
  {
    if (result < 0) {
      throw std::runtime_error(operation + ": " + snd_strerror(result));
    }
  }

  void configure_pcm(snd_pcm_t * pcm, unsigned int channels, const std::string & description) const
  {
    check_alsa(
      snd_pcm_set_params(
        pcm, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED, channels, device_rate_, 0,
        latency_us_),
      "настройка " + description);

    snd_pcm_hw_params_t * parameters = nullptr;
    snd_pcm_hw_params_alloca(&parameters);
    check_alsa(snd_pcm_hw_params_current(pcm, parameters), "чтение параметров " + description);

    unsigned int actual_rate = 0;
    unsigned int actual_channels = 0;
    int direction = 0;
    check_alsa(
      snd_pcm_hw_params_get_rate(parameters, &actual_rate, &direction),
      "чтение частоты " + description);
    check_alsa(
      snd_pcm_hw_params_get_channels(parameters, &actual_channels),
      "чтение каналов " + description);
    if (actual_rate != device_rate_ || actual_channels != channels) {
      std::ostringstream message;
      message << description << " открылся с неожиданными параметрами: " << actual_rate << " Гц, "
              << actual_channels << " каналов";
      throw std::runtime_error(message.str());
    }
  }

  void open_audio()
  {
    check_alsa(
      snd_pcm_open(&capture_pcm_, capture_device_.c_str(), SND_PCM_STREAM_CAPTURE, 0),
      "открытие capture " + capture_device_);
    try {
      configure_pcm(capture_pcm_, capture_channels_, "capture");
      check_alsa(
        snd_pcm_open(&playback_pcm_, playback_device_.c_str(), SND_PCM_STREAM_PLAYBACK, 0),
        "открытие playback " + playback_device_);
      configure_pcm(playback_pcm_, playback_channels_, "playback");

      const int capture_card = pcm_card_index(capture_pcm_);
      const int playback_card = pcm_card_index(playback_pcm_);
      if (capture_card != playback_card) {
        throw std::runtime_error("capture и playback принадлежат разным ALSA-картам");
      }
      card_index_ = capture_card;
    } catch (...) {
      close_audio();
      throw;
    }
  }

  static int pcm_card_index(snd_pcm_t * pcm)
  {
    snd_pcm_info_t * info = nullptr;
    snd_pcm_info_alloca(&info);
    check_alsa(snd_pcm_info(pcm, info), "чтение ALSA PCM info");
    return snd_pcm_info_get_card(info);
  }

  static std::optional<std::string> read_line(const std::filesystem::path & path)
  {
    std::ifstream input(path);
    std::string value;
    if (!input || !std::getline(input, value)) {
      return std::nullopt;
    }
    return value;
  }

  static std::optional<std::string> usb_serial_for_card(int card_index)
  {
    std::error_code error;
    auto path = std::filesystem::canonical(
      "/sys/class/sound/card" + std::to_string(card_index) + "/device", error);
    if (error) {
      return std::nullopt;
    }

    while (path.has_parent_path() && path != path.root_path()) {
      if (auto serial = read_line(path / "serial")) {
        return serial;
      }
      path = path.parent_path();
    }
    return std::nullopt;
  }

  void validate_identity()
  {
    const auto actual_serial = usb_serial_for_card(card_index_);
    if (!actual_serial) {
      throw std::runtime_error("ALSA-карта не предоставляет USB serial через sysfs");
    }
    actual_usb_serial_ = *actual_serial;
    if (!expected_usb_serial_.empty() && actual_usb_serial_ != expected_usb_serial_) {
      throw std::runtime_error(
        "USB serial не совпадает: ожидался " + expected_usb_serial_ + ", найден " +
        actual_usb_serial_);
    }
  }

  void capture_loop()
  {
    std::vector<std::int16_t> interleaved(frame_samples_ * capture_channels_);
    while (!stop_requested_.load()) {
      snd_pcm_sframes_t frames_read =
        snd_pcm_readi(capture_pcm_, interleaved.data(), frame_samples_);
      if (frames_read < 0) {
        if (stop_requested_.load()) {
          break;
        }
        const int recovery = snd_pcm_recover(capture_pcm_, static_cast<int>(frames_read), 1);
        first_sample_ += frame_samples_;
        if (recovery < 0) {
          RCLCPP_ERROR_THROTTLE(
            get_logger(), *get_clock(), 5000, "ALSA capture error: %s", snd_strerror(recovery));
          std::this_thread::sleep_for(std::chrono::milliseconds(frame_ms_));
        } else {
          RCLCPP_WARN_THROTTLE(
            get_logger(), *get_clock(), 5000,
            "ALSA XRUN восстановлен; first_sample содержит разрыв");
        }
        continue;
      }
      if (frames_read == 0) {
        continue;
      }

      auto message = AudioChunk();
      const auto completed_at = now();
      const auto duration = rclcpp::Duration::from_seconds(
        static_cast<double>(frames_read) / static_cast<double>(device_rate_));
      const auto first_sample = first_sample_;

      if (mic_stereo_publisher_ != nullptr && mic_stereo_publisher_->is_activated()) {
        auto stereo_message = AudioChunk();
        stereo_message.header.stamp = completed_at - duration;
        stereo_message.header.frame_id = frame_id_;
        stereo_message.device_session_id = device_session_id_;
        stereo_message.sample_rate = device_rate_;
        stereo_message.channels = static_cast<std::uint16_t>(capture_channels_);
        stereo_message.first_sample = first_sample;
        const auto sample_count =
          static_cast<std::size_t>(frames_read) * static_cast<std::size_t>(capture_channels_);
        stereo_message.data.assign(interleaved.begin(), interleaved.begin() + sample_count);
        mic_stereo_publisher_->publish(std::move(stereo_message));
      }

      message.header.stamp = completed_at - duration;
      message.header.frame_id = frame_id_;
      message.device_session_id = device_session_id_;
      message.sample_rate = device_rate_;
      message.channels = 1;
      message.first_sample = first_sample;
      message.data.reserve(static_cast<std::size_t>(frames_read));
      for (snd_pcm_sframes_t frame = 0; frame < frames_read; ++frame) {
        const auto offset =
          static_cast<std::size_t>(frame) * capture_channels_ + processed_channel_;
        message.data.push_back(interleaved[offset]);
      }
      first_sample_ += static_cast<std::uint64_t>(frames_read);
      mic_publisher_->publish(std::move(message));
    }
  }

  void handle_begin_playback(
    const std::shared_ptr<BeginPlayback::Request> request,
    std::shared_ptr<BeginPlayback::Response> response)
  {
    response->accepted = false;
    response->device_session_id = device_session_id_;
    response->max_pcm_samples = std::min(max_pcm_block_samples_, max_playback_queue_samples_);

    if (!node_active_.load()) {
      response->reason = "audio_owner_not_active";
      return;
    }
    if (request->goal_id.empty()) {
      response->reason = "goal_id_empty";
      return;
    }
    if (
      !request->expected_device_session_id.empty() &&
      request->expected_device_session_id != device_session_id_) {
      response->reason = "device_session_changed";
      return;
    }
    if (request->sample_rate != device_rate_ || request->channels != 1U) {
      response->reason = "expected_mono_pcm_at_device_rate";
      return;
    }

    std::lock_guard<std::mutex> lock(playback_mutex_);
    refresh_presented_locked(last_device_delay_frames_);
    if (playback_stream_open_ && presented_samples_ < submitted_samples_) {
      response->reason = "previous_stream_still_playing";
      return;
    }

    ++playback_stream_id_;
    ++playback_generation_;
    playback_goal_id_ = request->goal_id;
    playback_reason_ = "begin";
    playback_stream_open_ = true;
    playback_queue_.clear();
    fade_queue_.clear();
    timeline_.clear();
    submitted_samples_ = 0;
    rendered_samples_ = 0;
    presented_samples_ = 0;

    response->accepted = true;
    response->stream_id = playback_stream_id_;
    response->generation = playback_generation_;
    response->reason = "accepted";
    playback_cv_.notify_all();
  }

  void handle_fence_playback(
    const std::shared_ptr<FencePlayback::Request> request,
    std::shared_ptr<FencePlayback::Response> response)
  {
    response->accepted = false;
    {
      std::lock_guard<std::mutex> lock(playback_mutex_);
      response->resulting_generation = playback_generation_;
      if (request->device_session_id != device_session_id_) {
        response->reason = "device_session_changed";
        return;
      }
      if (request->stream_id != playback_stream_id_) {
        response->reason = "stream_changed";
        return;
      }
      if (request->expected_generation != playback_generation_) {
        response->reason = "generation_changed";
        return;
      }
    }

    response->resulting_generation = fence_current_playback(request->reason, request->fade_ms);
    response->accepted = true;
    response->reason = "accepted";
  }

  rclcpp_action::GoalResponse handle_play_pcm_goal(
    const rclcpp_action::GoalUUID &, std::shared_ptr<const PlayPcm::Goal> goal)
  {
    if (
      !node_active_.load() || goal->pcm.empty() ||
      goal->pcm.size() > std::min(max_pcm_block_samples_, max_playback_queue_samples_) ||
      goal->sample_rate != device_rate_ || goal->channels != 1U) {
      return rclcpp_action::GoalResponse::REJECT;
    }

    std::lock_guard<std::mutex> lock(playback_mutex_);
    if (
      !playback_stream_open_ || goal->device_session_id != device_session_id_ ||
      goal->stream_id != playback_stream_id_ || goal->generation != playback_generation_) {
      return rclcpp_action::GoalResponse::REJECT;
    }
    return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
  }

  rclcpp_action::CancelResponse handle_play_pcm_cancel(const std::shared_ptr<PlayPcmGoalHandle>)
  {
    playback_cv_.notify_all();
    return rclcpp_action::CancelResponse::ACCEPT;
  }

  void handle_play_pcm_accepted(const std::shared_ptr<PlayPcmGoalHandle> goal_handle)
  {
    reap_action_threads();
    auto finished = std::make_shared<std::atomic<bool>>(false);
    std::lock_guard<std::mutex> lock(action_threads_mutex_);
    action_threads_.push_back(ActionWorker{
      std::thread([this, goal_handle, finished]() {
        execute_play_pcm(goal_handle);
        finished->store(true);
      }),
      finished});
  }

  void execute_play_pcm(const std::shared_ptr<PlayPcmGoalHandle> goal_handle)
  {
    const auto goal = goal_handle->get_goal();
    auto result = std::make_shared<PlayPcm::Result>();
    std::unique_lock<std::mutex> lock(playback_mutex_);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);

    while (node_active_.load() && playback_stream_open_ &&
           goal->device_session_id == device_session_id_ &&
           goal->stream_id == playback_stream_id_ && goal->generation == playback_generation_ &&
           playback_queue_.size() + goal->pcm.size() > max_playback_queue_samples_ &&
           !goal_handle->is_canceling()) {
      if (playback_cv_.wait_until(lock, deadline) == std::cv_status::timeout) {
        result->status = PlayPcm::Result::STATUS_FAILED;
        result->reason = "queue_wait_timeout";
        lock.unlock();
        goal_handle->abort(result);
        return;
      }
    }

    if (goal_handle->is_canceling()) {
      result->status = PlayPcm::Result::STATUS_CANCELLED;
      result->reason = "action_cancelled";
      lock.unlock();
      goal_handle->canceled(result);
      return;
    }
    if (
      !node_active_.load() || !playback_stream_open_ ||
      goal->device_session_id != device_session_id_ || goal->stream_id != playback_stream_id_ ||
      goal->generation != playback_generation_) {
      result->status = PlayPcm::Result::STATUS_REJECTED;
      result->reason = "stale_stream";
      lock.unlock();
      goal_handle->abort(result);
      return;
    }

    playback_queue_.insert(playback_queue_.end(), goal->pcm.begin(), goal->pcm.end());
    submitted_samples_ += goal->pcm.size();
    result->status = PlayPcm::Result::STATUS_ACCEPTED;
    result->submitted_samples = submitted_samples_;
    result->reason = "accepted";
    const auto feedback = std::make_shared<PlayPcm::Feedback>();
    feedback->presented_samples_estimate = presented_samples_;
    feedback->buffered_samples = static_cast<std::uint32_t>(playback_queue_.size());
    lock.unlock();
    playback_cv_.notify_all();
    goal_handle->publish_feedback(feedback);
    goal_handle->succeed(result);
  }

  std::uint64_t fence_current_playback(const std::string & reason, std::uint32_t requested_fade_ms)
  {
    std::lock_guard<std::mutex> lock(playback_mutex_);
    ++playback_generation_;
    playback_stream_open_ = false;
    playback_reason_ = reason.empty() ? "fenced" : reason;
    playback_queue_.clear();
    timeline_.clear();
    fade_queue_.clear();

    const auto fade_ms = std::min(requested_fade_ms, max_playback_fade_ms_);
    const auto fade_samples =
      static_cast<std::size_t>(device_rate_) * static_cast<std::size_t>(fade_ms) / 1000U;
    if (fade_samples > 0U && last_output_sample_ != 0) {
      for (std::size_t index = 0; index < fade_samples; ++index) {
        const double phase = static_cast<double>(index) / static_cast<double>(fade_samples);
        const double gain = 0.5 * (1.0 + std::cos(3.14159265358979323846 * phase));
        fade_queue_.push_back(static_cast<std::int16_t>(last_output_sample_ * gain));
      }
    }
    playback_cv_.notify_all();
    return playback_generation_;
  }

  PlaybackBlock next_playback_block()
  {
    PlaybackBlock block;
    block.mono.assign(frame_samples_, 0);
    std::lock_guard<std::mutex> lock(playback_mutex_);
    block.stream_id = playback_stream_id_;
    block.generation = playback_generation_;

    std::size_t run_start = 0;
    std::size_t run_count = 0;
    for (std::size_t index = 0; index < frame_samples_; ++index) {
      if (!fade_queue_.empty()) {
        block.mono[index] = fade_queue_.front();
        fade_queue_.pop_front();
      } else if (playback_stream_open_ && !playback_queue_.empty()) {
        block.mono[index] = playback_queue_.front();
        playback_queue_.pop_front();
        ++rendered_samples_;
        if (run_count == 0U) {
          run_start = index;
        }
        ++run_count;
      } else if (run_count > 0U) {
        block.audio_runs.emplace_back(run_start, run_count);
        run_count = 0;
      }
      last_output_sample_ = block.mono[index];
    }
    if (run_count > 0U) {
      block.audio_runs.emplace_back(run_start, run_count);
    }
    playback_cv_.notify_all();
    return block;
  }

  void commit_playback_frames(
    const PlaybackBlock & block, std::size_t block_offset, std::size_t count)
  {
    std::lock_guard<std::mutex> lock(playback_mutex_);
    const auto absolute_first = output_frames_written_;
    if (block.stream_id == playback_stream_id_ && block.generation == playback_generation_) {
      const auto write_end = block_offset + count;
      for (const auto & run : block.audio_runs) {
        const auto run_end = run.first + run.second;
        const auto overlap_start = std::max(run.first, block_offset);
        const auto overlap_end = std::min(run_end, write_end);
        if (overlap_start < overlap_end) {
          timeline_.push_back(TimelineSegment{
            absolute_first + overlap_start - block_offset, overlap_end - overlap_start});
        }
      }
    }
    output_frames_written_ += count;
  }

  void refresh_presented_locked(snd_pcm_sframes_t device_delay)
  {
    const auto nonnegative_delay = std::max<snd_pcm_sframes_t>(0, device_delay);
    const auto delay = static_cast<std::uint64_t>(nonnegative_delay);
    const auto presented_output =
      output_frames_written_ > delay ? output_frames_written_ - delay : 0U;

    while (!timeline_.empty()) {
      auto & segment = timeline_.front();
      if (presented_output <= segment.output_first) {
        break;
      }
      const auto available =
        std::min(segment.sample_count, presented_output - segment.output_first);
      presented_samples_ += available;
      segment.output_first += available;
      segment.sample_count -= available;
      if (segment.sample_count == 0U) {
        timeline_.pop_front();
      } else {
        break;
      }
    }
    presented_samples_ = std::min(presented_samples_, submitted_samples_);
  }

  PlaybackSnapshot playback_snapshot()
  {
    snd_pcm_sframes_t delay = 0;
    if (playback_pcm_ != nullptr && snd_pcm_delay(playback_pcm_, &delay) >= 0) {
      last_device_delay_frames_ = delay;
    }

    std::lock_guard<std::mutex> lock(playback_mutex_);
    refresh_presented_locked(last_device_delay_frames_);
    PlaybackSnapshot snapshot;
    snapshot.goal_id = playback_goal_id_;
    snapshot.reason = playback_reason_;
    snapshot.stream_id = playback_stream_id_;
    snapshot.generation = playback_generation_;
    snapshot.submitted_samples = submitted_samples_;
    snapshot.presented_samples = presented_samples_;
    // После fence старые PCM удалены, но короткий fade ещё пишется в ALSA.
    // Не сообщать FSM об остановке до исчерпания этого хвоста.
    snapshot.buffered_samples =
      static_cast<std::uint32_t>(playback_queue_.size() + fade_queue_.size());

    if (!node_active_.load()) {
      snapshot.state = PlaybackState::STATE_ERROR;
    } else if (!playback_stream_open_) {
      snapshot.state =
        playback_reason_.empty() ? PlaybackState::STATE_IDLE : PlaybackState::STATE_FENCED;
    } else if (submitted_samples_ == 0U || presented_samples_ == 0U) {
      snapshot.state = PlaybackState::STATE_BUFFERING;
    } else if (presented_samples_ >= submitted_samples_ && playback_queue_.empty()) {
      snapshot.state = PlaybackState::STATE_IDLE;
    } else if (playback_queue_.empty() && rendered_samples_ >= submitted_samples_) {
      snapshot.state = PlaybackState::STATE_DRAINING;
    } else {
      snapshot.state = PlaybackState::STATE_PLAYING;
    }
    return snapshot;
  }

  void publish_playback_state()
  {
    if (playback_state_publisher_ == nullptr || !playback_state_publisher_->is_activated()) {
      return;
    }
    const auto snapshot = playback_snapshot();
    PlaybackState message;
    message.stamp = now();
    message.device_session_id = device_session_id_;
    message.goal_id = snapshot.goal_id;
    message.stream_id = snapshot.stream_id;
    message.generation = snapshot.generation;
    message.state = snapshot.state;
    message.submitted_samples = snapshot.submitted_samples;
    message.presented_samples_estimate = snapshot.presented_samples;
    message.buffered_samples = snapshot.buffered_samples;
    message.uncertainty_ms = static_cast<float>(frame_ms_);
    message.reason = snapshot.reason;
    playback_state_publisher_->publish(message);
  }

  void reset_playback_runtime()
  {
    std::lock_guard<std::mutex> lock(playback_mutex_);
    playback_goal_id_.clear();
    playback_reason_.clear();
    playback_stream_open_ = false;
    playback_queue_.clear();
    fade_queue_.clear();
    timeline_.clear();
    submitted_samples_ = 0;
    rendered_samples_ = 0;
    presented_samples_ = 0;
    output_frames_written_ = 0;
    last_device_delay_frames_ = 0;
    last_output_sample_ = 0;
  }

  void playback_loop()
  {
    std::vector<std::int16_t> interleaved(frame_samples_ * playback_channels_, 0);
    while (!stop_requested_.load()) {
      auto block = next_playback_block();
      for (std::size_t frame = 0; frame < frame_samples_; ++frame) {
        for (std::size_t channel = 0; channel < playback_channels_; ++channel) {
          interleaved[frame * playback_channels_ + channel] = block.mono[frame];
        }
      }
      snd_pcm_uframes_t written = 0;
      while (written < frame_samples_ && !stop_requested_.load()) {
        {
          std::lock_guard<std::mutex> lock(playback_mutex_);
          if (block.generation != playback_generation_ || block.stream_id != playback_stream_id_) {
            std::fill(interleaved.begin() + written * playback_channels_, interleaved.end(), 0);
            block.audio_runs.clear();
          }
        }
        const auto * data = interleaved.data() + written * playback_channels_;
        const auto remaining = frame_samples_ - written;
        const snd_pcm_sframes_t result = snd_pcm_writei(playback_pcm_, data, remaining);
        if (result < 0) {
          if (stop_requested_.load()) {
            return;
          }
          const int recovery = snd_pcm_recover(playback_pcm_, static_cast<int>(result), 1);
          if (recovery < 0) {
            RCLCPP_ERROR_THROTTLE(
              get_logger(), *get_clock(), 5000, "ALSA playback error: %s", snd_strerror(recovery));
            std::this_thread::sleep_for(std::chrono::milliseconds(frame_ms_));
          } else {
            RCLCPP_WARN_THROTTLE(
              get_logger(), *get_clock(), 5000, "ALSA playback XRUN восстановлен");
          }
          written = 0;
          continue;
        }
        commit_playback_frames(
          block, static_cast<std::size_t>(written), static_cast<std::size_t>(result));
        written += static_cast<snd_pcm_uframes_t>(result);
      }
    }
  }

  void stop_streams()
  {
    node_active_.store(false);
    stop_requested_.store(true);
    playback_cv_.notify_all();
    if (capture_pcm_ != nullptr) {
      snd_pcm_drop(capture_pcm_);
    }
    if (playback_pcm_ != nullptr) {
      snd_pcm_drop(playback_pcm_);
    }
    if (capture_thread_.joinable()) {
      capture_thread_.join();
    }
    if (playback_thread_.joinable()) {
      playback_thread_.join();
    }
    if (streams_linked_ && playback_pcm_ != nullptr) {
      snd_pcm_unlink(playback_pcm_);
      streams_linked_ = false;
    }
    join_action_threads();
  }

  void join_action_threads()
  {
    std::vector<ActionWorker> workers;
    {
      std::lock_guard<std::mutex> lock(action_threads_mutex_);
      workers.swap(action_threads_);
    }
    for (auto & worker : workers) {
      if (worker.thread.joinable()) {
        worker.thread.join();
      }
    }
  }

  void reap_action_threads()
  {
    std::vector<std::thread> finished_threads;
    {
      std::lock_guard<std::mutex> lock(action_threads_mutex_);
      auto iterator = action_threads_.begin();
      while (iterator != action_threads_.end()) {
        if (iterator->finished->load()) {
          finished_threads.push_back(std::move(iterator->thread));
          iterator = action_threads_.erase(iterator);
        } else {
          ++iterator;
        }
      }
    }
    for (auto & thread : finished_threads) {
      if (thread.joinable()) {
        thread.join();
      }
    }
  }

  void close_audio()
  {
    stop_streams();
    if (playback_pcm_ != nullptr) {
      snd_pcm_close(playback_pcm_);
      playback_pcm_ = nullptr;
    }
    if (capture_pcm_ != nullptr) {
      snd_pcm_close(capture_pcm_);
      capture_pcm_ = nullptr;
    }
    card_index_ = -1;
    actual_usb_serial_.clear();
  }

  std::string expected_usb_serial_;
  std::string actual_usb_serial_;
  std::string capture_device_;
  std::string playback_device_;
  std::string frame_id_;
  unsigned int device_rate_{16000};
  unsigned int capture_channels_{2};
  unsigned int playback_channels_{2};
  unsigned int processed_channel_{0};
  bool publish_stereo_debug_{false};
  unsigned int frame_ms_{16};
  unsigned int latency_us_{48000};
  unsigned int frame_samples_{256};
  unsigned int max_playback_queue_samples_{9600};
  unsigned int max_pcm_block_samples_{32000};
  unsigned int max_playback_fade_ms_{20};
  double playback_state_hz_{20.0};
  int card_index_{-1};

  snd_pcm_t * capture_pcm_{nullptr};
  snd_pcm_t * playback_pcm_{nullptr};
  std::atomic<bool> stop_requested_{true};
  std::atomic<bool> node_active_{false};
  std::thread capture_thread_;
  std::thread playback_thread_;
  std::uint64_t first_sample_{0};
  bool streams_linked_{false};
  rclcpp_lifecycle::LifecyclePublisher<AudioChunk>::SharedPtr mic_publisher_;
  rclcpp_lifecycle::LifecyclePublisher<AudioChunk>::SharedPtr mic_stereo_publisher_;
  rclcpp_lifecycle::LifecyclePublisher<PlaybackState>::SharedPtr playback_state_publisher_;
  rclcpp::Service<BeginPlayback>::SharedPtr begin_playback_service_;
  rclcpp::Service<FencePlayback>::SharedPtr fence_playback_service_;
  rclcpp_action::Server<PlayPcm>::SharedPtr play_pcm_server_;
  rclcpp::TimerBase::SharedPtr playback_state_timer_;

  std::string device_session_id_;
  std::mutex playback_mutex_;
  std::condition_variable playback_cv_;
  std::deque<std::int16_t> playback_queue_;
  std::deque<std::int16_t> fade_queue_;
  std::deque<TimelineSegment> timeline_;
  std::string playback_goal_id_;
  std::string playback_reason_;
  std::uint64_t playback_stream_id_{0};
  std::uint64_t playback_generation_{0};
  std::uint64_t submitted_samples_{0};
  std::uint64_t rendered_samples_{0};
  std::uint64_t presented_samples_{0};
  std::uint64_t output_frames_written_{0};
  snd_pcm_sframes_t last_device_delay_frames_{0};
  std::int16_t last_output_sample_{0};
  bool playback_stream_open_{false};

  std::mutex action_threads_mutex_;
  std::vector<ActionWorker> action_threads_;
};

}  // namespace guide_robot_audio

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto node = std::make_shared<guide_robot_audio::Xvf3800AudioNode>();
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(node->get_node_base_interface());
  executor.spin();
  rclcpp::shutdown();
  return 0;
}

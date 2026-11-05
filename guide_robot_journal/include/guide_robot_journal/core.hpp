#pragma once
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace guide_robot_journal {
constexpr size_t kFields = 16;
struct Field {
  char key[32]{};
  char text[96]{};
  double number{};
  bool numeric{};
};
struct Compact {
  char text[256]{}, component[96]{}, hardware[64]{};
  uint8_t level{};
  uint8_t count{};
  bool truncated{};
  std::array<Field, kFields> fields{};
};
enum class Encoding : uint8_t { Compact, Cdr, Arrival };
struct Header {
  char source[48]{}, topic[192]{}, ros_type[96]{}, event[48]{}, instance_id[48]{};
  uint64_t sequence{}, snapshot{}, received{}, unchanged{};
  int64_t utc_ns{}, monotonic_ns{}, ros_ns{}, source_ns{}, last_received_ns{};
  double rate_hz{};
  uint8_t freshness{};  // 0 never received, 1 fresh, 2 stale, 3 timeout disabled
  Encoding encoding{Encoding::Arrival};
  bool payload_available{}, truncated{}, source_stamp_available{};
};
struct Record {
  Header header{};
  std::vector<uint8_t> data;
  size_t used{};
  explicit Record(size_t capacity) : data(capacity) {}
  bool copy(const Record & other);
};
// One executor is the sole producer; the writer thread is the sole consumer.
class Queue
{
public:
  Queue(size_t slots, size_t bytes);
  bool push(const Record & record);
  const Record * front() const;
  void pop();
  size_t size() const;
  size_t high_water() const { return high_water_.load(); }

private:
  std::vector<Record> slots_;
  alignas(64) std::atomic<size_t> head_{0};
  alignas(64) std::atomic<size_t> tail_{0};
  std::atomic<size_t> high_water_{0};
};
struct Source {
  std::string id, topic, adapter, type;
  std::string reliability{"best_effort"}, durability{"volatile"};
  size_t depth{4}, max_bytes{65536};
  double stale_s{0};
  bool stream{false}, events{true};
  std::vector<std::string> event_fields;
};
struct Config {
  std::string directory{"~/ros2_ws/log/robot-journal"};
  double snapshot_s{5}, freshness_s{1}, flush_s{1}, sync_s{10}, rotation_s{3600},
    retention_s{604800}, maintenance_s{60}, retry_s{5};
  size_t batch_bytes{65536}, file_bytes{20 * 1024 * 1024}, max_files{168},
    total_bytes{1024ULL * 1024 * 1024}, reserve_bytes{128 * 1024 * 1024},
    max_record_bytes{128 * 1024}, max_sources{32}, max_components{32}, max_statuses_per_message{64},
    snapshot_slots{16}, event_slots{64}, memory_budget{2 * 1024 * 1024};
  std::vector<Source> sources;
  size_t snapshot_capacity() const;
  size_t owned_memory_bound() const;
  void validate() const;
};
Config load_config(const std::string & path);
int64_t steady_ns();
int64_t utc_ns();
bool text(char * dest, size_t capacity, const std::string & value);
std::string json_line(const Record & record, size_t max_bytes);
struct Counters {
  std::atomic<uint64_t> dropped_snapshots{0}, dropped_events{0}, disk_drops{0}, input_limits{0},
    write_errors{0}, deleted_files{0}, written_records{0}, retained_bytes{0}, bytes_written{0};
  std::atomic<bool> disk_ok{false};
  std::atomic<int> last_errno{0};
  std::atomic<uint64_t> max_callback_ns{0};
};
// Filesystem implementation lives entirely on the writer thread (also unit-testable).
class Store
{
public:
  Store(const Config & config, Counters & counters);
  ~Store();
  void open();
  void append(const std::string & bytes);
  void sync();
  void maintenance();
  void finish();
  void abandon() noexcept;

private:
  struct File {
    std::filesystem::path path;
    uint64_t bytes;
    int64_t modified_ns;
  };
  Config config_;
  Counters & counters_;
  int lock_fd_{-1}, fd_{-1};
  std::filesystem::path active_;
  uint64_t active_bytes_{0}, serial_{0};
  int64_t opened_ns_{0};
  std::vector<File> files_;
  void scan(bool recover);
  void create();
  void rotate();
  void trim(uint64_t incoming);
  uint64_t total() const;
};
class Writer
{
public:
  Writer(const Config & config, Counters & counters);
  ~Writer();
  bool push(const Record & record, bool event);
  void stop();
  Queue snapshots, events;

private:
  Config config_;
  Counters & counters_;
  std::atomic<bool> stopping_{false};
  std::mutex mutex_;
  std::condition_variable cv_;
  std::thread thread_;
  void run();
};
}  // namespace guide_robot_journal

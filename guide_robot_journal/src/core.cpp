#include "guide_robot_journal/core.hpp"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <system_error>

namespace guide_robot_journal {
int64_t steady_ns()
{
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
           std::chrono::steady_clock::now().time_since_epoch())
    .count();
}
int64_t utc_ns()
{
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
           std::chrono::system_clock::now().time_since_epoch())
    .count();
}
bool text(char * dest, size_t capacity, const std::string & value)
{
  const auto nul = value.find('\0');
  const size_t n = std::min(capacity - 1, nul == std::string::npos ? value.size() : nul);
  std::memcpy(dest, value.data(), n);
  dest[n] = 0;
  return n != value.size();
}
bool Record::copy(const Record & other)
{
  if (other.used > data.size()) return false;
  header = other.header;
  used = other.used;
  if (used) std::memcpy(data.data(), other.data.data(), used);
  return true;
}
Queue::Queue(size_t slots, size_t bytes)
{
  slots_.reserve(slots + 1);
  for (size_t i = 0; i <= slots; ++i) slots_.emplace_back(bytes);
}
bool Queue::push(const Record & record)
{
  auto head = head_.load(std::memory_order_relaxed), next = (head + 1) % slots_.size();
  if (next == tail_.load(std::memory_order_acquire) || !slots_[head].copy(record)) return false;
  head_.store(next, std::memory_order_release);
  const auto used = size();
  if (used > high_water_.load()) high_water_.store(used);
  return true;
}
const Record * Queue::front() const
{
  auto tail = tail_.load(std::memory_order_relaxed);
  return tail == head_.load(std::memory_order_acquire) ? nullptr : &slots_[tail];
}
void Queue::pop()
{
  tail_.store(
    (tail_.load(std::memory_order_relaxed) + 1) % slots_.size(), std::memory_order_release);
}
size_t Queue::size() const
{
  auto head = head_.load(std::memory_order_acquire), tail = tail_.load(std::memory_order_acquire);
  return (head + slots_.size() - tail) % slots_.size();
}
namespace {
void quoted(std::ostream & out, const char * value)
{
  out << '"';
  const auto * p = reinterpret_cast<const unsigned char *>(value);
  while (*p) {
    if (*p == '"' || *p == '\\') {
      out << '\\' << char(*p++);
      continue;
    }
    if (*p < 32) {
      out << "\\u00" << std::hex << std::setw(2) << std::setfill('0') << unsigned(*p++) << std::dec;
      continue;
    }
    if (*p < 128) {
      out << char(*p++);
      continue;
    }
    size_t n = (*p >= 0xc2 && *p <= 0xdf)   ? 2
               : (*p >= 0xe0 && *p <= 0xef) ? 3
               : (*p >= 0xf0 && *p <= 0xf4) ? 4
                                            : 0;
    uint32_t code = n ? (*p & ((1u << (7 - n)) - 1)) : 0;
    bool valid = n != 0;
    for (size_t i = 1; i < n; ++i) {
      if (!p[i] || (p[i] & 0xc0) != 0x80) {
        valid = false;
        break;
      }
      code = (code << 6) | (p[i] & 63);
    }
    valid = valid && (n != 3 || code >= 0x800) && (n != 4 || code >= 0x10000) && code <= 0x10ffff &&
            !(code >= 0xd800 && code <= 0xdfff);
    if (valid) {
      out.write(reinterpret_cast<const char *>(p), n);
      p += n;
    } else {
      out << "\\ufffd";
      ++p;
    }
  }
  out << '"';
}
void number(std::ostream & out, double value)
{
  if (std::isfinite(value))
    out << std::setprecision(17) << value;
  else
    out << "null";
}
void base64(std::ostream & out, const uint8_t * data, size_t n)
{
  constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  for (size_t i = 0; i < n; i += 3) {
    uint32_t v = uint32_t(data[i]) << 16;
    if (i + 1 < n) v |= uint32_t(data[i + 1]) << 8;
    if (i + 2 < n) v |= data[i + 2];
    out << alphabet[(v >> 18) & 63] << alphabet[(v >> 12) & 63]
        << (i + 1 < n ? alphabet[(v >> 6) & 63] : '=') << (i + 2 < n ? alphabet[v & 63] : '=');
  }
}
}  // namespace
std::string json_line(const Record & r, size_t max_bytes)
{
  const auto & h = r.header;
  std::ostringstream out;
  out << "{\"schema_version\":1,\"type\":";
  quoted(out, h.event[0] ? "event" : (h.snapshot ? "snapshot" : "sample"));
  out << ",\"sequence\":" << h.sequence << ",\"snapshot_id\":" << h.snapshot
      << ",\"utc_ns\":" << h.utc_ns << ",\"monotonic_ns\":" << h.monotonic_ns
      << ",\"ros_ns\":" << h.ros_ns << ",\"source_stamp_ns\":";
  if (h.source_stamp_available)
    out << h.source_ns;
  else
    out << "null";
  out << ",\"received_monotonic_ns\":" << h.last_received_ns << ",\"source\":";
  quoted(out, h.source);
  out << ",\"instance_id\":";
  quoted(out, h.instance_id);
  out << ",\"topic\":";
  quoted(out, h.topic);
  out << ",\"ros_type\":";
  quoted(out, h.ros_type);
  out << ",\"event\":";
  quoted(out, h.event);
  out << ",\"received\":" << h.received << ",\"unchanged_messages\":" << h.unchanged
      << ",\"rate_hz\":";
  number(out, h.rate_hz);
  out << ",\"freshness\":";
  quoted(
    out, h.freshness == 0   ? "never"
         : h.freshness == 1 ? "fresh"
         : h.freshness == 2 ? "stale"
                            : "timeout_disabled");
  out << ",\"payload_available\":" << (h.payload_available ? "true" : "false")
      << ",\"truncated\":" << (h.truncated ? "true" : "false");
  if (h.encoding == Encoding::Cdr && h.payload_available) {
    out << ",\"encoding\":\"cdr-base64\",\"byte_length\":" << r.used << ",\"data\":\"";
    base64(out, r.data.data(), r.used);
    out << '"';
  } else if (h.encoding == Encoding::Compact && r.used == sizeof(Compact)) {
    Compact data;
    std::memcpy(&data, r.data.data(), sizeof(data));
    out << ",\"text_encoding\":\"utf-8\",\"component\":";
    quoted(out, data.component);
    out << ",\"hardware_id\":";
    quoted(out, data.hardware);
    out << ",\"text\":";
    quoted(out, data.text);
    out << ",\"level\":" << unsigned(data.level) << ",\"fields\":{";
    for (size_t i = 0; i < data.count && i < kFields; ++i) {
      if (i) out << ',';
      quoted(out, data.fields[i].key);
      out << ':';
      if (data.fields[i].numeric)
        number(out, data.fields[i].number);
      else
        quoted(out, data.fields[i].text);
    }
    out << '}';
  }
  out << "}\n";
  auto line = out.str();
  if (line.size() > max_bytes) throw std::length_error("record limit exceeded");
  return line;
}
Writer::Writer(const Config & c, Counters & stats)
: snapshots(c.snapshot_slots, c.snapshot_capacity()),
  events(c.event_slots, sizeof(Compact)),
  config_(c),
  counters_(stats),
  thread_([this] { run(); })
{
}
Writer::~Writer()
{
  stop();
}
bool Writer::push(const Record & record, bool event)
{
  bool ok = event ? events.push(record) : snapshots.push(record);
  if (!ok) {
    if (event)
      ++counters_.dropped_events;
    else
      ++counters_.dropped_snapshots;
  } else
    cv_.notify_one();
  return ok;
}
void Writer::stop()
{
  stopping_.store(true);
  cv_.notify_one();
  if (thread_.joinable()) thread_.join();
}
void Writer::run()
{
  Store store(config_, counters_);
  std::string buffer;
  buffer.reserve(config_.batch_bytes);
  uint64_t buffered_records = 0;
  auto now = steady_ns(), last_flush = now, last_sync = now, last_maintenance = now, retry_at = now;
  bool opened = false;
  auto flush = [&] {
    if (!buffer.empty()) {
      store.append(buffer);
      counters_.written_records += buffered_records;
      buffer.clear();
      buffered_records = 0;
    }
    last_flush = steady_ns();
  };
  while (true) {
    now = steady_ns();
    try {
      if (!opened && now >= retry_at) {
        store.open();
        opened = true;
        counters_.disk_ok = true;
      }
      bool had_event = false;
      for (auto pair : {std::make_pair(&events, 16), std::make_pair(&snapshots, 4)}) {
        for (int i = 0; i < pair.second; ++i) {
          auto * record = pair.first->front();
          if (!record) break;
          if (!opened) {
            ++counters_.disk_drops;
            pair.first->pop();
            continue;
          }
          std::string line;
          try {
            line = json_line(*record, config_.max_record_bytes);
          } catch (const std::length_error &) {
            ++counters_.input_limits;
            ++counters_.disk_drops;
            pair.first->pop();
            continue;
          }
          // Consume exactly once; failed/uncertain writes are never replayed.
          pair.first->pop();
          try {
            if (buffer.size() + line.size() > config_.batch_bytes) flush();
            if (line.size() > config_.batch_bytes) {
              store.append(line);
              ++counters_.written_records;
            } else {
              buffer += line;
              ++buffered_records;
            }
          } catch (...) {
            ++counters_.disk_drops;
            throw;
          }
          had_event |= pair.first == &events;
        }
      }
      now = steady_ns();
      if (opened) {
        if (had_event || now - last_flush >= config_.flush_s * 1e9 || stopping_) flush();
        if (now - last_sync >= config_.sync_s * 1e9) {
          store.sync();
          last_sync = now;
        }
        if (now - last_maintenance >= config_.maintenance_s * 1e9) {
          store.maintenance();
          last_maintenance = now;
        }
      }
    } catch (const std::exception & error) {
      const auto * system = dynamic_cast<const std::system_error *>(&error);
      counters_.last_errno = system ? system->code().value() : EIO;
      ++counters_.write_errors;
      counters_.disk_ok = false;
      counters_.disk_drops += buffered_records;
      buffer.clear();
      buffered_records = 0;
      store.abandon();
      opened = false;
      retry_at = steady_ns() + int64_t(config_.retry_s * 1e9);
    }
    if (stopping_ && !events.size() && !snapshots.size()) break;
    if (events.size() || snapshots.size()) continue;
    // No polling: new records wake us, or the next monotonic service deadline does.
    auto next =
      opened
        ? std::min(
            {last_flush + int64_t(config_.flush_s * 1e9), last_sync + int64_t(config_.sync_s * 1e9),
             last_maintenance + int64_t(config_.maintenance_s * 1e9)})
        : retry_at;
    std::unique_lock<std::mutex> lock(mutex_);
    cv_.wait_until(
      lock, std::chrono::steady_clock::time_point(std::chrono::nanoseconds(next)),
      [&] { return stopping_ || events.size() || snapshots.size(); });
  }
  try {
    if (opened) {
      flush();
      store.finish();
    }
  } catch (const std::exception &) {
    counters_.disk_drops += buffered_records;
    ++counters_.write_errors;
    counters_.disk_ok = false;
  }
}
}  // namespace guide_robot_journal

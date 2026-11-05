#include <yaml-cpp/yaml.h>

#include <cmath>
#include <set>
#include <stdexcept>

#include "guide_robot_journal/core.hpp"

namespace guide_robot_journal {
size_t Config::snapshot_capacity() const
{
  size_t size = sizeof(Compact);
  for (const auto & s : sources)
    if (s.adapter == "serialized") size = std::max(size, s.max_bytes);
  return size;
}
size_t Config::owned_memory_bound() const
{
  size_t size = (snapshot_slots + 1) * (snapshot_capacity() + sizeof(Record)) +
                (event_slots + 1) * (sizeof(Compact) + sizeof(Record));
  // Cache storage, keys/metadata and vector control objects; allocate full diagnostic pool.
  size += (max_components + sources.size()) * (sizeof(Record) + 256);
  size += max_components * sizeof(Compact);
  for (const auto & s : sources) size += s.adapter == "serialized" ? s.max_bytes : sizeof(Compact);
  // Formatting scratch + buffered I/O; ROS/DDS message allocations are NOT in this budget.
  return size + 3 * max_record_bytes + batch_bytes + 3 * sizeof(Compact) +
         (max_files + 2) * (directory.size() + 256);
}
void Config::validate() const
{
  for (auto v :
       {snapshot_s, freshness_s, flush_s, sync_s, rotation_s, retention_s, maintenance_s, retry_s})
    if (!std::isfinite(v) || v <= 0 || v > 31536000) throw std::runtime_error("invalid interval");
  if (
    directory.empty() || directory.find('\0') != std::string::npos || directory.size() > 4096 ||
    max_sources > 128 || max_components > 256 || sources.size() > max_sources || !snapshot_slots ||
    snapshot_slots > 4096 || !event_slots || event_slots > 4096 || !max_statuses_per_message ||
    max_statuses_per_message > 1024 || max_files > 10000 || batch_bytes < 1024 ||
    batch_bytes > 4 * 1024 * 1024 || max_record_bytes < 32768 ||
    max_record_bytes > 16 * 1024 * 1024 || file_bytes < std::max(batch_bytes, max_record_bytes) ||
    total_bytes < file_bytes || total_bytes > (1ULL << 50) || reserve_bytes > (1ULL << 50) ||
    memory_budget > 256 * 1024 * 1024)
    throw std::runtime_error("invalid storage/memory limits");
  std::set<std::string> ids;
  for (const auto & s : sources) {
    if (
      s.id.empty() || s.id.find('\0') != std::string::npos ||
      s.topic.find('\0') != std::string::npos || s.type.find('\0') != std::string::npos ||
      s.id == "__journal" || s.id.size() >= 48 || !ids.insert(s.id).second || s.topic.empty() ||
      s.topic.front() != '/' || s.topic.size() >= 192 || s.type.size() >= 96 ||
      s.topic == "/robot_journal/diagnostics" || !s.depth || s.depth > 128 ||
      !std::isfinite(s.stale_s) || s.stale_s < 0 ||
      (s.reliability != "best_effort" && s.reliability != "reliable") ||
      (s.durability != "volatile" && s.durability != "transient_local") || s.max_bytes < 1 ||
      s.max_bytes > 4 * 1024 * 1024 || s.event_fields.size() > kFields)
      throw std::runtime_error("invalid source " + s.id);
    if (!s.event_fields.empty() && s.adapter != "diagnostics")
      throw std::runtime_error("event_fields are diagnostic string keys");
    for (const auto & key : s.event_fields)
      if (key.empty() || key.find('\0') != std::string::npos || key.size() >= 32)
        throw std::runtime_error("invalid event field");
    const std::set<std::string> adapters{"string",     "bool",    "battery",
                                         "odometry",   "twist",   "diagnostics",
                                         "serialized", "arrival", "mission"};
    if (
      !adapters.count(s.adapter) || (s.stream && s.adapter != "serialized") ||
      ((s.adapter == "serialized" || s.adapter == "arrival") && s.type.empty()))
      throw std::runtime_error("invalid adapter/type " + s.id);
    if (s.adapter == "serialized" && (4 * ((s.max_bytes + 2) / 3) + 2048 > max_record_bytes))
      throw std::runtime_error("full CDR payload will not fit max_record_bytes");
  }
  if (owned_memory_bound() > memory_budget)
    throw std::runtime_error("owned memory budget exceeded");
}
namespace {
template <typename T>
void load(const YAML::Node & node, const char * key, T & value)
{
  if (node[key]) value = node[key].as<T>();
}
void keys(const YAML::Node & node, const std::set<std::string> & allowed)
{
  if (!node.IsMap()) throw std::runtime_error("expected configuration map");
  for (const auto & entry : node)
    if (!allowed.count(entry.first.as<std::string>()))
      throw std::runtime_error("unknown configuration key: " + entry.first.as<std::string>());
}
}  // namespace
Config load_config(const std::string & path)
{
  Config c;
  auto doc = YAML::LoadFile(path);
  keys(doc, {"storage", "timing", "limits", "sources"});
  if (auto n = doc["storage"]) {
    keys(
      n, {"directory", "batch_bytes", "file_bytes", "max_files", "total_bytes", "reserve_bytes"});
    if (n["directory"]) c.directory = n["directory"].as<std::string>();
    load(n, "batch_bytes", c.batch_bytes);
    load(n, "file_bytes", c.file_bytes);
    load(n, "max_files", c.max_files);
    load(n, "total_bytes", c.total_bytes);
    load(n, "reserve_bytes", c.reserve_bytes);
  }
  if (auto n = doc["limits"]) {
    keys(
      n, {"max_record_bytes", "max_sources", "max_components", "max_statuses_per_message",
          "snapshot_slots", "event_slots", "memory_budget"});
    load(n, "max_record_bytes", c.max_record_bytes);
    load(n, "max_sources", c.max_sources);
    load(n, "max_components", c.max_components);
    load(n, "max_statuses_per_message", c.max_statuses_per_message);
    load(n, "snapshot_slots", c.snapshot_slots);
    load(n, "event_slots", c.event_slots);
    load(n, "memory_budget", c.memory_budget);
  }
  if (auto n = doc["timing"]) {
    keys(
      n, {"snapshot_s", "freshness_s", "flush_s", "sync_s", "rotation_s", "retention_s",
          "maintenance_s", "retry_s"});
    load(n, "snapshot_s", c.snapshot_s);
    load(n, "freshness_s", c.freshness_s);
    load(n, "flush_s", c.flush_s);
    load(n, "sync_s", c.sync_s);
    load(n, "rotation_s", c.rotation_s);
    load(n, "retention_s", c.retention_s);
    load(n, "maintenance_s", c.maintenance_s);
    load(n, "retry_s", c.retry_s);
  }
  if (auto list = doc["sources"]) {
    if (!list.IsSequence() || list.size() > c.max_sources)
      throw std::runtime_error("invalid sources");
    for (const auto & n : list) {
      keys(
        n, {"id", "topic", "adapter", "type", "reliability", "durability", "depth", "max_bytes",
            "stale_s", "stream", "events", "event_fields"});
      Source s;
      s.id = n["id"].as<std::string>();
      s.topic = n["topic"].as<std::string>();
      s.adapter = n["adapter"].as<std::string>();
      load(n, "type", s.type);
      load(n, "reliability", s.reliability);
      load(n, "durability", s.durability);
      load(n, "depth", s.depth);
      load(n, "max_bytes", s.max_bytes);
      load(n, "stale_s", s.stale_s);
      load(n, "stream", s.stream);
      load(n, "events", s.events);
      load(n, "event_fields", s.event_fields);
      c.sources.push_back(s);
    }
  }
  if (c.directory.rfind("~/", 0) == 0) {
    const char * home = std::getenv("HOME");
    if (!home) throw std::runtime_error("HOME missing; use absolute directory");
    c.directory = std::string(home) + c.directory.substr(1);
  }
  if (!std::filesystem::path(c.directory).is_absolute())
    throw std::runtime_error("directory must be absolute");
  c.validate();
  return c;
}
}  // namespace guide_robot_journal

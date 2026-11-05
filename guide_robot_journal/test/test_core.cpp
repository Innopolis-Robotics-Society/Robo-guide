#include <fcntl.h>
#include <gtest/gtest.h>
#include <sys/stat.h>
#include <unistd.h>
#include <yaml-cpp/yaml.h>

#include <fstream>
#include <future>

#include "guide_robot_journal/core.hpp"

using namespace guide_robot_journal;
namespace fs = std::filesystem;
class CoreTest : public testing::Test
{
protected:
  fs::path path;
  Config config;
  Counters stats;
  void SetUp() override
  {
    char name[] = "/tmp/guide-journal-test-XXXXXX";
    path = ::mkdtemp(name);
    config.directory = path.string();
    config.reserve_bytes = 0;
  }
  void TearDown() override { fs::remove_all(path); }
  size_t completed()
  {
    size_t n = 0;
    for (auto & e : fs::directory_iterator(path))
      if (e.path().extension() == ".jsonl") ++n;
    return n;
  }
};
TEST_F(CoreTest, QueueBoundedAndCopiesPayload)
{
  Queue queue(2, 16);
  Record record(16);
  record.used = 3;
  record.data[0] = 1;
  ASSERT_TRUE(queue.push(record));
  record.data[0] = 2;
  ASSERT_TRUE(queue.push(record));
  ASSERT_FALSE(queue.push(record));
  ASSERT_EQ(queue.front()->data[0], 1);
  queue.pop();
  ASSERT_EQ(queue.front()->data[0], 2);
  queue.pop();
  ASSERT_EQ(queue.front(), nullptr);
  ASSERT_EQ(queue.high_water(), 2);
}
TEST_F(CoreTest, QueueMaintainsOrderAcrossThreads)
{
  Queue queue(8, 1);
  Record record(1);
  auto reader = std::async(std::launch::async, [&] {
    for (uint64_t i = 0; i < 20000; ++i) {
      while (!queue.front()) std::this_thread::yield();
      EXPECT_EQ(queue.front()->header.sequence, i);
      queue.pop();
    }
  });
  for (uint64_t i = 0; i < 20000; ++i) {
    record.header.sequence = i;
    while (!queue.push(record)) std::this_thread::yield();
  }
  reader.get();
}
TEST_F(CoreTest, JsonPreservesUnicodeNumbersAndFullCdr)
{
  Record record(sizeof(Compact));
  Compact data;
  text(data.text, sizeof(data.text), "Ошибка: \"привод\"\n");
  text(data.fields[0].key, 32, "voltage");
  data.fields[0].numeric = true;
  data.fields[0].number = 24.5;
  data.count = 1;
  record.header.encoding = Encoding::Compact;
  record.header.payload_available = true;
  record.used = sizeof(data);
  std::memcpy(record.data.data(), &data, sizeof(data));
  auto parsed = YAML::Load(json_line(record, 32768));
  EXPECT_EQ(parsed["text"].as<std::string>(), "Ошибка: \"привод\"\n");
  EXPECT_EQ(parsed["fields"]["voltage"].as<double>(), 24.5);
  record.header.encoding = Encoding::Cdr;
  record.used = 3;
  record.data[0] = 0;
  record.data[1] = 1;
  record.data[2] = 2;
  parsed = YAML::Load(json_line(record, 32768));
  EXPECT_EQ(parsed["data"].as<std::string>(), "AAEC");
  EXPECT_THROW(json_line(record, 10), std::length_error);
}
TEST_F(CoreTest, ConfigurationRejectsUnboundedMemoryAndUnknownKeys)
{
  config.validate();
  Source source;
  source.id = "lidar";
  source.topic = "/scan";
  source.adapter = "serialized";
  source.type = "sensor_msgs/msg/LaserScan";
  config.sources.push_back(source);
  EXPECT_NO_THROW(config.validate());
  config.snapshot_slots = 4096;
  EXPECT_THROW(config.validate(), std::runtime_error);
  std::ofstream(path / "bad.yaml") << "sources: []\ntypo: true\n";
  EXPECT_THROW(load_config((path / "bad.yaml").string()), std::runtime_error);
}
TEST_F(CoreTest, RotationCountAndForeignFilesAreSafe)
{
  config.file_bytes = 32;
  config.max_files = 2;
  std::ofstream(path / "keep.txt") << "untouched";
  fs::create_symlink(path / "keep.txt", path / "robotlog-1-1-1.jsonl");
  Store store(config, stats);
  store.open();
  for (int i = 0; i < 8; ++i) store.append("{\"value\":123456789}\n");
  store.finish();
  EXPECT_LE(completed(), 3);  // two regular completed files and the untouched symlink
  EXPECT_TRUE(fs::is_symlink(path / "robotlog-1-1-1.jsonl"));
  EXPECT_TRUE(fs::exists(path / "keep.txt"));
  EXPECT_GT(stats.deleted_files, 0);
}
TEST_F(CoreTest, ActiveFileCountsTowardVolumeLimit)
{
  config.file_bytes = 40;
  config.total_bytes = 50;
  Store store(config, stats);
  store.open();
  for (int i = 0; i < 20; ++i) store.append("{\"x\":123456789}\n");
  EXPECT_LE(stats.retained_bytes, 50);
  store.finish();
  EXPECT_LE(stats.retained_bytes, 50);
}
TEST_F(CoreTest, RecoversOnlyIncompleteTail)
{
  auto active = path / "robotlog-1-2-3.open";
  std::ofstream(active) << "{\"ok\":1}\n{\"partial\":";
  Store store(config, stats);
  store.open();
  auto final = path / "robotlog-1-2-3.jsonl";
  ASSERT_TRUE(fs::exists(final));
  std::ifstream in(final);
  std::string content((std::istreambuf_iterator<char>(in)), {});
  EXPECT_EQ(content, "{\"ok\":1}\n");
  store.finish();
}
TEST_F(CoreTest, LockExcludesSecondWriterAndSymlinkLock)
{
  Store first(config, stats);
  first.open();
  Store second(config, stats);
  EXPECT_THROW(second.open(), std::system_error);
  first.finish();
  auto other = path / "other";
  fs::create_directory(other);
  fs::create_symlink(path / ".robotlog.lock", other / ".robotlog.lock");
  config.directory = other.string();
  Store linked(config, stats);
  EXPECT_THROW(linked.open(), std::system_error);
}
TEST_F(CoreTest, AgeRetentionUsesFileLastWrite)
{
  auto old = path / "robotlog-1-2-3.jsonl";
  std::ofstream(old) << "{}\n";
  struct timespec times[2] = {{1, 0}, {1, 0}};
  ASSERT_EQ(::utimensat(AT_FDCWD, old.c_str(), times, 0), 0);
  Store store(config, stats);
  store.open();
  EXPECT_FALSE(fs::exists(old));
  store.finish();
}
TEST_F(CoreTest, DiskReserveFailsClosedWithoutGrowingQueues)
{
  config.reserve_bytes = 1ULL << 50;
  config.retry_s = .05;
  config.snapshot_slots = 2;
  config.event_slots = 2;
  Writer writer(config, stats);
  Record record(sizeof(Compact));
  record.header.encoding = Encoding::Arrival;
  for (int i = 0; i < 1000; ++i) writer.push(record, false);
  std::this_thread::sleep_for(std::chrono::milliseconds(130));
  writer.stop();
  EXPECT_FALSE(stats.disk_ok);
  EXPECT_GT(stats.write_errors, 0);
  EXPECT_LT(stats.write_errors, 10);
  EXPECT_LE(writer.snapshots.high_water(), 2);
  EXPECT_EQ(stats.dropped_snapshots + stats.disk_drops, 1000);
}
TEST_F(CoreTest, GracefulShutdownDrainsAndCloses)
{
  Writer writer(config, stats);
  Record record(sizeof(Compact));
  for (int i = 0; i < 4; ++i) {
    record.header.sequence = i;
    ASSERT_TRUE(writer.push(record, true));
  }
  writer.stop();
  EXPECT_EQ(stats.written_records, 4);
  EXPECT_EQ(stats.disk_drops, 0);
  EXPECT_EQ(completed(), 1);
  for (auto & e : fs::directory_iterator(path)) EXPECT_NE(e.path().extension(), ".open");
}

TEST_F(CoreTest, RecoveryFinishesInterruptedLinkAndUnlink)
{
  auto active = path / "robotlog-9-8-7.open", final = path / "robotlog-9-8-7.jsonl";
  std::ofstream(active) << "{\"ok\":1}\n";
  fs::create_hard_link(active, final);
  Store store(config, stats);
  EXPECT_NO_THROW(store.open());
  EXPECT_FALSE(fs::exists(active));
  EXPECT_TRUE(fs::exists(final));
  store.finish();
}
TEST_F(CoreTest, RecoveryKeepsOldFileAgeAndBoundsStartupIndex)
{
  config.max_files = 2;
  for (int i = 0; i < 20; ++i)
    std::ofstream(path / ("robotlog-1-1-" + std::to_string(i) + ".jsonl")) << "{}\n";
  auto active = path / "robotlog-2-1-1.open";
  std::ofstream(active) << "{}\n{";
  struct timespec times[2] = {{1, 0}, {1, 0}};
  ::utimensat(AT_FDCWD, active.c_str(), times, 0);
  Store store(config, stats);
  store.open();
  EXPECT_FALSE(fs::exists(path / "robotlog-2-1-1.jsonl"));
  EXPECT_LE(completed(), 2);
  store.finish();
}

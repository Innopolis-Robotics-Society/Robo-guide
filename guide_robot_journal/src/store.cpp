#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <regex>
#include <stdexcept>
#include <system_error>

#include "guide_robot_journal/core.hpp"

namespace guide_robot_journal {
namespace fs = std::filesystem;
namespace {
[[noreturn]] void failed(const char * what)
{
  throw std::system_error(errno, std::generic_category(), what);
}
void close_fd(int & fd)
{
  if (fd >= 0) ::close(fd);
  fd = -1;
}
void sync_directory(const std::string & path)
{
  int fd = ::open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0) failed("open directory for sync");
  if (::fsync(fd) < 0) {
    const int error = errno;
    close_fd(fd);
    errno = error;
    failed("sync directory");
  }
  close_fd(fd);
}
bool regular(const fs::path & p)
{
  return fs::is_regular_file(fs::symlink_status(p));
}
void complete_name(const fs::path & p)
{
  auto final = p;
  final.replace_extension(".jsonl");
  // Link/unlink prevents overwriting a previous file (unlike POSIX rename).
  if (::link(p.c_str(), final.c_str()) < 0) {
    struct stat original {
    }, completed{};
    // A crash between link and unlink leaves two names for the same owned inode.
    if (
      errno != EEXIST || ::lstat(p.c_str(), &original) < 0 ||
      ::lstat(final.c_str(), &completed) < 0 || !S_ISREG(completed.st_mode) ||
      original.st_dev != completed.st_dev || original.st_ino != completed.st_ino)
      failed("complete file");
  }
  if (::unlink(p.c_str()) < 0) failed("unlink active file");
}
void recover_file(const fs::path & path)
{
  int fd = ::open(path.c_str(), O_RDWR | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0) failed("open recovery");
  try {
    struct stat st {
    };
    if (::fstat(fd, &st) < 0) failed("recovery stat");
    if (!S_ISREG(st.st_mode)) throw std::runtime_error("not a regular recovery file");
    off_t end = st.st_size, cut = 0;
    std::array<char, 4096> bytes{};
    while (end > 0) {
      size_t n = std::min<off_t>(end, bytes.size());
      off_t begin = end - n;
      ssize_t got = ::pread(fd, bytes.data(), n, begin);
      if (got < 0 && errno == EINTR) continue;
      if (got != static_cast<ssize_t>(n)) {
        if (got >= 0) errno = EIO;
        failed("read recovery");
      }
      for (size_t i = n; i > 0; --i)
        if (bytes[i - 1] == '\n') {
          cut = begin + i;
          break;
        }
      if (cut) break;
      end = begin;
    }
    if (::ftruncate(fd, cut) < 0 || ::fdatasync(fd) < 0) failed("truncate recovery");
    struct timespec times[2] = {st.st_atim, st.st_mtim};
    if (::futimens(fd, times) < 0) failed("restore recovery mtime");
    close_fd(fd);
    complete_name(path);
  } catch (...) {
    close_fd(fd);
    throw;
  }
}
}  // namespace
Store::Store(const Config & config, Counters & counters) : config_(config), counters_(counters)
{
  files_.reserve(config_.max_files + 2);
}
Store::~Store()
{
  abandon();
  close_fd(lock_fd_);
}
void Store::abandon() noexcept
{
  close_fd(fd_);
  active_.clear();
  active_bytes_ = 0;
}
uint64_t Store::total() const
{
  uint64_t sum = active_bytes_;
  for (const auto & file : files_) sum += file.bytes;
  return sum;
}
void Store::scan(bool recover)
{
  static const std::regex owned("^robotlog-[0-9]+-[0-9]+-[0-9]+\\.(open|jsonl)$");
  files_.clear();
  for (const auto & entry : fs::directory_iterator(config_.directory)) {
    const auto p = entry.path();
    if (p == active_ || !std::regex_match(p.filename().string(), owned) || !regular(p)) continue;
    if (p.extension() == ".open") {
      if (recover)
        recover_file(p);
      else
        continue;
    }
  }
  for (const auto & entry : fs::directory_iterator(config_.directory)) {
    const auto p = entry.path();
    if (p.extension() != ".jsonl" || !std::regex_match(p.filename().string(), owned) || !regular(p))
      continue;
    struct stat st {
    };
    if (::lstat(p.c_str(), &st) < 0) failed("index file");
    files_.push_back(
      {p, uint64_t(st.st_size), int64_t(st.st_mtim.tv_sec) * 1000000000 + st.st_mtim.tv_nsec});
    if (files_.size() > config_.max_files + 1) {
      auto oldest = std::min_element(
        files_.begin(), files_.end(),
        [](const File & a, const File & b) { return a.modified_ns < b.modified_ns; });
      if (regular(oldest->path)) {
        fs::remove(oldest->path);
        ++counters_.deleted_files;
      }
      files_.erase(oldest);
    }
  }
  std::sort(files_.begin(), files_.end(), [](const File & a, const File & b) {
    return a.modified_ns == b.modified_ns ? a.path < b.path : a.modified_ns < b.modified_ns;
  });
  counters_.retained_bytes = total();
}
void Store::trim(uint64_t incoming)
{
  while (true) {
    struct statvfs space {
    };
    if (::statvfs(config_.directory.c_str(), &space) < 0) failed("free space");
    uint64_t free = uint64_t(space.f_bavail) * space.f_frsize;
    bool expired =
      !files_.empty() && utc_ns() - files_.front().modified_ns > config_.retention_s * 1e9;
    bool count = files_.size() > config_.max_files;
    bool size = total() + incoming > config_.total_bytes;
    bool reserve = free < config_.reserve_bytes + incoming;
    if (!expired && !count && !size && !reserve) break;
    if (files_.empty()) {
      if (size || reserve) {
        errno = ENOSPC;
        failed("journal reserve/volume limit");
      }
      break;
    }
    const auto file = files_.front();
    // Recheck: never follow or delete a symlink substituted since indexing.
    if (regular(file.path)) {
      if (!fs::remove(file.path)) throw std::runtime_error("retention removal failed");
      ++counters_.deleted_files;
    }
    files_.erase(files_.begin());
  }
  counters_.retained_bytes = total();
}
void Store::create()
{
  for (int attempt = 0; attempt < 64; ++attempt) {
    active_ = fs::path(config_.directory) /
              ("robotlog-" + std::to_string(utc_ns()) + "-" + std::to_string(::getpid()) + "-" +
               std::to_string(++serial_) + ".open");
    fd_ = ::open(active_.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd_ >= 0) {
      active_bytes_ = 0;
      opened_ns_ = steady_ns();
      return;
    }
    if (errno != EEXIST) failed("create journal");
  }
  throw std::runtime_error("journal filename collision");
}
void Store::open()
{
  fs::create_directories(config_.directory);
  if (lock_fd_ < 0) {
    auto path = fs::path(config_.directory) / ".robotlog.lock";
    int fd = ::open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) failed("open journal lock");
    struct stat st {
    };
    if (::fstat(fd, &st) < 0 || !S_ISREG(st.st_mode)) {
      close_fd(fd);
      throw std::runtime_error("invalid lock file");
    }
    if (::flock(fd, LOCK_EX | LOCK_NB) < 0) {
      const int error = errno;
      close_fd(fd);
      errno = error;
      failed("lock journal");
    }
    lock_fd_ = fd;
  }
  scan(true);
  trim(0);
  create();
}
void Store::sync()
{
  if (fd_ >= 0 && ::fdatasync(fd_) < 0) failed("sync journal");
  sync_directory(config_.directory);
}
void Store::rotate()
{
  if (fd_ < 0) return;
  sync();
  close_fd(fd_);
  auto p = active_;
  complete_name(p);
  sync_directory(config_.directory);
  active_.clear();
  active_bytes_ = 0;
  // At rotation update the index; directory reconciliation is otherwise periodic.
  p.replace_extension(".jsonl");
  struct stat st {
  };
  if (::lstat(p.c_str(), &st) < 0) failed("rotated stat");
  files_.push_back(
    {p, uint64_t(st.st_size), int64_t(st.st_mtim.tv_sec) * 1000000000 + st.st_mtim.tv_nsec});
  std::sort(files_.begin(), files_.end(), [](const File & a, const File & b) {
    return a.modified_ns < b.modified_ns;
  });
  trim(0);
}
void Store::append(const std::string & bytes)
{
  if (bytes.size() > config_.file_bytes) throw std::length_error("batch exceeds file size");
  if (fd_ < 0) throw std::runtime_error("journal not open");
  if (
    active_bytes_ && (active_bytes_ + bytes.size() > config_.file_bytes ||
                      steady_ns() - opened_ns_ >= config_.rotation_s * 1e9)) {
    rotate();
    create();
  }
  trim(bytes.size());
  size_t written = 0;
  while (written < bytes.size()) {
    ssize_t n = ::write(fd_, bytes.data() + written, bytes.size() - written);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) {
      const int error = n < 0 ? errno : EIO;
      // Never replay an uncertain batch. Keep only its pre-write complete boundary.
      (void)::ftruncate(fd_, active_bytes_);
      errno = error;
      failed("write journal");
    }
    written += n;
  }
  active_bytes_ += bytes.size();
  counters_.bytes_written += bytes.size();
  counters_.retained_bytes = total();
}
void Store::maintenance()
{
  if (fd_ >= 0 && steady_ns() - opened_ns_ >= config_.rotation_s * 1e9) {
    rotate();
    create();
  }
  scan(false);
  trim(0);
}
void Store::finish()
{
  rotate();
}
}  // namespace guide_robot_journal

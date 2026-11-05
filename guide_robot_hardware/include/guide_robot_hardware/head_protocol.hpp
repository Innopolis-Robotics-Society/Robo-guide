#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace guide_robot_hardware {

// The only head write supported by the ROS bridge: absolute position, axis 2B,
// speed 10. Limit/configuration/firmware operations have no code path here.
constexpr std::array<uint8_t, 13> makeHeadPositionPacket(uint16_t target)
{
  std::array<uint8_t, 13> packet = {
    0xff, 0xff, 0xfe, 0x09, 0x83, 0x1e, 0x04, 0x2b,
    static_cast<uint8_t>(target & 0xff), static_cast<uint8_t>((target >> 8) & 0xff),
    0x0a, 0x00, 0x00};
  unsigned int sum = 0;
  for (std::size_t i = 2; i < 12; ++i) sum += packet[i];
  packet[12] = static_cast<uint8_t>(~sum);
  return packet;
}

static_assert(makeHeadPositionPacket(1770)[8] == 0xea, "head target little-endian");
static_assert(makeHeadPositionPacket(1770)[9] == 0x06, "head target little-endian");
static_assert(makeHeadPositionPacket(1770)[12] == 0x2e, "known head packet checksum");

}  // namespace guide_robot_hardware

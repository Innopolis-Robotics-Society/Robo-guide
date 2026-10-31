#!/usr/bin/env python3
"""Build a FURO-D head packet offline; this tool never opens a serial port."""

import argparse
import math
import struct
import sys


def head_packet(position: int, speed: int = 10) -> bytes:
    """Match MobilityJoveTech.DriveOneAxis(0x2B, position, speed)."""
    if not 0 <= position <= 65535 or not 0 <= speed <= 65535:
        raise ValueError("position and speed must be unsigned 16-bit integers")
    body = bytes.fromhex("FE 09 83 1E 04 2B") + struct.pack("<HH", position, speed)
    return b"\xff\xff" + body + bytes([~sum(body) & 0xFF])


def dynrobot_position(angle_deg: float, center: int = 1900) -> int:
    """Use this disk's DynRobot calibration; angle sign is DynRobot's sign."""
    if not math.isfinite(angle_deg) or not -15 <= angle_deg <= 15:
        raise ValueError("angle must be finite and within the saved DynRobot limit [-15, 15]")
    return center + math.trunc(math.radians(angle_deg) / 8.7266e-4)


def main() -> None:
    """Print the requested packet as hexadecimal text or binary bytes."""
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--position", type=int, help="raw absolute position, not a signed increment"
    )
    target.add_argument("--angle", type=float, help="degrees using saved DynRobot calibration")
    parser.add_argument(
        "--center", type=int, default=1900, help="DynRobot raw center (default: 1900)"
    )
    parser.add_argument(
        "--speed", type=int, default=10, help="raw speed (default: 10, as in DynRobot)"
    )
    parser.add_argument(
        "--binary", action="store_true", help="write 13 raw bytes to stdout instead of hex"
    )
    args = parser.parse_args()
    try:
        position = (
            args.position
            if args.position is not None
            else dynrobot_position(args.angle, args.center)
        )
        packet = head_packet(position, args.speed)
    except ValueError as error:
        parser.error(str(error))
    if args.binary:
        sys.stdout.buffer.write(packet)
    else:
        print(packet.hex(" ").upper())


if __name__ == "__main__":
    main()

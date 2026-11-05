"""Check that head senders reject other controller operations before opening COM."""

import unittest

from furo_head_packet import head_packet, validate_head_packet
from furo_head_send_one import parse_and_validate
from furo_head_step import validate


class HeadPacketTest(unittest.TestCase):
    def test_known_center_packet(self):
        expected = bytes.fromhex("FF FF FE 09 83 1E 04 2B EA 06 0A 00 2E")
        self.assertEqual(head_packet(1770, 10), expected)
        for check in (parse_and_validate, validate):
            self.assertEqual(check(expected.hex(" ")), expected)

    def test_rejects_other_layouts_even_with_correct_checksum(self):
        good = head_packet(1770)
        rejected = [b"", good[:7], good + good, good[:-1], good[:-1] + b"\0"]
        for offset, value in ((2, 0x2F), (4, 0x03), (5, 0xB1), (6, 0x08), (7, 0x2E)):
            changed = bytearray(good)
            changed[offset] = value
            changed[-1] = ~sum(changed[2:-1]) & 255
            rejected.append(bytes(changed))
        # Known telemetry and wheel packets are valid packets, but never head commands.
        rejected.extend(bytes.fromhex(s) for s in (
            "FF FF 2E 04 03 A0 05 25",
            "FF FF FE 0E 06 20 04 2E 00 00 E8 03 2F 00 00 E8 03 96",
        ))
        for packet in rejected:
            with self.subTest(packet=packet.hex(" ")):
                with self.assertRaises(ValueError):
                    validate_head_packet(packet)
                for check in (parse_and_validate, validate):
                    with self.assertRaises(SystemExit):
                        check(packet.hex(" "))


if __name__ == "__main__":
    unittest.main()

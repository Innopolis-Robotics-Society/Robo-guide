"""Exercise telemetry recovery and Linux COM capture without robot hardware."""

import json
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import furo_head_capture as capture


def frame(head):
    """Build a telemetry response with signed wheel counts."""
    body = bytes.fromhex("2E 0F 47 A0") + struct.pack("<iii", -15, 1579, head)
    return b"\xff\xff" + body + bytes([~sum(body) & 255])


class CaptureTest(unittest.TestCase):
    """Check recovery, marking and actual POSIX serial file operations."""

    def test_fragmentation_corruption_and_concatenation(self):
        """Find valid frames after noise, bad checksums and split headers."""
        decoder = capture.Decoder()
        broken = bytearray(frame(1900))
        broken[-1] ^= 1
        stream = b"noise\xff" + broken + frame(1747) + frame(2200)
        frames = []
        for byte in stream:
            frames.extend(decoder.feed(bytes([byte])))
        self.assertEqual(frames, [frame(1747), frame(2200)])
        self.assertGreaterEqual(decoder.bad_checksums, 1)

    def test_marks_reject_stale_or_incomplete_windows(self):
        """Do not turn a missing response or an old sample into a limit."""
        samples = [(i / 10, 1750 + i % 3) for i in range(11)]
        result = capture.mark_window(samples, 1, 1)
        self.assertEqual(result["median"], 1751)
        with self.assertRaises(ValueError):
            capture.mark_window(samples, 3, 1)
        with self.assertRaises(ValueError):
            capture.mark_window(samples[-5:], 1, 1)

    def test_serial_session_records_only_valid_frames_and_query_commands(self):
        """Emulate USB COM responses on a PTY and inspect saved artifacts."""
        master, slave = pty.openpty()
        device = os.ttyname(slave)
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "capture"
                process = subprocess.Popen(
                    [sys.executable, str(Path(capture.__file__)), "--port", device,
                     "--output", str(output), "--duration", "0.5", "--timeout", "2"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                transmitted = bytearray()
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.05)[0]:
                        transmitted.extend(os.read(master, 4096))
                        if capture.QUERY in transmitted:
                            os.write(master, frame(1747)[:8])
                            os.write(master, frame(1747)[8:] + frame(1775))
                stdout, stderr = process.communicate(timeout=2)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
                self.assertTrue(transmitted)
                self.assertEqual(len(transmitted) % len(capture.QUERY), 0)
                self.assertEqual(
                    bytes(transmitted), capture.QUERY * (len(transmitted) // len(capture.QUERY))
                )
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["status"], "finished")
                self.assertGreaterEqual(summary["frames"], 2)
                self.assertEqual((summary["head_min"], summary["head_max"]), (1747, 1775))
                self.assertNotIn("endpoints", summary)
                self.assertGreater((output / "rx.bin").stat().st_size, 0)
                self.assertIn("head_encoder", (output / "samples.csv").read_text())
                # The exclusive TTY flag must be released for the later ROS driver.
                reopened = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                os.close(reopened)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(slave)
            os.close(master)

    def test_missing_feedback_is_an_error_with_saved_summary(self):
        """A silent controller must not produce a successful empty capture."""
        master, slave = pty.openpty()
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "capture"
                result = subprocess.run(
                    [sys.executable, str(Path(capture.__file__)), "--port", os.ttyname(slave),
                     "--output", str(output), "--duration", "1", "--timeout", "0.1"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
                )
                self.assertEqual(result.returncode, 1)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["status"], "error")
                self.assertEqual(summary["frames"], 0)
        finally:
            os.close(slave)
            os.close(master)

    def test_passive_session_sends_no_bytes(self):
        """A passive session must not transmit even the known encoder query."""
        master, slave = pty.openpty()
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "capture"
                result = subprocess.run(
                    [sys.executable, str(Path(capture.__file__)), "--port", os.ttyname(slave),
                     "--output", str(output), "--duration", "0.1", "--passive"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
                )
                self.assertEqual(result.returncode, 1)  # No telemetry is an empty capture.
                self.assertFalse(select.select([master], [], [], 0)[0])
                summary = json.loads((output / "summary.json").read_text())
                self.assertTrue(summary["passive"])
                self.assertEqual(summary["commands_sent"], 0)
        finally:
            os.close(slave)
            os.close(master)

    def test_interactive_endpoint_marks(self):
        """Drive terminal input and COM feedback together, then verify both limits."""
        master, slave = pty.openpty()
        keyboard, terminal = pty.openpty()
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "capture"
                process = subprocess.Popen(
                    [sys.executable, str(Path(capture.__file__)), "--port", os.ttyname(slave),
                     "--output", str(output), "--window", "0.2", "--timeout", "2"],
                    stdin=terminal, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                start = None
                marked_left = marked_right = False
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.01)[0]:
                        os.read(master, 4096)
                        if start is None:
                            start = time.monotonic()
                    if start is None:
                        continue
                    elapsed = time.monotonic() - start
                    os.write(master, frame(1700 if elapsed < 0.5 else 2300))
                    if elapsed > 0.4 and not marked_left:
                        os.write(keyboard, b"l\n")
                        marked_left = True
                    if elapsed > 0.9 and not marked_right:
                        os.write(keyboard, b"r\n")
                        marked_right = True
                    if elapsed > 1.1:
                        os.write(keyboard, b"q\n")
                stdout, stderr = process.communicate(timeout=2)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["endpoints"]["left"], 1700)
                self.assertEqual(summary["endpoints"]["right"], 2300)
                self.assertEqual(summary["endpoints"]["span"], 600)
                self.assertEqual(summary["endpoints"]["midpoint"], 2000)
                self.assertEqual(summary["endpoints"]["increases_toward"], "right")
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            for fd in (terminal, keyboard, slave, master):
                os.close(fd)


if __name__ == "__main__":
    unittest.main()

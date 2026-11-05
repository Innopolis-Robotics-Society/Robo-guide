"""LED setup sends only documented effect/color commands and checks readback."""
import struct
from unittest.mock import patch

from guide_robot_voice import xvf3800_control as xvf


def test_direction_color_configuration_is_checked():
    writes = []
    def ioctl(fd, request, transfer):
        import ctypes
        payload = ctypes.string_at(transfer.data, transfer.wLength)
        writes.append((transfer.wValue, payload))
    reads = [struct.pack('<II', 0x8B0001, 0x999999), b'\x04']
    with patch.object(xvf, 'ctrl_in', side_effect=reads), \
         patch.object(xvf.fcntl, 'ioctl', side_effect=ioctl):
        xvf.configure_direction_led(42)
    assert writes == [(17, struct.pack('<II', 0x8B0001, 0x999999)), (12, b'\x04')]

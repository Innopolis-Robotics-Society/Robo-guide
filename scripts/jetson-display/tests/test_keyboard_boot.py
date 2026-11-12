"""Exercise the unit's real link-selection commands after simulated NVIDIA boots."""
from pathlib import Path
import shlex
import subprocess


def test_keyboard_selection_survives_repeated_vendor_link_resets(tmp_path):
    unit = (Path(__file__).parents[1] / "guide-direct-keyboard.service").read_text()
    vendor = tmp_path / "nvidia-keyboard"
    corrected = tmp_path / "corrected-keyboard"
    link = tmp_path / "weston-keyboard"
    vendor.write_text("vendor executable unchanged")
    corrected.write_text("corrected executable unchanged")
    commands = [line.removeprefix("ExecStart=") for line in unit.splitlines()
                if line.startswith("ExecStart=")]
    assert commands
    for _boot in range(2):
        link.unlink(missing_ok=True)
        link.symlink_to(vendor)
        for command in commands:
            command = command.replace("/opt/guide-kiosk-input/weston-keyboard", str(corrected))
            command = command.replace("/usr/lib/weston/weston-keyboard", str(link))
            subprocess.run(shlex.split(command), check=True)
        assert link.resolve() == corrected
        assert vendor.read_text() == "vendor executable unchanged"
        assert corrected.read_text() == "corrected executable unchanged"

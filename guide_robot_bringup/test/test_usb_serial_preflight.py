"""Тесты usb_serial_preflight без железа: разбор sysfs-путей, политика сброса, probe на pty."""

import os

import pytest

from guide_robot_bringup import usb_serial_preflight as pf


@pytest.mark.parametrize(
    ("path", "parent"),
    [
        ("1-2.1.4", "1-2.1"),
        ("1-2.1", "1-2"),
        ("1-2", "usb1"),
        ("usb1", None),
    ],
)
def test_parent_usb_path(path, parent):
    assert pf.parent_usb_path(path) == parent


def test_is_external_hub():
    assert pf.is_external_hub("1-2.1")
    assert pf.is_external_hub("1-2.1.2")
    assert not pf.is_external_hub("1-2")  # хаб на плате devkit
    assert not pf.is_external_hub("usb1")


def test_reset_target_behind_external_hub():
    target, _ = pf.choose_reset_target("1-2.3.1")
    assert target == "1-2.3"


def test_reset_target_direct_port_never_touches_onboard_hub():
    target, _ = pf.choose_reset_target("1-2.4")
    assert target == "1-2.4"


def test_usb_path_of_tty_walks_up_to_usb_device(tmp_path, monkeypatch):
    # Реальная раскладка sysfs: class/tty/ttyUSB3/device -> .../1-2.1.4/1-2.1.4:1.0/ttyUSB3
    usb_dev = tmp_path / "devices" / "usb1" / "1-2" / "1-2.1" / "1-2.1.4"
    port_dir = usb_dev / "1-2.1.4:1.0" / "ttyUSB3"
    port_dir.mkdir(parents=True)
    for d in (usb_dev, usb_dev.parent, usb_dev.parent.parent):
        (d / "devnum").write_text("15\n")
        (d / "busnum").write_text("1\n")
    class_tty = tmp_path / "class" / "tty" / "ttyUSB3"
    class_tty.mkdir(parents=True)
    (class_tty / "device").symlink_to(port_dir)
    monkeypatch.setattr(pf, "SYSFS_TTY", tmp_path / "class" / "tty")
    # udev-симлинк как на роботе: /dev/tty_lidar_left -> ttyUSB3
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyUSB3").touch()
    (dev / "tty_lidar_left").symlink_to("ttyUSB3")
    assert pf.usb_path_of_tty(str(dev / "tty_lidar_left")) == "1-2.1.4"


def test_probe_port_ok_on_pty():
    master, slave = os.openpty()
    try:
        ok, msg = pf.probe_port(os.ttyname(slave), timeout=5.0)
    finally:
        os.close(master)
        os.close(slave)
    assert ok, msg
    assert msg.startswith("OK")


def test_probe_port_missing_device():
    ok, msg = pf.probe_port("/dev/tty_does_not_exist_preflight", timeout=5.0)
    assert not ok
    assert "errno 2" in msg


def test_probe_port_reports_hang(monkeypatch):
    def slow_open(*_args, **_kwargs):
        import time

        time.sleep(2.0)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(pf.os, "open", slow_open)
    ok, msg = pf.probe_port("/dev/whatever", timeout=0.2)
    assert not ok
    assert "висит" in msg


def test_run_no_reset_reports_missing_symlink(capsys):
    rc = pf.run(["/dev/tty_missing_preflight"], timeout=1.0, allow_reset=False, settle=0.0)
    assert rc == 1
    out = capsys.readouterr().out
    assert "нет симлинка" in out

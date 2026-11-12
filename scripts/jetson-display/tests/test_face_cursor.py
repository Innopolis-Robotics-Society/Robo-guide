"""Exercise XFixes transitions with a fake display, without moving the real pointer."""

import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "guide-face-cursor"


class Function:
    def __init__(self, callback=lambda *_: None):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def run_helper(monkeypatch, state_dir, frames):
    module = runpy.run_path(str(SCRIPT))
    scope = module["main"].__globals__
    calls, handlers = [], {}
    step = 0

    def preference(value):
        path = state_dir / "face-cursor-visible"
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def advance(_delay):
        nonlocal step
        step += 1
        if step == len(frames):
            handlers[scope["signal"].SIGTERM]()
        else:
            preference(frames[step])

    x = SimpleNamespace(XOpenDisplay=Function(lambda *_: 123),
                        XDefaultRootWindow=Function(lambda *_: 1),
                        XSync=Function(),
                        XCloseDisplay=Function(lambda *_: calls.append("close")))
    fixes = SimpleNamespace(
        XFixesHideCursor=Function(lambda *_: calls.append("hide")),
        XFixesShowCursor=Function(lambda *_: calls.append("show")))
    monkeypatch.setattr(scope["C"], "CDLL", lambda name: x if "X11" in name else fixes)
    monkeypatch.setattr(scope["signal"], "signal", lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(scope["time"], "sleep", advance)
    monkeypatch.setenv("DISPLAY", ":test")
    monkeypatch.setenv("XAUTHORITY", str(state_dir / "test-auth"))
    monkeypatch.setenv("GUIDE_LAUNCHER_STATE_DIR", str(state_dir))
    preference(frames[0])
    module["main"]()
    return calls


def test_toggle_without_browser_or_ros_and_cleanup(tmp_path, monkeypatch):
    frames = [None, "true", "true", "false", "false", "true", "false"]
    assert run_helper(monkeypatch, tmp_path, frames) == [
        "hide", "show", "hide", "show", "hide", "show", "close"]


def test_saved_visible_setting_survives_helper_restart(tmp_path, monkeypatch):
    assert run_helper(monkeypatch, tmp_path, ["true"]) == ["close"]


def test_invalid_setting_preserves_hidden_default(tmp_path):
    read = runpy.run_path(str(SCRIPT))["cursor_visible"]
    path = tmp_path / "face-cursor-visible"
    assert read(tmp_path) is False
    for value in (b"broken", b"1", b"\xff", b"false\n"):
        path.write_bytes(value)
        assert read(tmp_path) is False
    path.write_text("true\n")
    assert read(tmp_path) is True


def test_discovers_current_gnome_display(monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    tried = []

    def open_x(name):
        tried.append(name)
        return 123 if name == b":1" else None

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("XAUTHORITY", "/test-auth")
    monkeypatch.setattr(Path, "glob", lambda *_: [Path("X0"), Path("X1")])
    assert module["open_display"](SimpleNamespace(XOpenDisplay=open_x)) == 123
    assert tried == [b":0", b":1"]


def test_missing_display_exits_for_systemd_retry(monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    monkeypatch.setenv("DISPLAY", ":test")
    monkeypatch.setenv("XAUTHORITY", "/test-auth")
    with pytest.raises(SystemExit, match="Waiting for GNOME X display"):
        module["open_display"](SimpleNamespace(XOpenDisplay=lambda _: None))

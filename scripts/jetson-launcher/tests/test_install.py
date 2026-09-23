"""install.sh: preflight и DRY_RUN на поддельных docker/systemctl/ss/id (без робота)."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
INSTALL = REPO / "scripts" / "jetson-launcher" / "install.sh"
HDMI_SCRIPT = REPO / "scripts" / "jetson-display" / "guide-kiosk-hdmi"
COMPOSE_CMD = yaml.safe_load((REPO / "compose.yaml").read_text())["services"]["jetson"]["command"]

FAKE_DOCKER = """#!/bin/bash
D="$FAKE_DIR"
case "$1" in
  inspect) cat "$D/inspect.json" ;;
  exec)
    shift 2
    case "$*" in
      "bash -ic env") cat "$D/container_env.txt" ;;
      "id -u") cat "$D/container_uid" ;;
      "id -g") cat "$D/container_gid" ;;
    esac ;;
esac
"""
FAKE_SYSTEMCTL = """#!/bin/bash
case "$1" in
  is-active) [ -f "$FAKE_DIR/launcher_active" ] ;;
  cat) exit 0 ;;
  *) echo "systemctl $*" >>"$FAKE_DIR/systemctl.log" ;;
esac
"""
FAKE_SS = '#!/bin/bash\n[ -f "$FAKE_DIR/ss.txt" ] && cat "$FAKE_DIR/ss.txt"\nexit 0\n'
FAKE_ID = """#!/bin/bash
case "$*" in
  "-nG jetson") cat "$FAKE_DIR/groups" ;;
  "-u jetson") echo 1000 ;;
  "-g jetson") echo 1000 ;;
  *) /usr/bin/id "$@" ;;
esac
"""
FAKE_PYTHON = '#!/bin/bash\n[ -f "$FAKE_DIR/no_deps" ] && exit 1\nexit 0\n'


def _script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "fake"
    fake.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for name, body in (
        ("docker", FAKE_DOCKER),
        ("systemctl", FAKE_SYSTEMCTL),
        ("ss", FAKE_SS),
        ("id", FAKE_ID),
        ("fake_python", FAKE_PYTHON),
    ):
        _script(fakebin / name, body)
    (fake / "inspect.json").write_text(
        json.dumps(
            [
                {
                    "State": {"Running": True},
                    "HostConfig": {"NetworkMode": "host"},
                    "Mounts": [
                        {"Source": str(REPO), "Destination": "/home/fabian/ros2_ws/src"},
                        {"Source": "/dev", "Destination": "/dev"},
                    ],
                    "Config": {"Cmd": COMPOSE_CMD},
                }
            ]
        )
    )
    (fake / "groups").write_text("jetson sudo docker video\n")
    (fake / "container_env.txt").write_text("HOME=/home/fabian\nROS_DISTRO=humble\n")
    (fake / "container_uid").write_text("1000\n")
    (fake / "container_gid").write_text("1000\n")

    etc = tmp_path / "etc"
    (etc / "guide-kiosk").mkdir(parents=True)
    (etc / "guide-kiosk" / "url").write_text("http://127.0.0.1:8091\n")
    binp = tmp_path / "usrbin"
    binp.mkdir()
    shutil.copy(HDMI_SCRIPT, binp / "guide-kiosk-hdmi")
    (binp / "guide-kiosk-face").write_text(
        '#!/bin/bash\nURL=$(cat /etc/guide-kiosk/url)\nexec firefox --kiosk "$URL"\n'
    )
    environ = dict(
        os.environ,
        PATH=f"{fakebin}:{os.environ['PATH']}",
        FAKE_DIR=str(fake),
        ETC_DIR=str(etc),
        BIN_DIR=str(binp),
        OPT_DIR=str(tmp_path / "opt"),
        UNIT_DIR=str(tmp_path / "units"),
        HOST_PYTHON=str(fakebin / "fake_python"),
    )
    return environ, fake, tmp_path


def run(environ, *args, **extra):
    return subprocess.run(
        ["bash", str(INSTALL), *args],
        env={**environ, **extra},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_syntax_and_shellcheck():
    assert subprocess.run(["bash", "-n", str(INSTALL)]).returncode == 0
    if shutil.which("shellcheck"):
        assert subprocess.run(["shellcheck", "-S", "error", str(INSTALL)]).returncode == 0


def test_all_good(env):
    environ, _, _ = env
    r = run(environ, "--preflight-only")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Preflight пройден" in r.stdout
    assert "токен 0640" in r.stdout


def test_cmd_mismatch(env):
    environ, fake, _ = env
    data = json.loads((fake / "inspect.json").read_text())
    data[0]["Config"]["Cmd"] = ["/bin/bash", "-lc", "старый command"]
    (fake / "inspect.json").write_text(json.dumps(data))
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "docker compose up -d --force-recreate jetson" in r.stdout


def test_wrong_network_or_mount(env):
    environ, fake, _ = env
    data = json.loads((fake / "inspect.json").read_text())
    data[0]["HostConfig"]["NetworkMode"] = "bridge"
    data[0]["Mounts"] = []
    (fake / "inspect.json").write_text(json.dumps(data))
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "NetworkMode=bridge" in r.stdout
    assert "не смонтирован" in r.stdout


def test_container_missing(env):
    environ, fake, _ = env
    (fake / "inspect.json").write_text("[]")
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "не найден" in r.stdout


def test_missing_docker_group(env):
    environ, fake, _ = env
    (fake / "groups").write_text("jetson sudo video\n")
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "usermod -aG docker jetson" in r.stdout


def test_face_kiosk_not_reading_url(env):
    environ, _, tmp = env
    (tmp / "usrbin" / "guide-kiosk-face").write_text(
        "#!/bin/bash\nexec firefox --kiosk http://x\n"
    )
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "берёт адрес не из /etc/guide-kiosk/url" in r.stdout


def test_ros_env_set_in_container(env):
    environ, fake, _ = env
    (fake / "container_env.txt").write_text("ROS_DOMAIN_ID=42\nHOME=/home/fabian\n")
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "ROS_DOMAIN_ID=42" in r.stdout


def test_port_taken(env):
    environ, fake, _ = env
    (fake / "ss.txt").write_text("LISTEN 0 128 127.0.0.1:8089 0.0.0.0:*\n")
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "порт 8089 занят другим процессом" in r.stdout


def test_port_held_by_launcher_is_fine(env):
    environ, fake, _ = env
    (fake / "ss.txt").write_text("LISTEN 0 128 127.0.0.1:8089 0.0.0.0:*\n")
    (fake / "launcher_active").write_text("")
    assert run(environ, "--preflight-only").returncode == 0


def test_uid_mismatch_gives_0644(env):
    environ, fake, _ = env
    (fake / "container_uid").write_text("1001\n")
    r = run(environ, "--preflight-only")
    assert r.returncode == 0, r.stdout
    assert "токен 0644" in r.stdout


def test_unknown_installed_kiosk_script_stops_with_diff(env):
    environ, _, tmp = env
    (tmp / "usrbin" / "guide-kiosk-hdmi").write_text("#!/usr/bin/python3\nprint('чужой')\n")
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "не совпадает ни с одной версией" in r.stdout
    assert "чужой" in r.stdout


def test_missing_python_deps_reported_without_installing(env):
    environ, fake, _ = env
    (fake / "no_deps").write_text("")
    r = run(environ, "--preflight-only")
    assert r.returncode == 1
    assert "python3-aiohttp" in r.stdout


def test_dry_run_changes_nothing(env):
    environ, fake, tmp = env
    token_dir = REPO / ".guide_launcher"
    existed = token_dir.exists()
    url_before = (tmp / "etc" / "guide-kiosk" / "url").read_text()
    r = run(environ, DRY_RUN="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY:" in r.stdout
    assert token_dir.exists() == existed
    assert (tmp / "etc" / "guide-kiosk" / "url").read_text() == url_before
    assert not (tmp / "etc" / "guide-kiosk" / "url-hdmi").exists()
    assert not (tmp / "opt").exists()
    assert not (tmp / "units").exists()
    assert not (tmp / "etc" / "guide-launcher").exists()
    assert not (fake / "systemctl.log").exists()

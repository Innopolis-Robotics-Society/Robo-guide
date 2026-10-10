from __future__ import annotations

from pathlib import Path

import pytest
from guide_launcher.__main__ import main
from guide_launcher.config import ConfigError, load_config


def _write(tmp_path: Path, text: str, token: str | None = "secret") -> Path:
    if token is not None:
        (tmp_path / "token").write_text(token, encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"bridge_token_file: {tmp_path / 'token'}\n{text}", encoding="utf-8")
    return cfg


def test_defaults_merge_and_expanduser(tmp_path):
    cfg = load_config(_write(tmp_path, 'operator_pin: "12345678"\nhttp_port: 9000\n'))
    assert cfg.http_port == 9000
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.container == "robo-guide-jetson-1"
    assert cfg.autostart_stack is False
    assert cfg.auth_backends == ("rfid", "pin")
    assert "~" not in cfg.state_dir and "~" not in cfg.promo_dir
    assert cfg.stack_control is True
    assert cfg.bridge_token() == "secret"


def test_empty_container_disables_stack_control(tmp_path):
    cfg = load_config(_write(tmp_path, 'operator_pin: "12345678"\ncontainer: ""\n'))
    assert cfg.stack_control is False


@pytest.mark.parametrize("pin", ['"1234567"', '""'])
def test_short_or_missing_pin_is_rejected(tmp_path, pin):
    with pytest.raises(ConfigError, match="operator_pin"):
        load_config(_write(tmp_path, f"operator_pin: {pin}\n"))


def test_default_pin_is_not_accepted(tmp_path):
    with pytest.raises(ConfigError, match="operator_pin"):
        load_config(_write(tmp_path, ""))


@pytest.mark.parametrize("token", ["", "  \n"])
def test_empty_token_file_is_rejected(tmp_path, token):
    with pytest.raises(ConfigError, match="пуст"):
        load_config(_write(tmp_path, 'operator_pin: "12345678"\n', token=token))


def test_missing_token_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="bridge_token_file"):
        load_config(_write(tmp_path, 'operator_pin: "12345678"\n', token=None))


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="bogus"):
        load_config(_write(tmp_path, 'operator_pin: "12345678"\nbogus: 1\n'))


def test_missing_config_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_main_exits_2_on_bad_config(tmp_path, capsys):
    path = _write(tmp_path, 'operator_pin: "short"\n')
    assert main(["--config", str(path)]) == 2
    assert "operator_pin" in capsys.readouterr().err


def test_main_exits_2_on_empty_token(tmp_path):
    path = _write(tmp_path, 'operator_pin: "12345678"\n', token="")
    assert main(["--config", str(path)]) == 2


def test_example_config_is_valid_apart_from_paths(tmp_path):
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    text = example.read_text(encoding="utf-8").replace(
        "~/Desktop/Projects/Robo-guide/.guide_launcher/bridge_token", str(tmp_path / "token")
    )
    (tmp_path / "token").write_text("t", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.http_port == 8089 and cfg.default_tour == "expo_one"

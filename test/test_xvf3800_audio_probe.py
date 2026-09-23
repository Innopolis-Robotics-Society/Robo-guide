import math
import wave
from array import array

from scripts.xvf3800_audio_probe import (
    Endpoint,
    analyse_wav,
    is_xvf3800,
    parse_device_list,
    parse_udev_properties,
    select_endpoint,
    write_stereo_test_tone,
)

CAPTURE_LIST = """\
**** List of CAPTURE Hardware Devices ****
card 0: sofhdadsp [sof-hda-dsp], device 0: HDA Analog (*) []
  Subdevices: 1/1
card 4: Array [reSpeaker XVF3800 4-Mic Array], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""


def test_parse_and_select_official_alsa_example():
    endpoints = parse_device_list(CAPTURE_LIST)

    assert len(endpoints) == 2
    assert endpoints[1] == Endpoint(4, "Array", "reSpeaker XVF3800 4-Mic Array", 0, "USB Audio")
    assert endpoints[1].hw_name == "hw:CARD=Array,DEV=0"
    assert select_endpoint(endpoints, None) == endpoints[1]
    assert select_endpoint(endpoints, "Array") == endpoints[1]
    assert select_endpoint(endpoints, "4") == endpoints[1]


def test_select_by_usb_serial():
    endpoints = parse_device_list(CAPTURE_LIST)
    properties = {4: {"ID_SERIAL_SHORT": "101991441262500122"}}

    assert select_endpoint(endpoints, None, "101991441262500122", properties) == endpoints[1]
    assert select_endpoint(endpoints, None, "another-device", properties) is None


def test_parse_udev_properties_preserves_serial():
    output = """\
ID_VENDOR_ID=2886
ID_MODEL_ID=001a
ID_SERIAL_SHORT=101991441262500122
"""

    assert parse_udev_properties(output) == {
        "ID_VENDOR_ID": "2886",
        "ID_MODEL_ID": "001a",
        "ID_SERIAL_SHORT": "101991441262500122",
    }


def test_xvf_name_variants_and_unrelated_card():
    assert is_xvf3800(Endpoint(1, "XVF3800", "USB array", 0, "USB Audio"))
    assert is_xvf3800(Endpoint(1, "Array", "reSpeaker 3800", 0, "USB Audio"))
    assert not is_xvf3800(Endpoint(0, "sofhdadsp", "sof-hda-dsp", 0, "HDA Analog"))


def test_stereo_tone_uses_both_channels_separately(tmp_path):
    path = tmp_path / "tone.wav"
    write_stereo_test_tone(path, rate=16000)

    with wave.open(str(path), "rb") as source:
        assert source.getnchannels() == 2
        assert source.getsampwidth() == 2
        assert source.getframerate() == 16000
        values = array("h", source.readframes(source.getnframes()))

    midpoint = len(values) // 2
    assert max(abs(value) for value in values[0:midpoint:2]) > 0
    assert max(abs(value) for value in values[1:midpoint:2]) == 0
    assert max(abs(value) for value in values[midpoint::2]) == 0
    assert max(abs(value) for value in values[midpoint + 1 :: 2]) > 0


def test_analyse_wav_reports_expected_full_scale_fraction(tmp_path):
    path = tmp_path / "levels.wav"
    samples = array("h", [16384, 8192, -16384, -8192])
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(samples.tobytes())

    levels = analyse_wav(path)

    assert math.isclose(levels[0][0], -6.0206, abs_tol=0.001)
    assert math.isclose(levels[0][1], -6.0206, abs_tol=0.001)
    assert math.isclose(levels[1][0], -12.0412, abs_tol=0.001)
    assert math.isclose(levels[1][1], -12.0412, abs_tol=0.001)

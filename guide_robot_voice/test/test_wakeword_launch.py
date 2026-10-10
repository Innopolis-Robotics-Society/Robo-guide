"""Проверить реальные ParameterFile/substitutions без исполнения ROS-процессов."""

import importlib.util
from pathlib import Path

import pytest
import yaml
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument
from launch.utilities import perform_substitutions


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("barge_opt_in", [False, True])
def test_launch_switches_all_three_input_gate_components_together(enabled, barge_opt_in):
    path = Path(__file__).resolve().parents[1] / "launch/xvf3800_voice.launch.py"
    spec = importlib.util.spec_from_file_location("xvf_voice_launch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = LaunchContext()
    context.launch_configurations["wakeword_enabled"] = str(enabled).lower()
    if barge_opt_in:
        context.launch_configurations.update(
            {
                "wakeword_barge_in_enabled": "true",
                "allow_unvalidated_wakeword_barge_in": "true",
                "followup_window_seconds": "4.5",
            }
        )
    description = module.generate_launch_description()
    for entity in description.entities:
        if isinstance(entity, DeclareLaunchArgument):
            entity.execute(context)
    parameters = {}
    for entity in description.entities:
        if not isinstance(entity, Node):
            continue
        # Introspection deliberately avoids Node.execute(), which spawns a process.
        raw_name = entity._Node__node_name
        name = raw_name if isinstance(raw_name, str) else perform_substitutions(context, raw_name)
        merged = {}
        for item in evaluate_parameters(context, entity._Node__parameters):
            if isinstance(item, dict):
                merged.update(item)
            else:
                data = yaml.safe_load(Path(item).read_text())
                merged.update(data.get(f"/**/{name}", {}).get("ros__parameters", {}))
        parameters[name] = merged
    assert parameters["wakeword_node"]["backend"] == ("oww" if enabled else "asr_kws")
    assert parameters["voice_session_manager"].get("wakeword_gated", False) == enabled
    assert parameters["asr_node"].get("wakeword_gated_input", False) == enabled
    assert parameters["asr_node"]["session_managed_input"] is True
    assert parameters["asr_node"]["pre_roll_ms"] == (1200.0 if enabled else 500.0)
    assert parameters["voice_session_manager"]["automatic_barge_in_enabled"] is False
    assert parameters["voice_session_manager"]["audio_profile_validated"] is False
    assert parameters["voice_session_manager"]["followup_window_seconds"] == (
        4.5 if barge_opt_in else 8.0
    )
    assert parameters["voice_session_manager"]["wakeword_barge_in_enabled"] == barge_opt_in
    assert (
        parameters["voice_session_manager"]["allow_unvalidated_wakeword_barge_in"] == barge_opt_in
    )


def test_trigger_only_launch_has_no_asr_tts_vad_or_session_manager():
    path = Path(__file__).resolve().parents[1] / "launch/xvf3800_wakeword.launch.py"
    spec = importlib.util.spec_from_file_location("xvf_wakeword_launch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = LaunchContext()
    description = module.generate_launch_description()
    for entity in description.entities:
        if isinstance(entity, DeclareLaunchArgument):
            entity.execute(context)
    parameters = {}
    for entity in description.entities:
        if not isinstance(entity, Node):
            continue
        raw_name = entity._Node__node_name
        name = raw_name if isinstance(raw_name, str) else perform_substitutions(context, raw_name)
        merged = {}
        for item in evaluate_parameters(context, entity._Node__parameters):
            if isinstance(item, dict):
                merged.update(item)
            else:
                data = yaml.safe_load(Path(item).read_text())
                merged.update(data.get(f"/**/{name}", {}).get("ros__parameters", {}))
        parameters[name] = merged
        if name == "wakeword_node":
            remappings = [
                (perform_substitutions(context, source), perform_substitutions(context, target))
                for source, target in entity._Node__remappings
            ]
            assert ("/asr/partial", "/wakeword_test/asr/partial") in remappings
            assert ("/asr/transcript", "/wakeword_test/asr/transcript") in remappings
            assert ("/speech/cancel_all", "/wakeword_test/speech/cancel_all") in remappings
    assert set(parameters) == {
        "xvf3800_audio_node",
        "wakeword_node",
        "lifecycle_manager_wakeword_test",
    }
    assert parameters["wakeword_node"]["backend"] == "oww"
    assert Path(parameters["wakeword_node"]["model_bundle_path"]).is_dir()
    assert parameters["lifecycle_manager_wakeword_test"] == {
        "node_names": ("xvf3800_audio_node", "wakeword_node"),
        "autostart": True,
        "bond_timeout": 0.0,
    }

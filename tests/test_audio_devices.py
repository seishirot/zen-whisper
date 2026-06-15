"""Audio input device discovery and resolution tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import audio_devices


HOSTAPIS = (
    {"name": "MME"},
    {"name": "Windows DirectSound"},
    {"name": "Windows WASAPI"},
    {"name": "Windows WDM-KS"},
)


def _device(name: str, hostapi: int, inputs: int = 2, sr: float = 48000.0) -> dict:
    return {
        "name": name,
        "hostapi": hostapi,
        "max_input_channels": inputs,
        "max_output_channels": 0,
        "default_samplerate": sr,
    }


@pytest.fixture
def fake_sounddevice(monkeypatch):
    devices = [
        _device("Speakers", 0, inputs=0),
        _device("マイク (Anker PowerConf C200)", 0, sr=44100.0),
        _device("マイク (NVIDIA Broadcast)", 0, sr=44100.0),
        _device("マイク (NVIDIA Broadcast)", 1, sr=44100.0),
        _device("マイク (NVIDIA Broadcast)", 2, sr=48000.0),
        _device("マイク (NVIDIA Broadcast)", 3, sr=48000.0),
        _device("Vocaster mic (Focusrite USB Audio)", 1, inputs=8, sr=44100.0),
        _device("MOTIV Mix Virtual Output (Shure", 0, sr=44100.0),
        _device("MOTIV Mix Virtual Output (Shure Virtual Audio)", 1, sr=44100.0),
        _device("Primary Sound Capture Driver", 1, sr=44100.0),
        _device("Input (Steam Streaming Speakers Wave)", 3, inputs=8, sr=44100.0),
        _device("ヘッドセット (Bose QC Ultra Headphones)", 1, sr=16000.0),
        _device("Mono Only USB Mic", 1, sr=16000.0),
    ]
    supported_rates = {
        1: {16000, 44100},
        2: {16000, 44100},
        3: {16000, 44100},
        4: {48000},
        5: set(),
        6: {16000, 44100},
        7: {16000, 44100},
        8: {16000, 44100},
        9: {16000, 44100},
        10: {16000, 44100},
        11: {16000},
        12: {16000},
    }

    def query_devices(device=None, kind=None):
        if kind == "input":
            result = dict(devices[1])
            result["index"] = 1
            return result
        if device is not None:
            return devices[device]
        return devices

    def query_hostapis(index=None):
        if index is None:
            return HOSTAPIS
        return HOSTAPIS[index]

    def check_input_settings(device=None, channels=None, dtype=None, samplerate=None):
        if device == 12 and channels != 1:
            raise ValueError("Only mono is supported")
        if int(samplerate) not in supported_rates.get(device, set()):
            raise ValueError("Invalid sample rate")

    monkeypatch.setattr(audio_devices.sd, "query_devices", query_devices)
    monkeypatch.setattr(audio_devices.sd, "query_hostapis", query_hostapis)
    monkeypatch.setattr(audio_devices.sd, "check_input_settings", check_input_settings)
    monkeypatch.setattr(audio_devices.sd, "default", SimpleNamespace(device=[1, 0]))
    monkeypatch.setattr(audio_devices, "is_windows", lambda: True)
    monkeypatch.setattr(audio_devices, "_active_capture_device_names", lambda: None)

    return devices


def test_empty_microphone_uses_os_default(fake_sounddevice) -> None:
    result = audio_devices.resolve_input_device("", 16000)

    assert result.stream_device is None
    assert result.actual_index == 1
    assert result.name == "マイク (Anker PowerConf C200)"
    assert result.hostapi == "MME"
    assert result.stream_sample_rate == 16000
    assert result.fallback_used is False


def test_nvidia_broadcast_prefers_wasapi_with_default_sample_rate(fake_sounddevice) -> None:
    result = audio_devices.resolve_input_device("マイク (NVIDIA Broadcast)", 16000)

    assert result.stream_device == 4
    assert result.actual_index == 4
    assert result.hostapi == "Windows WASAPI"
    assert result.stream_sample_rate == 48000
    assert result.fallback_used is False


def test_unsupported_target_rate_can_still_use_default_rate(fake_sounddevice) -> None:
    names = audio_devices.list_microphone_names(16000)
    result = audio_devices.resolve_input_device("NVIDIA Broadcast", 16000)

    assert names.count("マイク (NVIDIA Broadcast)") == 1
    assert result.stream_device == 4
    assert result.hostapi == "Windows WASAPI"
    assert result.stream_sample_rate == 48000


def test_truncated_windows_names_are_merged_into_longer_display_name(fake_sounddevice) -> None:
    names = audio_devices.list_microphone_names(16000)

    assert "MOTIV Mix Virtual Output (Shure Virtual Audio)" in names
    assert "MOTIV Mix Virtual Output (Shure" not in names


def test_windows_menu_hides_pseudo_and_low_level_devices(fake_sounddevice) -> None:
    names = audio_devices.list_microphone_names(16000)

    assert "Primary Sound Capture Driver" not in names
    assert "Input (Steam Streaming Speakers Wave)" not in names


def test_windows_menu_hides_inactive_registry_endpoints(fake_sounddevice, monkeypatch) -> None:
    monkeypatch.setattr(
        audio_devices,
        "_active_capture_device_names",
        lambda: {
            "マイク (NVIDIA Broadcast)",
            "Vocaster mic (Focusrite USB Audio)",
            "MOTIV Mix Virtual Output (Shure Virtual Audio)",
        },
    )

    names = audio_devices.list_microphone_names(16000)

    assert "マイク (NVIDIA Broadcast)" in names
    assert "Vocaster mic (Focusrite USB Audio)" in names
    assert "ヘッドセット (Bose QC Ultra Headphones)" not in names


def test_empty_active_registry_names_do_not_hide_all_devices(
    fake_sounddevice,
    monkeypatch,
) -> None:
    monkeypatch.setattr(audio_devices, "_active_capture_device_names", lambda: set())

    names = audio_devices.list_microphone_names(16000)

    assert "マイク (NVIDIA Broadcast)" in names
    assert "Vocaster mic (Focusrite USB Audio)" in names


def test_inactive_registry_endpoint_is_unavailable_for_resolution(
    fake_sounddevice,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        audio_devices,
        "_active_capture_device_names",
        lambda: {
            "マイク (NVIDIA Broadcast)",
            "Vocaster mic (Focusrite USB Audio)",
            "MOTIV Mix Virtual Output (Shure Virtual Audio)",
        },
    )

    result = audio_devices.resolve_input_device("Bose QC Ultra", 16000)

    assert not audio_devices.microphone_available("Bose QC Ultra", 16000)
    assert result.fallback_used is True
    assert result.stream_device is None


def test_stereo_rejected_device_falls_back_to_mono_stream(fake_sounddevice) -> None:
    result = audio_devices.resolve_input_device("Mono Only USB", 16000)

    assert result.stream_device == 12
    assert result.stream_channels == 1
    assert result.fallback_used is False


def test_missing_selected_microphone_falls_back_to_default(fake_sounddevice) -> None:
    result = audio_devices.resolve_input_device("Missing Mic", 16000)

    assert result.stream_device is None
    assert result.actual_index == 1
    assert result.stream_sample_rate == 16000
    assert result.fallback_used is True
    assert result.configured == "Missing Mic"
    assert "Missing Mic" in (result.warning_message or "")


def test_numeric_device_id_is_preserved_when_supported(fake_sounddevice) -> None:
    result = audio_devices.resolve_input_device("6", 16000)

    assert result.stream_device == 6
    assert result.name == "Vocaster mic (Focusrite USB Audio)"
    assert result.fallback_used is False


def test_legacy_partial_match_still_resolves(fake_sounddevice) -> None:
    result = audio_devices.resolve_input_device("Focusrite", 16000)

    assert result.stream_device == 6
    assert result.name == "Vocaster mic (Focusrite USB Audio)"

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_audio_device_manager_enumerates_and_applies_coreaudio_input_devices() -> None:
    manager = (SWIFT_SRC / "AudioDeviceManager.swift").read_text(encoding="utf-8")
    capture = (SWIFT_SRC / "HALInputCapture.swift").read_text(encoding="utf-8")

    assert "struct AudioInputDevice: Equatable" in manager
    assert "static func inputDevices() -> [AudioInputDevice]" in manager
    assert "kAudioHardwarePropertyDevices" in manager
    assert "kAudioDevicePropertyStreamConfiguration" in manager
    assert "kAudioDevicePropertyScopeInput" in manager
    assert "kAudioDevicePropertyDeviceUID" in manager
    assert "static func resolveInputDeviceID(uid: String?) throws -> AudioDeviceID?" in manager
    assert "AVAudioInputNode" not in manager
    assert "kAudioOutputUnitProperty_CurrentDevice" in capture
    assert "AudioUnitSetProperty" in capture
    # The device must be bound before the unit is initialized; otherwise the
    # default input (e.g. a Bluetooth headset) is opened and drops into HFP.
    assert capture.index("kAudioOutputUnitProperty_CurrentDevice") < capture.index("AudioUnitInitialize(unit)")

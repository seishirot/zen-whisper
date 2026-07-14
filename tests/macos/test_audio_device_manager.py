from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_audio_device_manager_enumerates_and_applies_coreaudio_input_devices() -> None:
    manager = (SWIFT_SRC / "AudioDeviceManager.swift").read_text(encoding="utf-8")

    assert "struct AudioInputDevice: Equatable" in manager
    assert "static func inputDevices() -> [AudioInputDevice]" in manager
    assert "kAudioHardwarePropertyDevices" in manager
    assert "kAudioDevicePropertyStreamConfiguration" in manager
    assert "kAudioDevicePropertyScopeInput" in manager
    assert "kAudioDevicePropertyDeviceUID" in manager
    assert "static func applyInputDevice(uid: String?, to inputNode: AVAudioInputNode) throws" in manager
    assert "kAudioOutputUnitProperty_CurrentDevice" in manager
    assert "AudioUnitSetProperty" in manager

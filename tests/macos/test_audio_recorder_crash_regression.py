from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIO_RECORDER = REPO_ROOT / "macos/ZenWhisper/ZenWhisper/AudioRecorder.swift"


def test_audio_recorder_does_not_write_avaudiofile_from_realtime_tap() -> None:
    text = AUDIO_RECORDER.read_text(encoding="utf-8")

    assert "AVAudioFile" not in text
    assert "AVAudioConverter" not in text
    assert "write(from:" not in text
    assert "writeWav16kMonoPCM16" in text
    assert "input.installTap" in text
    assert "AudioDeviceManager.applyInputDevice" in text


def test_audio_recorder_writes_16khz_mono_pcm16_wav_header() -> None:
    text = AUDIO_RECORDER.read_text(encoding="utf-8")

    assert 'data.appendASCII("RIFF")' in text
    assert 'data.appendASCII("WAVE")' in text
    assert 'data.appendUInt32LE(16_000)' in text
    assert "data.appendUInt16LE(1)" in text
    assert "data.appendUInt16LE(16)" in text

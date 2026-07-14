from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_recording_status_tracks_voice_activity() -> None:
    app_state = (SWIFT_SRC / "AppState.swift").read_text(encoding="utf-8")
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")
    status_icon = (SWIFT_SRC / "StatusIconFactory.swift").read_text(encoding="utf-8")

    assert "case recording(elapsed: TimeInterval, voiceActive: Bool)" in app_state
    assert "setState(.recording(elapsed: 0, voiceActive: false))" in app_delegate
    assert "setState(.recording(elapsed: level.elapsed, voiceActive: level.voiceActive))" in app_delegate
    assert "Recording \\(StatusText.elapsed(elapsed)) - voice" in status_controller
    assert "Recording \\(StatusText.elapsed(elapsed)) - listening" in status_controller
    assert "case recordingSilent" in status_icon
    assert "case recordingSpeech" in status_icon
    assert "voiceActive ? .recordingSpeech : .recordingSilent" in status_icon
    assert "NSColor.systemRed" in status_icon
    assert "NSColor.systemGreen" in status_icon


def test_silence_auto_stop_uses_audio_level_snapshot() -> None:
    recorder = (SWIFT_SRC / "AudioRecorder.swift").read_text(encoding="utf-8")
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")

    assert "func levelSnapshot() -> RecordingLevelSnapshot" in recorder
    assert "private static let settings = RMSSettings()" in recorder
    assert "static let autoStopSilence: TimeInterval = settings.silenceAutoStopSeconds" in recorder
    assert "static let maxDuration: TimeInterval = settings.maxRecordingSeconds" in recorder
    assert "static let minDuration: TimeInterval = settings.minRecordingSeconds" in recorder
    assert "static let speechRMS: Float = settings.speechStartRMS" in recorder
    assert "RMSAnalyzer.rms(samples)" in recorder
    assert "let reachedMaxDuration = elapsed >= RecordingLevelDefaults.maxDuration" in recorder
    assert "reachedMaxDuration: reachedMaxDuration" in recorder
    assert "level.shouldAutoStop" in app_delegate
    assert "level.reachedMaxDuration || (settings.silenceAutoStopEnabled && level.shouldAutoStop)" in app_delegate
    assert "settings.silenceAutoStopEnabled" in app_delegate
    assert "recorder.start(deviceUID: settings.microphoneDeviceUID)" in app_delegate

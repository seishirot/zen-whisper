from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DELEGATE = REPO_ROOT / "macos/ZenWhisper/ZenWhisper/AppDelegate.swift"


def test_copied_status_is_transient_and_returns_ready() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "private var statusResetTimer: Timer?" in text
    assert "private func setCopiedTransient(pasteDispatched: Bool, reason: String?)" in text
    assert "setState(.copied(pasteDispatched: pasteDispatched, reason: reason))" in text
    assert 'logInfo("transcript copied; paste not verified: \\(reason ?? "unknown")")' in text
    assert 'logInfo("transcript copied; paste verified: \\(reason ?? "unknown")")' in text
    assert "selector: #selector(resetCopiedState(_:))" in text
    assert "@objc private func resetCopiedState(_ timer: Timer)" in text
    assert "setState(readyState())" in text
    assert "RunLoop.main.add(resetTimer, forMode: .common)" in text


def test_copy_failed_status_is_transient_and_returns_ready() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "private func setCopyFailedTransient(_ reason: String)" in text
    assert "setState(.copyFailed(reason))" in text
    assert "selector: #selector(resetCopyFailedState(_:))" in text
    assert "guard case .copyFailed = state else" in text
    assert "setState(readyState())" in text


def test_copy_skipped_status_is_transient_and_returns_ready() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "private func setCopySkippedTransient(_ reason: String)" in text
    assert "setState(.copySkipped(reason))" in text
    assert "selector: #selector(resetCopySkippedState(_:))" in text
    assert "guard case .copySkipped = state else" in text
    assert "setState(readyState())" in text


def test_recording_timer_runs_in_common_run_loop_modes() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "selector: #selector(refreshRecordingTimer(_:))" in text
    assert "RunLoop.main.add(recordingTimer, forMode: .common)" in text
    assert "Timer.scheduledTimer(" not in text


def test_app_delegate_timers_do_not_capture_actor_isolated_self() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "selector: #selector(pollPasteTargetCache(_:))" in text
    assert "Timer(timeInterval: 0.2, repeats: true) { [weak self]" not in text
    assert "Timer(timeInterval: 0.75, repeats: true) { [weak self]" not in text

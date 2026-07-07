from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DELEGATE = REPO_ROOT / "macos/ZenWhisper/ZenWhisper/AppDelegate.swift"


def test_copied_status_is_transient_and_returns_ready() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "private var statusResetTimer: Timer?" in text
    assert "private func setCopiedTransient(pasteDispatched: Bool, reason: String?)" in text
    assert "setState(.copied(pasteDispatched: pasteDispatched, reason: reason))" in text
    assert 'logInfo("transcript copied; paste skipped: \\(reason ?? "unknown")")' in text
    assert 'logInfo("transcript copied; paste sent: \\(reason ?? "unknown")")' in text
    assert "self.setState(self.readyState())" in text
    assert "RunLoop.main.add(resetTimer, forMode: .common)" in text


def test_copy_failed_status_is_transient_and_returns_ready() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "private func setCopyFailedTransient(_ reason: String)" in text
    assert "setState(.copyFailed(reason))" in text
    assert "guard let self, case .copyFailed = self.state else" in text
    assert "self.setState(self.readyState())" in text


def test_copy_skipped_status_is_transient_and_returns_ready() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "private func setCopySkippedTransient(_ reason: String)" in text
    assert "setState(.copySkipped(reason))" in text
    assert "guard let self, case .copySkipped = self.state else" in text
    assert "self.setState(self.readyState())" in text


def test_recording_timer_runs_in_common_run_loop_modes() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")

    assert "Timer(timeInterval: 0.2, repeats: true)" in text
    assert "RunLoop.main.add(recordingTimer, forMode: .common)" in text
    assert "Timer.scheduledTimer(" not in text

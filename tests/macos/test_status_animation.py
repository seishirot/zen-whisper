from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_processing_status_item_uses_animation_timer() -> None:
    controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")

    assert "private var animationTimer: Timer?" in controller
    assert "Timer(timeInterval: 0.12" in controller
    assert "RunLoop.main.add(timer, forMode: .common)" in controller
    assert (
        "case .preloading, .transcribing, .postprocessing, "
        ".repairingBackend:"
    ) in controller
    assert "StatusIconFactory.image(for: currentState, animationFrame: frame)" in controller


def test_processing_icon_rotates_arc_by_animation_frame() -> None:
    factory = (SWIFT_SRC / "StatusIconFactory.swift").read_text(encoding="utf-8")

    assert "static func image(for state: AppState, animationFrame: Int)" in factory
    assert "static func image(kind: StatusIconKind, animationFrame: Int)" in factory
    assert "let startAngle = CGFloat((360 - ((animationFrame * 15) % 360)) % 360)" in factory
    assert "endAngle: startAngle + 270" in factory


def test_ready_icon_is_neutral_monogram_not_yellow() -> None:
    factory = (SWIFT_SRC / "StatusIconFactory.swift").read_text(encoding="utf-8")

    assert "case .inputWaiting:" in factory
    assert "drawMonogramIcon()" in factory
    assert "NSColor.labelColor.withAlphaComponent" in factory
    assert 'NSString(string: "Z")' in factory
    assert "NSBezierPath(roundedRect:" in factory
    assert "case .inputWaiting:\n            drawCircle(color: NSColor.systemYellow" not in factory


def test_status_title_uses_baseline_offset_and_ready_is_icon_only() -> None:
    controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")
    app_state = (SWIFT_SRC / "AppState.swift").read_text(encoding="utf-8")

    assert "private func statusTitle(_ title: String) -> NSAttributedString" in controller
    assert "guard !title.isEmpty else" in controller
    assert "title.isEmpty ? .imageOnly : .imageLeft" in controller
    assert ".baselineOffset: -1.0" in controller
    assert "NSFont.monospacedDigitSystemFont" in controller
    assert "statusItem.button?.attributedTitle = statusTitle(title)" in controller
    assert 'case .inputWaiting:\n            return ""' in app_state
    assert 'return "Ready"' not in app_state

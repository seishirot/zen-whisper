from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_copy_only_reasons_are_user_visible_and_logged() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")

    assert 'copyTranscriptWithoutPaste(trimmed, reason: "Accessibility not allowed")' in app_delegate
    assert "setCopySkippedTransient(reason)" in app_delegate
    assert 'copyTranscriptWithoutPaste(trimmed, reason: "paste event unavailable")' in app_delegate
    assert 'setCopyFailedTransient("pasteboard write failed")' in app_delegate
    assert 'logInfo("transcript copied; paste skipped: \\(reason ?? "unknown")")' in app_delegate
    assert 'logInfo("transcript copied; paste sent: \\(reason ?? "unknown")")' in app_delegate
    assert 'logInfo("transcript copy skipped: \\(reason)")' in app_delegate
    assert "Copy Skipped: \\(reason)" in status_controller
    assert "Copied; paste skipped: \\(reason)" in status_controller
    assert '"Paste sent\\(enterText); clipboard restored"' in status_controller
    assert 'enterText = "; Enter sent"' in status_controller
    assert '"Paste sent\\(enterText); clipboard kept"' in status_controller


def test_status_title_surfaces_short_copy_only_reason() -> None:
    app_state = (SWIFT_SRC / "AppState.swift").read_text(encoding="utf-8")

    assert "static func copyOnlyReason(_ reason: String) -> String" in app_state
    assert 'return "AX"' in app_state
    assert 'return "Changed"' in app_state
    assert 'return "Not editable"' in app_state
    assert 'return "Unsafe"' in app_state
    assert 'return "No start"' in app_state
    assert 'return "No stop"' in app_state
    assert 'return "No current"' in app_state
    assert '"Copied: \\(StatusText.copyOnlyReason($0))"' in app_state


def test_paste_dispatch_reports_event_creation_failure() -> None:
    paste_controller = (SWIFT_SRC / "PasteController.swift").read_text(encoding="utf-8")

    assert "func copy(_ text: String, restoreAfter delay: TimeInterval? = nil) -> Bool" in paste_controller
    assert "let previous = PasteboardSnapshot(pasteboard: pasteboard)" in paste_controller
    assert "guard pasteboard.setString(text, forType: .string) else" in paste_controller
    assert "writtenChangeCount: pasteboard.changeCount" in paste_controller
    assert "current.changeCount == token.writtenChangeCount" in paste_controller
    assert "current.string(forType: .string) == token.text" in paste_controller
    assert "DispatchQueue.main.asyncAfter(deadline: .now() + delay)" in paste_controller
    assert "self.restore(token)" in paste_controller
    assert "func restore(_ token: PasteboardRestoreToken)" in paste_controller
    assert "token.previous.restore(to: NSPasteboard.general)" in paste_controller
    assert "func copyForAutoPaste(_ text: String) -> PasteboardRestoreToken?" in paste_controller
    assert "func scheduleRestore(_ token: PasteboardRestoreToken, after delay: TimeInterval)" in paste_controller
    assert "func paste(to pid: pid_t) -> Bool" in paste_controller
    assert "func pressReturn(to pid: pid_t) -> Bool" in paste_controller
    assert "func canCreatePasteEvents() -> Bool" in paste_controller
    assert "private func postKeyEvents(to pid: pid_t, virtualKey: CGKeyCode, flags: CGEventFlags) -> Bool" in paste_controller
    assert "CGPreflightPostEventAccess()" in paste_controller
    assert "return false" in paste_controller
    assert "keyDown.postToPid(pid)" in paste_controller
    assert "return true" in paste_controller


def test_native_pasteboard_write_happens_only_after_paste_decision() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")

    decision_index = app_delegate.index("let decision = pasteController.decide(")
    paste_case_index = app_delegate.index("case .paste:")
    preflight_index = app_delegate.index("guard pasteController.canCreatePasteEvents() else")
    copy_index = app_delegate.index("guard let restoreToken = pasteController.copyForAutoPaste(trimmed) else")
    restore_index = app_delegate.index("pasteController.scheduleRestore(restoreToken, after: 1.0)")
    schedule_enter_index = app_delegate.index("scheduleSubmitReturn(to: current, pasteReason: pasteReason)")
    keep_index = app_delegate.index('pasteReason = "clipboard kept"')
    failure_restore_index = app_delegate.index("pasteController.restore(restoreToken)")
    copy_only_index = app_delegate.index("copyTranscriptWithoutPaste(trimmed, reason: reason)")

    assert decision_index < paste_case_index < preflight_index < copy_index
    assert copy_index < restore_index
    assert copy_index < schedule_enter_index
    assert copy_index < keep_index
    assert copy_index < failure_restore_index
    assert copy_only_index > copy_index
    assert 'copyTranscriptWithoutPaste(trimmed, reason: "Accessibility not allowed")' in app_delegate
    assert "guard settings.outputMode.shouldAttemptPaste else" in app_delegate
    assert 'copyTranscriptWithoutPaste(trimmed, reason: "output mode copy only")' in app_delegate
    assert "private func scheduleSubmitReturn(to approvedTarget: PasteTargetSnapshot, pasteReason: String)" in app_delegate
    assert "capturePasteTarget(stage: \"submit return\", allowCached: false)" in app_delegate
    assert "pasteController.decide(" in app_delegate
    assert "pasteController.pressReturn(to: approvedTarget.pid)" in app_delegate
    assert '"\\(pasteReason); enter skipped"' in app_delegate


def test_pasteboard_write_failure_has_distinct_state_and_restore_attempt() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    app_state = (SWIFT_SRC / "AppState.swift").read_text(encoding="utf-8")
    paste_controller = (SWIFT_SRC / "PasteController.swift").read_text(encoding="utf-8")
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")

    assert "private func setCopyFailedTransient(_ reason: String)" in app_delegate
    assert 'logInfo("transcript copy failed: \\(reason)")' in app_delegate
    assert "setState(.copyFailed(reason))" in app_delegate
    assert "case copyFailed(String)" in app_state
    assert "case copySkipped(String)" in app_state
    assert 'return "Copy failed"' in app_state
    assert 'return "Skipped: \\(StatusText.copyOnlyReason(reason))"' in app_state
    assert "case .copySkipped(let reason):" in status_controller
    assert "case .copyFailed(let message):" in status_controller
    assert "Copy Failed: \\(StatusText.visibleErrorSummary(message))" in status_controller
    assert "fileprivate struct PasteboardSnapshot" in paste_controller
    assert "struct PasteboardRestoreToken" in paste_controller
    assert "previous.restore(to: pasteboard)" in paste_controller


def test_paste_controller_keeps_specific_copy_only_reasons() -> None:
    paste_controller = (SWIFT_SRC / "PasteController.swift").read_text(encoding="utf-8")

    assert '.copyOnly("missing recording start AX target")' in paste_controller
    assert '.copyOnly("missing recording stop AX target")' in paste_controller
    assert 'return .copyOnly("missing current AX target")' in paste_controller
    assert 'return .skipCopy("target is unsafe")' in paste_controller
    assert 'return .copyOnly("target is not editable")' in paste_controller
    assert "guard isEligible(start), isEligible(stop), isEligible(current) else" in paste_controller
    assert 'return .copyOnly("target changed")' in paste_controller
    assert 'return .copyOnly("target changed during recording")' in paste_controller
    assert "AXSecureTextField" in paste_controller
    assert 'snapshot.role == "AXWebArea"' in paste_controller
    assert '"api key"' in paste_controller
    assert '"認証コード"' in paste_controller


def test_paste_target_snapshot_uses_system_wide_focus_before_frontmost_fallback() -> None:
    paste_controller = (SWIFT_SRC / "PasteController.swift").read_text(encoding="utf-8")

    system_wide_index = paste_controller.index("let systemWide = AXUIElementCreateSystemWide()")
    frontmost_index = paste_controller.index("NSWorkspace.shared.frontmostApplication")

    assert system_wide_index < frontmost_index
    assert "func snapshotFocusedTargetProbe() -> PasteTargetProbe" in paste_controller
    assert "kAXFocusedApplicationAttribute" in paste_controller
    assert "systemFocused=\\(describe(systemFocused))" in paste_controller
    assert "focusedAppElement=\\(describe(focused))" in paste_controller
    assert "frontmostElement=\\(describe(focused))" in paste_controller
    assert "kAXFocusedWindowAttribute" in paste_controller
    assert "kAXMainWindowAttribute" in paste_controller
    assert "snapshotEditableDescendant" in paste_controller
    assert "foundFocused" in paste_controller
    assert "unverified eligible=" in paste_controller
    assert '"AXVisibleChildren" as CFString' in paste_controller
    assert "AXUIElementGetPid(focused, &pid)" in paste_controller
    assert "NSRunningApplication.current.processIdentifier" in paste_controller
    assert "let enabled = copyBoolAttribute(focused, kAXEnabledAttribute as CFString) ?? true" in paste_controller
    assert "var redactedDescription: String" in paste_controller
    assert "bundleIdentifier" in paste_controller
    assert "windowTitle" in paste_controller
    assert "elementIdentifier" in paste_controller


def test_paste_decision_compares_start_stop_and_current_targets() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    paste_controller = (SWIFT_SRC / "PasteController.swift").read_text(encoding="utf-8")

    assert "private var pasteTargetAtRecordingStart: PasteTargetSnapshot?" in app_delegate
    assert 'pasteTargetAtRecordingStart = capturePasteTarget(stage: "recording start", allowCached: true)' in app_delegate
    assert 'let stopTarget = capturePasteTarget(stage: "recording stop", allowCached: false)' in app_delegate
    assert "startTarget: startTarget" in app_delegate
    assert "stopTarget: stopTarget" in app_delegate
    assert "func decide(\n        start: PasteTargetSnapshot?," in paste_controller
    assert "guard let stop else" in paste_controller
    assert "guard let start else" in paste_controller
    assert "guard sameTarget(start, stop) else" in paste_controller


def test_app_delegate_polls_paste_targets_for_recording_start_but_requires_fresh_stop_and_current() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")

    assert "var onMenuWillOpen: (() -> Void)?" in status_controller
    assert "func menuWillOpen(_ menu: NSMenu)" in status_controller
    assert "statusController.onMenuWillOpen = { [weak self] in" in app_delegate
    assert 'self?.rememberPasteTarget(stage: "menu open")' in app_delegate
    assert "self?.refreshLaunchAtLoginState()" in app_delegate
    assert "private var lastKnownPasteTarget: PasteTargetSnapshot?" in app_delegate
    assert 'capturePasteTarget(stage: "recording start", allowCached: true)' in app_delegate
    assert 'capturePasteTarget(stage: "recording stop", allowCached: false)' in app_delegate
    assert 'capturePasteTarget(stage: "transcription complete", allowCached: false)' in app_delegate
    assert "paste target reused at \\(stage)" in app_delegate
    assert "paste target missing at \\(stage): \\(probe.detail)" in app_delegate
    assert "private var pasteTargetCacheTimer: Timer?" in app_delegate
    assert "private func refreshPasteTargetCache(stage: String)" in app_delegate
    assert "private func updatePasteTargetCacheTimer(for state: AppState)" in app_delegate
    assert 'refreshPasteTargetCache(stage: "idle target poll")' in app_delegate
    assert "pasteController.snapshotFocusedTargetProbe().snapshot" in app_delegate
    assert "case .inputWaiting, .pasteUnavailable, .copied, .copySkipped, .copyFailed:" in app_delegate
    assert "cachePasteTargetIfEligible(snapshot, stage: stage, log: true)" in app_delegate


def test_swift_unit_tests_cover_paste_decision_matrix() -> None:
    core_tests = (REPO_ROOT / "macos/ZenWhisper/ZenWhisperTests/CoreTests.swift").read_text(encoding="utf-8")

    assert "func testPasteDecisionRequiresCompleteStableTargetHistory()" in core_tests
    assert "controller.decide(start: nil, stop: target, current: target),\n            .copyOnly(\"missing recording start AX target\")" in core_tests
    assert "controller.decide(start: nil, stop: nil, current: target),\n            .copyOnly(\"missing recording start AX target\")" in core_tests
    assert '.copyOnly("missing recording stop AX target")' in core_tests
    assert '.copyOnly("missing current AX target")' in core_tests
    assert '.copyOnly("target changed during recording")' in core_tests
    assert '.copyOnly("target changed")' in core_tests
    assert '.copyOnly("target is not editable")' in core_tests
    assert 'controller.decide(start: ineligible, stop: target, current: target)' in core_tests
    assert '.skipCopy("target is unsafe")' in core_tests
    assert 'controller.decide(start: unsafe, stop: target, current: target)' in core_tests
    assert 'controller.decide(start: unsafe, stop: nil, current: target)' in core_tests
    assert 'controller.decide(start: nil, stop: unsafe, current: target)' in core_tests
    assert 'controller.decide(start: nil, stop: nil, current: unsafe)' in core_tests
    assert 'pasteTarget(searchableText: "pinboard")' in core_tests
    assert ".paste" in core_tests
    assert "func testPasteEligibilityRejectsProtectedAndNonEditableTargets()" in core_tests


def test_copy_only_decisions_copy_without_auto_paste() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    app_state = (SWIFT_SRC / "AppState.swift").read_text(encoding="utf-8")
    paste_controller = (SWIFT_SRC / "PasteController.swift").read_text(encoding="utf-8")

    assert "case .skipCopy(let reason):" in app_delegate
    assert 'setCopySkippedTransient("target is unsafe")' in app_delegate
    assert "paste decision skip-copy before AX check: target is unsafe" in app_delegate
    assert "private func copyTranscriptWithoutPaste(_ text: String, reason: String)" in app_delegate
    assert "guard pasteController.copy(text) else" in app_delegate
    assert "setCopiedTransient(pasteDispatched: false, reason: reason)" in app_delegate
    assert "case skipCopy(String)" in paste_controller
    assert "func isUnsafeForClipboard(_ snapshot: PasteTargetSnapshot) -> Bool" in paste_controller
    assert "unsafeTermMatches" in paste_controller
    assert "isShortLatinTerm" in paste_controller
    assert "NSRegularExpression.escapedPattern" in paste_controller
    assert "guard [start, stop, current].compactMap({ $0 }).allSatisfy({ !isUnsafeForClipboard($0) }) else" in paste_controller
    assert "case pasteUnavailable(String)" in app_state
    assert 'case .pasteUnavailable:' in app_state
    assert 'return "AX"' in app_state


def test_paste_returns_true_only_after_event_post_path() -> None:
    paste_controller = (SWIFT_SRC / "PasteController.swift").read_text(encoding="utf-8")

    assert "private func postPasteEvents(to pid: pid_t) -> Bool" in paste_controller
    assert "return postPasteEvents(to: pid)" in paste_controller
    assert "return false" in paste_controller
    assert "keyDown.postToPid(pid)" in paste_controller
    assert "keyUp.postToPid(pid)" in paste_controller
    assert "return true" in paste_controller

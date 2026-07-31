from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def source(name: str) -> str:
    return (SWIFT_SRC / name).read_text(encoding="utf-8")


def test_paste_results_distinguish_verified_unconfirmed_and_blocked() -> None:
    app_delegate = source("AppDelegate.swift")
    app_state = source("AppState.swift")
    status_controller = source("StatusController.swift")
    coordinator = source("PasteAttemptCoordinator.swift")

    assert "enum PasteAttemptResult: Equatable" in coordinator
    assert "case pastedVerified(" in coordinator
    assert "case copiedForManualPaste(reason: PasteFailureReason)" in coordinator
    assert "case blocked(reason: PasteBlockReason)" in coordinator
    assert "private func applyPasteAttemptReport" in app_delegate
    assert 'return "paste not confirmed; clipboard kept"' in app_delegate
    assert 'return "Pasted"' in app_state
    assert 'return "Copied · Paste not confirmed"' in app_state
    assert 'return "Copied · No editable target"' in app_state
    assert 'return "Blocked · Secure field"' in app_state
    assert "Paste verified" in status_controller
    assert "transcript kept for manual paste" in status_controller
    assert "Paste tried" not in app_state


def test_pasteboard_restore_requires_attempt_ownership() -> None:
    paste_controller = source("PasteController.swift")
    coordinator = source("PasteAttemptCoordinator.swift")

    assert '"app.zen-whisper.paste-attempt"' in paste_controller
    assert "attemptID.uuidString" in paste_controller
    assert "pasteboard.changeCount == token.writtenChangeCount" in paste_controller
    assert "pasteboard.string(forType: .string) == token.text" in paste_controller
    assert "func restoreIfOwned" in paste_controller
    assert "case ownershipLost" in paste_controller
    assert "previous = retainedToken.previous" in paste_controller
    assert "case .ownershipLost:" in coordinator
    assert "return .externalChangePreserved" in coordinator
    assert "retainedRestoreToken = restoreToken" in coordinator


def test_target_resolution_uses_current_focus_without_frame_gating() -> None:
    app_delegate = source("AppDelegate.swift")
    paste_controller = source("PasteController.swift")
    coordinator = source("PasteAttemptCoordinator.swift")

    system_wide_index = paste_controller.index(
        "let systemWide = AXUIElementCreateSystemWide()"
    )
    frontmost_index = paste_controller.index(
        "NSWorkspace.shared.frontmostApplication"
    )
    assert system_wide_index < frontmost_index
    assert "struct PasteTargetContext" in paste_controller
    assert "foundUniqueEligible" in paste_controller
    assert "ambiguous eligible=" in paste_controller
    assert "maxDepth: Int = 10" in paste_controller
    assert "maxNodes: Int = 300" in paste_controller
    assert "optionalFramesMatch" not in paste_controller
    assert "sameLogicalTarget" in paste_controller
    assert "windowFrame" not in paste_controller.split(
        "func sameLogicalTarget", maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    assert "func decide(" not in paste_controller
    assert "stopTarget" not in app_delegate
    assert "recordingAnchor: startTarget" in app_delegate
    assert "for attempt in 0..<3" in coordinator
    assert "activateApplication(for: recordingAnchor)" in coordinator


def test_editability_uses_ax_capabilities_and_preserves_secure_guards() -> None:
    paste_controller = source("PasteController.swift")

    assert "canSetSelectedText" in paste_controller
    assert "canSetSelectedTextRange" in paste_controller
    assert "hasReadableSelectedTextRange" in paste_controller
    assert "kAXSelectedTextAttribute" in paste_controller
    assert "kAXSelectedTextRangeAttribute" in paste_controller
    eligibility = paste_controller.split(
        "func isEligible", maxsplit=1
    )[1].split("func isUnsafeForClipboard", maxsplit=1)[0]
    assert 'snapshot.role == "AXWebArea"' in eligibility
    assert 'snapshot.role == "AXGroup"' not in eligibility
    assert "snapshot.canSetSelectedText" in paste_controller
    assert "AXSecureTextField" in paste_controller
    assert '"AXProtectedContent"' in paste_controller
    assert '"api key"' in paste_controller
    assert '"認証コード"' in paste_controller
    assert "unsafeTermMatches" in paste_controller
    assert "NSRegularExpression.escapedPattern" in paste_controller


def test_dispatch_settles_clipboard_uses_layout_and_posts_once() -> None:
    paste_controller = source("PasteController.swift")
    coordinator = source("PasteAttemptCoordinator.swift")

    settle_index = coordinator.index("await sleep(timing.pasteboardSettleNanoseconds)")
    revalidate_index = coordinator.index(
        "let dispatchResolution = await resolveTarget("
    )
    post_index = coordinator.index("controller.postKeyDown(pasteEvents)")

    assert settle_index < revalidate_index < post_index
    assert "KeyboardLayoutKeyCodeResolver.keyCode(for: \"v\")" in paste_controller
    assert "TISCopyCurrentKeyboardLayoutInputSource" in paste_controller
    assert "UCKeyTranslate" in paste_controller
    assert "translating translate: (UInt16) -> UniChar?" in paste_controller
    resolver = paste_controller.split(
        "enum KeyboardLayoutKeyCodeResolver", maxsplit=1
    )[1]
    assert "kVK_ANSI_V" not in resolver
    assert "CGEventSource(stateID: .combinedSessionState)" in paste_controller
    assert ".cgAnnotatedSessionEventTap" in paste_controller
    assert "keyUpDelayNanoseconds: UInt64 = 20_000_000" in coordinator
    assert "pasteboardSettleNanoseconds: UInt64 = 50_000_000" in coordinator
    assert "postToPid" not in paste_controller
    assert coordinator.count("controller.postKeyDown(pasteEvents)") == 1


def test_verification_precedes_restore_and_submit() -> None:
    paste_controller = source("PasteController.swift")
    coordinator = source("PasteAttemptCoordinator.swift")

    verify_index = coordinator.index("if expectation.isSatisfied(by: after)")
    restore_index = coordinator.index("let clipboard = finishClipboard(")
    submit_index = coordinator.index("let submit = await submitIfRequested(")

    assert verify_index < restore_index < submit_index
    assert "struct PasteVerificationExpectation: Equatable" in paste_controller
    assert "(value as NSString).replacingCharacters" in paste_controller
    assert "(insertedText as NSString).length" in paste_controller
    assert "verificationPollNanoseconds: UInt64 = 50_000_000" in coordinator
    assert "verificationTimeout: TimeInterval = 5" in coordinator
    assert "submitDelayNanoseconds: UInt64 = 100_000_000" in coordinator
    assert "controller.isFocused(target)" in coordinator
    assert "case .verificationTimedOut" in coordinator
    assert "result: .copiedForManualPaste(reason: .verificationTimedOut)" in coordinator


def test_unverified_frontmost_fallback_setting_is_removed_and_migrated() -> None:
    app_delegate = source("AppDelegate.swift")
    settings_store = source("SettingsStore.swift")
    status_controller = source("StatusController.swift")
    settings_window = source("SettingsWindowController.swift")

    assert "var allowUnverifiedPasteFallback" not in settings_store
    assert "legacyAllowUnverifiedPasteFallback" in settings_store
    assert "defaults.removeObject(forKey: Key.legacyAllowUnverifiedPasteFallback)" in settings_store
    assert "setUnverifiedPasteFallback" not in app_delegate
    assert "fallbackPasteApplicationTarget" not in app_delegate
    assert "unverifiedPasteFallbackMenuItem" not in status_controller
    assert "unverifiedPasteFallbackCheckbox" not in settings_window
    assert "Paste + Restore on Success" in settings_store


def test_logs_use_attempt_ids_without_transcript_values() -> None:
    coordinator = source("PasteAttemptCoordinator.swift")
    paste_controller = source("PasteController.swift")

    assert '"paste attempt id=\\(attemptID.uuidString) \\(message)"' in coordinator
    assert "target.snapshot.redactedDescription" in coordinator
    assert "insertedText=" not in coordinator
    assert "before.value" not in coordinator
    assert "after.value" not in coordinator
    assert "redactedAppIdentityHash" in paste_controller
    assert '"pid=\\(pid) bundle=\\(bundleIdentifier)' not in paste_controller


def test_swift_tests_cover_new_paste_invariants() -> None:
    core_tests = (
        REPO_ROOT / "macos/ZenWhisper/ZenWhisperTests/CoreTests.swift"
    ).read_text(encoding="utf-8")
    coordinator_tests = (
        REPO_ROOT
        / "macos/ZenWhisper/ZenWhisperTests/PasteAttemptCoordinatorTests.swift"
    ).read_text(encoding="utf-8")

    assert "func testPasteTargetIdentityIgnoresMovingFrames()" in core_tests
    assert "controller.sameLogicalTarget(target, moved)" in core_tests
    assert "func testPasteVerificationUsesUTF16ReplacementAndCaretMovement()" in core_tests
    assert "func testPasteVerificationRequiresCaretAndInsertedFragment()" in core_tests
    assert "func testPasteKeyCodeResolutionUsesTranslatedLayoutWithoutFixedFallback()" in core_tests
    assert "func testPasteEligibilityRejectsProtectedAndNonEditableTargets()" in core_tests
    assert 'pasteTarget(searchableText: "pinboard")' in core_tests
    assert "testSettingsStoreRemovesLegacyUnverifiedPasteFallback" in core_tests
    assert "func testOverlappingAttemptsAreSerialized() async" in coordinator_tests
    assert "func testVerificationTimeoutSendsPasteOnlyOnceAndKeepsTranscript() async" in coordinator_tests


def test_idle_cache_still_only_records_eligible_targets() -> None:
    app_delegate = source("AppDelegate.swift")

    assert "private var lastKnownPasteTarget: PasteTargetSnapshot?" in app_delegate
    assert "private func refreshPasteTargetCache(stage: String)" in app_delegate
    assert "guard pasteController.isEligible(snapshot) else" in app_delegate
    assert "lastKnownPasteTarget = snapshot" in app_delegate
    assert 'refreshPasteTargetCache(stage: "idle target poll")' in app_delegate
    assert 'capturePasteTarget(stage: "recording start", allowCached: true)' in app_delegate

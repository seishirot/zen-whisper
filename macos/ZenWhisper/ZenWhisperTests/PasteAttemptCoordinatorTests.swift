import Foundation
import XCTest
@testable import ZenWhisper

@MainActor
final class PasteAttemptCoordinatorTests: XCTestCase {
    func testVerifiedPasteUsesDeterministicEventOrderAndKeepsClipboardBeforeSubmit() async {
        let controller = FakePasteAttemptController()
        let clock = PasteVirtualClock()
        let insertedText = "zen 🐕"
        let before = PasteTextState(
            value: "hello world",
            selectedRange: NSRange(location: 6, length: 5)
        )
        controller.textStates = [
            before,
            before,
            PasteTextState(
                value: "hello \(insertedText)",
                selectedRange: NSRange(
                    location: 6 + (insertedText as NSString).length,
                    length: 0
                ),
                textImmediatelyBeforeSelection: insertedText
            )
        ]
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: insertedText,
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: true,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .pastedVerified(clipboard: .kept, submit: .sent)
        )
        XCTAssertEqual(report.verificationLatencyMilliseconds, 50)
        XCTAssertEqual(
            controller.eventLog,
            ["pasteDown", "pasteUp", "returnDown", "returnUp"]
        )
        XCTAssertEqual(controller.preparedTexts, [insertedText])
        XCTAssertEqual(controller.eventTargetPIDs, [4242, 4242, 4242, 4242])
        XCTAssertEqual(
            controller.precedingLengths,
            [0, (insertedText as NSString).length, (insertedText as NSString).length]
        )
        XCTAssertEqual(
            clock.sleeps,
            [50_000_000, 20_000_000, 50_000_000, 100_000_000, 20_000_000]
        )
    }

    func testVerificationTimeoutSendsPasteOnlyOnceAndKeepsTranscript() async {
        let controller = FakePasteAttemptController()
        let clock = PasteVirtualClock()
        let unchanged = PasteTextState(
            value: "before",
            selectedRange: NSRange(location: 6, length: 0)
        )
        controller.textStates = [unchanged]
        controller.fallbackTextState = unchanged
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "never confirmed",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .verificationTimedOut,
                availability: .clipboard
            )
        )
        XCTAssertEqual(controller.eventLog, ["pasteDown", "pasteUp"])
        XCTAssertEqual(
            clock.sleeps.filter { $0 == 50_000_000 }.count,
            101
        )
        XCTAssertEqual(
            clock.sleeps.dropFirst(2).reduce(UInt64(0), +),
            5_000_000_000
        )
    }

    func testMissingTargetRetriesThenCopiesWithoutSendingEvents() async {
        let controller = FakePasteAttemptController()
        controller.defaultProbe = PasteTargetProbe(
            context: nil,
            detail: "transient_ax_failure"
        )
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "manual fallback",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .noEditableTarget,
                availability: .clipboard
            )
        )
        XCTAssertEqual(controller.probeCallCount, 3)
        XCTAssertEqual(clock.sleeps, [50_000_000, 50_000_000])
        XCTAssertEqual(controller.preparedTexts, ["manual fallback"])
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testIndeterminateTargetSafetyRetriesWithoutWritingClipboard() async {
        let controller = FakePasteAttemptController()
        controller.defaultProbe = PasteTargetProbe(
            outcome: .safetyIndeterminate,
            detail: "protected=AXErrorCannotComplete"
        )
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "must not reach clipboard",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .blocked(
                reason: .targetSafetyIndeterminate,
                transcript: .intentionallyDiscarded
            )
        )
        XCTAssertEqual(controller.probeCallCount, 3)
        XCTAssertTrue(controller.preparedTexts.isEmpty)
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testTargetResolutionDeadlineFailsClosedBeforeClipboardWrite() async {
        let controller = FakePasteAttemptController()
        let clock = PasteVirtualClock()
        controller.onProbe = {
            clock.nanoseconds = 251_000_000
        }
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "must not be copied after late AX result",
                outputMode: .pasteKeepClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .blocked(
                reason: .targetSafetyIndeterminate,
                transcript: .intentionallyDiscarded
            )
        )
        XCTAssertTrue(controller.preparedTexts.isEmpty)
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testCopyOnlyBlocksWhenTargetSafetyIsIndeterminate() async {
        let controller = FakePasteAttemptController()
        controller.defaultProbe = PasteTargetProbe(
            outcome: .safetyIndeterminate,
            detail: "metadata=AXErrorCannotComplete"
        )
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "must not be copied",
                outputMode: .copyOnly,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .blocked(
                reason: .targetSafetyIndeterminate,
                transcript: .intentionallyDiscarded
            )
        )
        XCTAssertTrue(controller.preparedTexts.isEmpty)
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testUntrustedCopyOnlyCopiesWithoutProbingAccessibilityTarget() async {
        let controller = FakePasteAttemptController()
        controller.accessibilityTrusted = false
        controller.defaultProbe = PasteTargetProbe(
            outcome: .safetyIndeterminate,
            detail: "apiDisabled"
        )
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(
            controller: controller,
            clock: clock
        )

        let report = await coordinator.perform(
            PasteRequest(
                text: "copy without AX",
                outputMode: .copyOnly,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .copyOnlyMode,
                availability: .clipboard
            )
        )
        XCTAssertEqual(controller.probeCallCount, 0)
        XCTAssertEqual(controller.preparedTexts, ["copy without AX"])
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testMissingEventPermissionCopiesWithoutDispatching() async {
        let controller = FakePasteAttemptController()
        controller.pasteEventsAvailable = false
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "manual event fallback",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .eventPermissionUnavailable,
                availability: .clipboard
            )
        )
        XCTAssertEqual(controller.preparedTexts, ["manual event fallback"])
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testAutoPasteWriteFailureIsRecoverableAndSendsNoEvents() async {
        let controller = FakePasteAttemptController()
        controller.pasteboardWriteFailure = .originalUnavailable
        let coordinator = makeCoordinator(
            controller: controller,
            clock: PasteVirtualClock()
        )

        let report = await coordinator.perform(
            PasteRequest(
                text: "recover after failed write",
                outputMode: .pasteKeepClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .failed(
                reason: .pasteboardWriteFailed(
                    disposition: .originalUnavailable
                )
            )
        )
        XCTAssertTrue(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: report.result
            )
        )
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testCopyOnlyWriteFailurePreservesExactDisposition() async {
        let controller = FakePasteAttemptController()
        controller.pasteboardWriteFailure = .externalChangePreserved
        let coordinator = makeCoordinator(
            controller: controller,
            clock: PasteVirtualClock()
        )

        let report = await coordinator.perform(
            PasteRequest(
                text: "copy failure",
                outputMode: .copyOnly,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .failed(
                reason: .pasteboardWriteFailed(
                    disposition: .externalChangePreserved
                )
            )
        )
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testLegacyRestoreModeKeepsClipboardNonDestructively() async {
        let controller = FakePasteAttemptController()
        controller.textStates = verifiedTextStates(insertedText: "new")
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "new",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .pastedVerified(
                clipboard: .kept,
                submit: .notRequested
            )
        )
    }

    func testUnverifiedPasteWithExternalClipboardChangeUsesRecoveryMenu() async {
        let controller = FakePasteAttemptController()
        controller.pasteboardOwned = false
        controller.pasteboardOwnershipResults = [true, false]
        let unchanged = PasteTextState(
            value: "before",
            selectedRange: NSRange(location: 6, length: 0)
        )
        controller.textStates = [unchanged]
        controller.fallbackTextState = unchanged
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "recover me",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .verificationTimedOut,
                availability: .recoveryMenu
            )
        )
    }

    func testUnsafeDispatchTargetIsBlockedBeforePasteboardPreparation() async {
        let controller = FakePasteAttemptController()
        controller.probes = [
            PasteTargetProbe(
                context: PasteTargetContext(snapshot: testPasteSnapshot()),
                detail: "safe"
            ),
            PasteTargetProbe(
                context: PasteTargetContext(
                    snapshot: testPasteSnapshot(isProtectedContent: true)
                ),
                detail: "unsafe"
            )
        ]
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "must remain recoverable",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .blocked(
                reason: .unsafeTarget,
                transcript: .intentionallyDiscarded
            )
        )
        XCTAssertTrue(controller.preparedTexts.isEmpty)
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testUnsafeDispatchTargetDoesNotCreateRecoveryTranscript() async {
        let controller = FakePasteAttemptController()
        controller.probes = [
            PasteTargetProbe(
                context: PasteTargetContext(snapshot: testPasteSnapshot()),
                detail: "safe"
            ),
            PasteTargetProbe(
                context: PasteTargetContext(
                    snapshot: testPasteSnapshot(isProtectedContent: true)
                ),
                detail: "unsafe"
            )
        ]
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "recover outside the secure target",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .blocked(
                reason: .unsafeTarget,
                transcript: .intentionallyDiscarded
            )
        )
        XCTAssertFalse(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: report.result
            )
        )
        XCTAssertTrue(controller.preparedTexts.isEmpty)
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testCurrentExternalTargetWinsOverRecordingAnchor() async {
        let controller = FakePasteAttemptController()
        controller.textStates = verifiedTextStates(insertedText: "current")
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "current",
                outputMode: .pasteKeepClipboard,
                submitAfterPaste: false,
                recordingAnchor: testPasteSnapshot(
                    pid: 3131,
                    elementIdentifier: "recording-start"
                )
            )
        )

        XCTAssertEqual(
            report.result,
            .pastedVerified(clipboard: .kept, submit: .notRequested)
        )
        XCTAssertEqual(controller.activationCallCount, 0)
        XCTAssertEqual(controller.preparedTexts, ["current"])
    }

    func testRecordingAnchorIsActivatedOnlyWhenOwnUIIsFrontmost() async {
        let controller = FakePasteAttemptController()
        let missing = PasteTargetProbe(context: nil, detail: "own_menu")
        controller.frontmostPID = controller.processPID
        controller.probes = [missing, missing, missing]
        controller.textStates = verifiedTextStates(insertedText: "anchor")
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "anchor",
                outputMode: .pasteKeepClipboard,
                submitAfterPaste: false,
                recordingAnchor: testPasteSnapshot()
            )
        )

        XCTAssertEqual(
            report.result,
            .pastedVerified(clipboard: .kept, submit: .notRequested)
        )
        XCTAssertEqual(controller.activationCallCount, 1)
        XCTAssertEqual(controller.frontmostPID, 4242)
        XCTAssertEqual(
            Array(clock.sleeps.prefix(3)),
            [50_000_000, 50_000_000, 50_000_000]
        )
    }

    func testSubmitIsSuppressedWhenFocusChangesAfterVerifiedPaste() async {
        let controller = FakePasteAttemptController()
        controller.focusResults = [.matched, .changed]
        controller.textStates = verifiedTextStates(insertedText: "new")
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "new",
                outputMode: .pasteKeepClipboard,
                submitAfterPaste: true,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .pastedVerified(
                clipboard: .kept,
                submit: .skippedTargetChanged
            )
        )
        XCTAssertEqual(controller.eventLog, ["pasteDown", "pasteUp"])
        XCTAssertEqual(clock.sleeps.last, 100_000_000)
    }

    func testFrontmostChangeDuringOwnershipCheckStopsBeforePasteDispatch() async {
        let controller = FakePasteAttemptController()
        controller.textStates = verifiedTextStates(insertedText: "new")
        controller.onOwnsPasteboard = {
            controller.frontmostPID = 7777
        }
        let coordinator = makeCoordinator(
            controller: controller,
            clock: PasteVirtualClock()
        )

        let report = await coordinator.perform(
            PasteRequest(
                text: "new",
                outputMode: .pasteKeepClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .noEditableTarget,
                availability: .clipboard
            )
        )
        XCTAssertTrue(controller.eventTargetPIDs.isEmpty)
    }

    func testSubmitTargetChangeDuringEventCreationSuppressesReturn() async {
        let controller = FakePasteAttemptController()
        controller.textStates = verifiedTextStates(insertedText: "new")
        controller.onMakeReturnEvents = {
            controller.frontmostPID = 7777
        }
        let coordinator = makeCoordinator(
            controller: controller,
            clock: PasteVirtualClock()
        )

        let report = await coordinator.perform(
            PasteRequest(
                text: "new",
                outputMode: .pasteKeepClipboard,
                submitAfterPaste: true,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .pastedVerified(
                clipboard: .kept,
                submit: .skippedTargetChanged
            )
        )
        XCTAssertEqual(controller.eventTargetPIDs, [4242, 4242])
    }

    func testImmediateFocusSafetyRecheckStopsBeforePasteKeyDown() async {
        let controller = FakePasteAttemptController()
        controller.focusResults = [.changed]
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "do not dispatch",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .noEditableTarget,
                availability: .clipboard
            )
        )
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testImmediateUnsafeFocusKeepsTranscriptWithoutDestructiveRestore() async {
        let controller = FakePasteAttemptController()
        controller.focusResults = [.unsafe]
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "do not leave on clipboard",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .blocked(
                reason: .unsafeTarget,
                transcript: .recoveryMenu
            )
        )
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testImmediateIndeterminateFocusKeepsTranscriptWithoutDestructiveRestore() async {
        let controller = FakePasteAttemptController()
        controller.focusResults = [.safetyIndeterminate]
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "do not leave after AX failure",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .blocked(
                reason: .targetSafetyIndeterminate,
                transcript: .recoveryMenu
            )
        )
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testClipboardOwnershipLossBeforeKeyDownSkipsPasteEvent() async {
        let controller = FakePasteAttemptController()
        controller.pasteboardOwned = false
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "must not paste a newer clipboard",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .verificationUnavailable,
                availability: .recoveryMenu
            )
        )
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testClipboardOwnershipLossDuringEventCreationSkipsPasteEvent() async {
        let controller = FakePasteAttemptController()
        controller.onMakePasteEvents = {
            controller.pasteboardOwned = false
        }
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "do not paste after event creation race",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .verificationUnavailable,
                availability: .recoveryMenu
            )
        )
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testUnsafeTargetBlocksPasteboardAndEventWrites() async {
        let controller = FakePasteAttemptController()
        controller.unsafe = true
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let report = await coordinator.perform(
            PasteRequest(
                text: "must not be copied",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            report.result,
            .blocked(
                reason: .unsafeTarget,
                transcript: .intentionallyDiscarded
            )
        )
        XCTAssertTrue(controller.preparedTexts.isEmpty)
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testOverlappingAttemptsAreSerialized() async {
        let controller = FakePasteAttemptController()
        controller.textStates = [
            PasteTextState(
                value: "",
                selectedRange: NSRange(location: 0, length: 0)
            ),
            PasteTextState(
                value: "first",
                selectedRange: NSRange(location: 5, length: 0),
                textImmediatelyBeforeSelection: "first"
            ),
            PasteTextState(
                value: "first",
                selectedRange: NSRange(location: 5, length: 0)
            ),
            PasteTextState(
                value: "firstsecond",
                selectedRange: NSRange(location: 11, length: 0),
                textImmediatelyBeforeSelection: "second"
            )
        ]
        let clock = PasteGatedVirtualClock()
        let coordinator = PasteAttemptCoordinator(
            controller: controller,
            logger: { _ in },
            sleep: { nanoseconds in
                await clock.sleep(nanoseconds)
            },
            now: { clock.now }
        )
        let first = Task { @MainActor in
            await coordinator.perform(
                PasteRequest(
                    text: "first",
                    outputMode: .pasteKeepClipboard,
                    submitAfterPaste: false,
                    recordingAnchor: nil
                )
            )
        }
        for _ in 0..<100 where !clock.isBlocked {
            await Task.yield()
        }
        XCTAssertTrue(clock.isBlocked)
        XCTAssertEqual(controller.preparedTexts, ["first"])

        let second = Task { @MainActor in
            await coordinator.perform(
                PasteRequest(
                    text: "second",
                    outputMode: .pasteKeepClipboard,
                    submitAfterPaste: false,
                    recordingAnchor: nil
                )
            )
        }
        for _ in 0..<10 {
            await Task.yield()
        }
        XCTAssertEqual(controller.preparedTexts, ["first"])

        clock.release()
        let firstReport = await first.value
        let secondReport = await second.value

        XCTAssertEqual(
            firstReport.result,
            .pastedVerified(clipboard: .kept, submit: .notRequested)
        )
        XCTAssertEqual(
            secondReport.result,
            .pastedVerified(clipboard: .kept, submit: .notRequested)
        )
        XCTAssertEqual(controller.preparedTexts, ["first", "second"])
    }

    func testCancellationDuringTargetRetryStopsBeforePasteboardWrite() async {
        let controller = FakePasteAttemptController()
        controller.probes = [
            PasteTargetProbe(context: nil, detail: "transient")
        ]
        let clock = PasteGatedVirtualClock()
        let coordinator = PasteAttemptCoordinator(
            controller: controller,
            logger: { _ in },
            sleep: { nanoseconds in
                await clock.sleep(nanoseconds)
            },
            now: { clock.now }
        )
        let attempt = Task { @MainActor in
            await coordinator.perform(
                PasteRequest(
                    text: "cancelled",
                    outputMode: .pasteRestoreClipboard,
                    submitAfterPaste: false,
                    recordingAnchor: nil
                )
            )
        }
        for _ in 0..<100 where !clock.isBlocked {
            await Task.yield()
        }
        XCTAssertTrue(clock.isBlocked)

        coordinator.cancel()
        clock.release()
        let report = await attempt.value

        XCTAssertEqual(report.result, .failed(reason: .superseded))
        XCTAssertTrue(controller.preparedTexts.isEmpty)
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testCopyOnlyCancellationDuringTargetRetryStopsBeforePasteboardWrite() async {
        let controller = FakePasteAttemptController()
        controller.probes = [
            PasteTargetProbe(context: nil, detail: "transient")
        ]
        let clock = PasteGatedVirtualClock()
        let coordinator = PasteAttemptCoordinator(
            controller: controller,
            logger: { _ in },
            sleep: { nanoseconds in
                await clock.sleep(nanoseconds)
            },
            now: { clock.now }
        )
        let attempt = Task { @MainActor in
            await coordinator.perform(
                PasteRequest(
                    text: "cancelled copy",
                    outputMode: .copyOnly,
                    submitAfterPaste: false,
                    recordingAnchor: nil
                )
            )
        }
        for _ in 0..<100 where !clock.isBlocked {
            await Task.yield()
        }
        XCTAssertTrue(clock.isBlocked)

        coordinator.cancel()
        clock.release()
        let report = await attempt.value

        XCTAssertEqual(report.result, .failed(reason: .superseded))
        XCTAssertTrue(controller.preparedTexts.isEmpty)
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testCancellationDuringDispatchRetryNeverRestoresClipboard() async {
        let controller = FakePasteAttemptController()
        controller.probes = [
            PasteTargetProbe(
                context: PasteTargetContext(snapshot: testPasteSnapshot()),
                detail: "initial"
            ),
            PasteTargetProbe(context: nil, detail: "dispatch transient")
        ]
        let clock = PasteNthGatedVirtualClock(blockAtCall: 2)
        let coordinator = PasteAttemptCoordinator(
            controller: controller,
            logger: { _ in },
            sleep: { nanoseconds in
                await clock.sleep(nanoseconds)
            },
            now: { clock.now }
        )
        let attempt = Task { @MainActor in
            await coordinator.perform(
                PasteRequest(
                    text: "restore on cancel",
                    outputMode: .pasteRestoreClipboard,
                    submitAfterPaste: false,
                    recordingAnchor: nil
                )
            )
        }
        for _ in 0..<100 where !clock.isBlocked {
            await Task.yield()
        }
        XCTAssertTrue(clock.isBlocked)
        XCTAssertEqual(controller.preparedTexts, ["restore on cancel"])

        coordinator.cancel()
        clock.release()
        let report = await attempt.value

        XCTAssertEqual(report.result, .failed(reason: .superseded))
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testCancellationDuringDispatchRetryIgnoresLegacyRestoreOutcome() async {
        let controller = FakePasteAttemptController()
        controller.probes = [
            PasteTargetProbe(
                context: PasteTargetContext(snapshot: testPasteSnapshot()),
                detail: "initial"
            ),
            PasteTargetProbe(context: nil, detail: "dispatch transient")
        ]
        let clock = PasteNthGatedVirtualClock(blockAtCall: 2)
        let coordinator = PasteAttemptCoordinator(
            controller: controller,
            logger: { _ in },
            sleep: { nanoseconds in
                await clock.sleep(nanoseconds)
            },
            now: { clock.now }
        )
        let attempt = Task { @MainActor in
            await coordinator.perform(
                PasteRequest(
                    text: "restore failure on cancel",
                    outputMode: .pasteRestoreClipboard,
                    submitAfterPaste: false,
                    recordingAnchor: nil
                )
            )
        }
        for _ in 0..<100 where !clock.isBlocked {
            await Task.yield()
        }
        XCTAssertTrue(clock.isBlocked)
        XCTAssertEqual(
            controller.preparedTexts,
            ["restore failure on cancel"]
        )

        coordinator.cancel()
        clock.release()
        let report = await attempt.value

        XCTAssertEqual(
            report.result,
            .failed(reason: .superseded)
        )
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testCancellationDuringVerificationKeepsTranscriptRecoverable() async {
        let controller = FakePasteAttemptController()
        let unchanged = PasteTextState(
            value: "before",
            selectedRange: NSRange(location: 6, length: 0)
        )
        controller.textStates = [unchanged]
        controller.fallbackTextState = unchanged
        let clock = PasteNthGatedVirtualClock(blockAtCall: 3)
        let coordinator = PasteAttemptCoordinator(
            controller: controller,
            logger: { _ in },
            sleep: { nanoseconds in
                await clock.sleep(nanoseconds)
            },
            now: { clock.now }
        )
        let attempt = Task { @MainActor in
            await coordinator.perform(
                PasteRequest(
                    text: "recover after dispatch",
                    outputMode: .pasteRestoreClipboard,
                    submitAfterPaste: false,
                    recordingAnchor: nil
                )
            )
        }
        for _ in 0..<100 where !clock.isBlocked {
            await Task.yield()
        }
        XCTAssertTrue(clock.isBlocked)
        XCTAssertEqual(controller.eventLog, ["pasteDown", "pasteUp"])

        coordinator.cancel()
        clock.release()
        let report = await attempt.value

        XCTAssertEqual(
            report.result,
            .manualPasteFallback(
                reason: .superseded,
                availability: .clipboard
            )
        )
        XCTAssertEqual(controller.eventLog, ["pasteDown", "pasteUp"])
    }

    func testUnconfirmedAttemptDoesNotRetainOriginalClipboardSnapshot() async {
        let controller = FakePasteAttemptController()
        controller.textStates = [
            PasteTextState(value: nil, selectedRange: nil),
            PasteTextState(
                value: "",
                selectedRange: NSRange(location: 0, length: 0)
            ),
            PasteTextState(
                value: "verified",
                selectedRange: NSRange(location: 8, length: 0),
                textImmediatelyBeforeSelection: "verified"
            )
        ]
        let clock = PasteVirtualClock()
        let coordinator = makeCoordinator(controller: controller, clock: clock)

        let first = await coordinator.perform(
            PasteRequest(
                text: "unconfirmed",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )
        let second = await coordinator.perform(
            PasteRequest(
                text: "verified",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: nil
            )
        )

        XCTAssertEqual(
            first.result,
            .manualPasteFallback(
                reason: .verificationUnavailable,
                availability: .clipboard
            )
        )
        XCTAssertEqual(
            second.result,
            .pastedVerified(clipboard: .kept, submit: .notRequested)
        )
    }

    private func makeCoordinator(
        controller: FakePasteAttemptController,
        clock: PasteVirtualClock
    ) -> PasteAttemptCoordinator {
        PasteAttemptCoordinator(
            controller: controller,
            logger: { _ in },
            sleep: { nanoseconds in
                await clock.sleep(nanoseconds)
            },
            now: { clock.now }
        )
    }

    private func verifiedTextStates(insertedText: String) -> [PasteTextState] {
        [
            PasteTextState(
                value: "",
                selectedRange: NSRange(location: 0, length: 0)
            ),
            PasteTextState(
                value: insertedText,
                selectedRange: NSRange(
                    location: (insertedText as NSString).length,
                    length: 0
                ),
                textImmediatelyBeforeSelection: insertedText
            )
        ]
    }
}

private final class FakePasteAttemptController: PasteAttemptControlling {
    private let targetSnapshot = testPasteSnapshot()

    var accessibilityTrusted = true
    var pasteEventsAvailable = true
    var unsafe = false
    var eligible = true
    var frontmostPID: pid_t? = 4242
    var processPID: pid_t = 9000
    var probes: [PasteTargetProbe] = []
    var defaultProbe: PasteTargetProbe?
    var probeCallCount = 0
    var preparedTexts: [String] = []
    var pasteboardOwned = true
    var pasteboardWriteFailure: PasteboardWriteFailureDisposition?
    var pasteboardOwnershipResults: [Bool] = []
    var activationCallCount = 0
    var textStates: [PasteTextState] = []
    var fallbackTextState = PasteTextState(value: nil, selectedRange: nil)
    var precedingLengths: [Int] = []
    var focusResults: [PasteFocusValidation] = []
    var eventLog: [String] = []
    var eventTargetPIDs: [pid_t] = []
    var onMakePasteEvents: (() -> Void)?
    var onMakeReturnEvents: (() -> Void)?
    var onOwnsPasteboard: (() -> Void)?
    var onProbe: (() -> Void)?

    private var targetContext: PasteTargetContext {
        PasteTargetContext(snapshot: targetSnapshot)
    }

    func isAccessibilityTrusted() -> Bool {
        accessibilityTrusted
    }

    func snapshotFocusedTargetProbe() -> PasteTargetProbe {
        probeCallCount += 1
        onProbe?()
        if !probes.isEmpty {
            return probes.removeFirst()
        }
        return defaultProbe
            ?? PasteTargetProbe(context: targetContext, detail: "fake_target")
    }

    func isUnsafeForClipboard(_ snapshot: PasteTargetSnapshot) -> Bool {
        unsafe || snapshot.isProtectedContent
    }

    func isEligible(_ snapshot: PasteTargetSnapshot) -> Bool {
        eligible && !isUnsafeForClipboard(snapshot)
    }

    func activateApplication(for anchor: PasteTargetSnapshot) -> Bool {
        activationCallCount += 1
        frontmostPID = anchor.pid
        return true
    }

    func currentProcessIdentifier() -> pid_t {
        processPID
    }

    func frontmostProcessIdentifier() -> pid_t? {
        frontmostPID
    }

    func canCreatePasteEvents() -> Bool {
        pasteEventsAvailable
    }

    func prepareAutoPaste(
        _ text: String,
        attemptID: UUID
    ) -> PasteboardWriteResult {
        preparedTexts.append(text)
        if let pasteboardWriteFailure {
            return .writeFailed(disposition: pasteboardWriteFailure)
        }
        return .success(PasteboardOwnershipToken(testIdentifier: attemptID))
    }

    func ownsPasteboard(_ token: PasteboardOwnershipToken) -> Bool {
        onOwnsPasteboard?()
        if !pasteboardOwnershipResults.isEmpty {
            return pasteboardOwnershipResults.removeFirst()
        }
        return pasteboardOwned
    }

    func textState(
        for context: PasteTargetContext,
        precedingUTF16Length: Int
    ) -> PasteTextState {
        precedingLengths.append(precedingUTF16Length)
        guard !textStates.isEmpty else {
            return fallbackTextState
        }
        return textStates.removeFirst()
    }

    func validateFocus(
        _ approved: PasteTargetContext
    ) -> PasteFocusValidation {
        guard !focusResults.isEmpty else {
            return .matched
        }
        return focusResults.removeFirst()
    }

    func makePasteKeyEventPair() -> PasteKeyEventPair? {
        onMakePasteEvents?()
        return pasteEventsAvailable
            ? PasteKeyEventPair(testVirtualKey: 41)
            : nil
    }

    func makeReturnKeyEventPair() -> PasteKeyEventPair? {
        onMakeReturnEvents?()
        return pasteEventsAvailable
            ? PasteKeyEventPair(testVirtualKey: 36)
            : nil
    }

    func postKeyDown(_ pair: PasteKeyEventPair, to pid: pid_t) {
        eventTargetPIDs.append(pid)
        eventLog.append(pair.virtualKey == 36 ? "returnDown" : "pasteDown")
    }

    func postKeyUp(_ pair: PasteKeyEventPair, to pid: pid_t) {
        eventTargetPIDs.append(pid)
        eventLog.append(pair.virtualKey == 36 ? "returnUp" : "pasteUp")
    }
}

private func testPasteSnapshot(
    pid: pid_t = 4242,
    elementIdentifier: String = "editor",
    isProtectedContent: Bool = false
) -> PasteTargetSnapshot {
    PasteTargetSnapshot(
        pid: pid,
        bundleIdentifier: "test.target",
        role: "AXTextArea",
        subrole: "",
        windowTitle: "",
        windowFrame: .null,
        elementIdentifier: elementIdentifier,
        elementFrame: .null,
        hasEditableValue: true,
        canSetSelectedText: true,
        canSetSelectedTextRange: true,
        hasReadableValue: true,
        hasReadableSelectedTextRange: true,
        isProtectedContent: isProtectedContent,
        searchableText: "editor",
        discovery: "fake"
    )
}

private class PasteVirtualClock {
    var nanoseconds: UInt64 = 0
    var sleeps: [UInt64] = []

    var now: Date {
        Date(
            timeIntervalSinceReferenceDate:
                TimeInterval(nanoseconds) / 1_000_000_000
        )
    }

    func sleep(_ duration: UInt64) async {
        sleeps.append(duration)
        nanoseconds += duration
    }
}

private final class PasteGatedVirtualClock: PasteVirtualClock {
    private var shouldBlock = true
    private var continuation: CheckedContinuation<Void, Never>?

    var isBlocked: Bool {
        continuation != nil
    }

    override func sleep(_ duration: UInt64) async {
        sleeps.append(duration)
        nanoseconds += duration
        guard shouldBlock else {
            return
        }
        shouldBlock = false
        await withCheckedContinuation { continuation in
            self.continuation = continuation
        }
    }

    func release() {
        continuation?.resume()
        continuation = nil
    }
}

private final class PasteNthGatedVirtualClock: PasteVirtualClock {
    private let blockAtCall: Int
    private var callCount = 0
    private var continuation: CheckedContinuation<Void, Never>?

    init(blockAtCall: Int) {
        self.blockAtCall = blockAtCall
    }

    var isBlocked: Bool {
        continuation != nil
    }

    override func sleep(_ duration: UInt64) async {
        sleeps.append(duration)
        nanoseconds += duration
        callCount += 1
        guard callCount == blockAtCall else {
            return
        }
        await withCheckedContinuation { continuation in
            self.continuation = continuation
        }
    }

    func release() {
        continuation?.resume()
        continuation = nil
    }
}

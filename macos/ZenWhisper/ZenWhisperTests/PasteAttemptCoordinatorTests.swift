import Foundation
import XCTest
@testable import ZenWhisper

@MainActor
final class PasteAttemptCoordinatorTests: XCTestCase {
    func testVerifiedPasteUsesDeterministicEventOrderAndRestoresBeforeSubmit() async {
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
            .pastedVerified(clipboard: .restored, submit: .sent)
        )
        XCTAssertEqual(report.verificationLatencyMilliseconds, 50)
        XCTAssertEqual(
            controller.eventLog,
            ["pasteDown", "pasteUp", "returnDown", "returnUp"]
        )
        XCTAssertEqual(controller.preparedTexts, [insertedText])
        XCTAssertEqual(controller.restoreCallCount, 1)
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
            .copiedForManualPaste(reason: .verificationTimedOut)
        )
        XCTAssertEqual(controller.eventLog, ["pasteDown", "pasteUp"])
        XCTAssertEqual(controller.restoreCallCount, 0)
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
            .copiedForManualPaste(reason: .noEditableTarget)
        )
        XCTAssertEqual(controller.probeCallCount, 3)
        XCTAssertEqual(clock.sleeps, [50_000_000, 50_000_000])
        XCTAssertEqual(controller.preparedTexts, ["manual fallback"])
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
            .copiedForManualPaste(reason: .eventPermissionUnavailable)
        )
        XCTAssertEqual(controller.preparedTexts, ["manual event fallback"])
        XCTAssertTrue(controller.eventLog.isEmpty)
    }

    func testExternalClipboardChangeIsPreservedAfterVerifiedPaste() async {
        let controller = FakePasteAttemptController()
        controller.restoreResult = .ownershipLost
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
                clipboard: .externalChangePreserved,
                submit: .notRequested
            )
        )
        XCTAssertEqual(controller.restoreCallCount, 1)
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
        controller.focusResults = [true, false]
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

        XCTAssertEqual(report.result, .blocked(reason: .unsafeTarget))
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

    func testUnconfirmedAttemptCarriesOriginalClipboardIntoNextAttempt() async {
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
            .copiedForManualPaste(reason: .verificationUnavailable)
        )
        XCTAssertEqual(
            second.result,
            .pastedVerified(clipboard: .restored, submit: .notRequested)
        )
        XCTAssertEqual(controller.receivedRetainedToken, [false, true])
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
    var receivedRetainedToken: [Bool] = []
    var restoreResult = PasteboardRestoreResult.restored
    var restoreCallCount = 0
    var activationCallCount = 0
    var textStates: [PasteTextState] = []
    var fallbackTextState = PasteTextState(value: nil, selectedRange: nil)
    var precedingLengths: [Int] = []
    var focusResults: [Bool] = []
    var eventLog: [String] = []

    private var targetContext: PasteTargetContext {
        PasteTargetContext(snapshot: targetSnapshot)
    }

    func isAccessibilityTrusted() -> Bool {
        accessibilityTrusted
    }

    func snapshotFocusedTargetProbe() -> PasteTargetProbe {
        probeCallCount += 1
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
        attemptID: UUID,
        preservingBaseFrom retainedToken: PasteboardRestoreToken?
    ) -> PasteboardWriteResult {
        preparedTexts.append(text)
        receivedRetainedToken.append(retainedToken != nil)
        return .success(PasteboardRestoreToken(testIdentifier: attemptID))
    }

    func restoreIfOwned(
        _ token: PasteboardRestoreToken
    ) -> PasteboardRestoreResult {
        restoreCallCount += 1
        return restoreResult
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

    func isFocused(_ approved: PasteTargetContext) -> Bool {
        guard !focusResults.isEmpty else {
            return true
        }
        return focusResults.removeFirst()
    }

    func makePasteKeyEventPair() -> PasteKeyEventPair? {
        pasteEventsAvailable
            ? PasteKeyEventPair(testVirtualKey: 41)
            : nil
    }

    func makeReturnKeyEventPair() -> PasteKeyEventPair? {
        pasteEventsAvailable
            ? PasteKeyEventPair(testVirtualKey: 36)
            : nil
    }

    func postKeyDown(_ pair: PasteKeyEventPair) {
        eventLog.append(pair.virtualKey == 36 ? "returnDown" : "pasteDown")
    }

    func postKeyUp(_ pair: PasteKeyEventPair) {
        eventLog.append(pair.virtualKey == 36 ? "returnUp" : "pasteUp")
    }
}

private func testPasteSnapshot(
    pid: pid_t = 4242,
    elementIdentifier: String = "editor"
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
        isProtectedContent: false,
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

import AppKit
import Foundation

struct PasteRequest {
    let text: String
    let outputMode: OutputMode
    let submitAfterPaste: Bool
    let recordingAnchor: PasteTargetSnapshot?
}

enum PasteFailureReason: Equatable {
    case copyOnlyMode
    case accessibilityUnavailable
    case eventPermissionUnavailable
    case noEditableTarget
    case verificationUnavailable
    case verificationTimedOut
    case pasteboardWriteFailed(disposition: PasteboardWriteFailureDisposition)
    case superseded

    var logCode: String {
        switch self {
        case .copyOnlyMode:
            return "copy_only_mode"
        case .accessibilityUnavailable:
            return "accessibility_unavailable"
        case .eventPermissionUnavailable:
            return "event_permission_unavailable"
        case .noEditableTarget:
            return "no_editable_target"
        case .verificationUnavailable:
            return "verification_unavailable"
        case .verificationTimedOut:
            return "verification_timed_out"
        case .pasteboardWriteFailed(let disposition):
            return "pasteboard_write_failed_\(disposition.logCode)"
        case .superseded:
            return "superseded"
        }
    }
}

enum PasteBlockReason: Equatable {
    case unsafeTarget
    case targetSafetyIndeterminate

    var logCode: String {
        switch self {
        case .unsafeTarget:
            return "unsafe_target"
        case .targetSafetyIndeterminate:
            return "target_safety_indeterminate"
        }
    }
}

enum PasteBlockedTranscriptDisposition: Equatable {
    case intentionallyDiscarded
    case recoveryMenu

    var logCode: String {
        switch self {
        case .intentionallyDiscarded:
            return "intentionally_discarded"
        case .recoveryMenu:
            return "recovery_menu"
        }
    }
}

enum PasteClipboardDisposition: Equatable {
    case kept

    var logCode: String {
        switch self {
        case .kept:
            return "kept"
        }
    }
}

enum PasteManualPasteAvailability: Equatable {
    case clipboard
    case recoveryMenu

    var logCode: String {
        switch self {
        case .clipboard:
            return "clipboard"
        case .recoveryMenu:
            return "recovery_menu"
        }
    }
}

enum PasteSubmitResult: Equatable {
    case notRequested
    case sent
    case skippedTargetChanged
    case eventUnavailable

    var logCode: String {
        switch self {
        case .notRequested:
            return "not_requested"
        case .sent:
            return "sent"
        case .skippedTargetChanged:
            return "skipped_target_changed"
        case .eventUnavailable:
            return "event_unavailable"
        }
    }
}

enum PasteAttemptResult: Equatable {
    case pastedVerified(
        clipboard: PasteClipboardDisposition,
        submit: PasteSubmitResult
    )
    case manualPasteFallback(
        reason: PasteFailureReason,
        availability: PasteManualPasteAvailability
    )
    case blocked(
        reason: PasteBlockReason,
        transcript: PasteBlockedTranscriptDisposition
    )
    case failed(reason: PasteFailureReason)

    var logDescription: String {
        switch self {
        case .pastedVerified(let clipboard, let submit):
            return "outcome=pasted_verified clipboard=\(clipboard.logCode) submit=\(submit.logCode)"
        case .manualPasteFallback(let reason, let availability):
            return "outcome=manual_paste_fallback reason=\(reason.logCode) availability=\(availability.logCode)"
        case .blocked(let reason, let transcript):
            return "outcome=blocked reason=\(reason.logCode) transcript=\(transcript.logCode)"
        case .failed(let reason):
            return "outcome=failed reason=\(reason.logCode)"
        }
    }
}

struct PasteAttemptReport: Equatable {
    let attemptID: UUID
    let result: PasteAttemptResult
    let verificationLatencyMilliseconds: Int?
}

struct PasteAttemptTiming: Equatable {
    var targetRetryNanoseconds: UInt64 = 50_000_000
    var targetResolutionTimeout: TimeInterval = 0.25
    var pasteboardSettleNanoseconds: UInt64 = 50_000_000
    var keyUpDelayNanoseconds: UInt64 = 20_000_000
    var verificationPollNanoseconds: UInt64 = 50_000_000
    var verificationTimeout: TimeInterval = 5
    var submitDelayNanoseconds: UInt64 = 100_000_000
}

@MainActor
final class PasteAttemptCoordinator {
    typealias Logger = (String) -> Void
    typealias Sleeper = (UInt64) async -> Void
    typealias Clock = () -> Date

    private enum TargetResolution {
        case target(PasteTargetContext)
        case unsafe(PasteTargetContext)
        case missing(detail: String)
        case safetyIndeterminate(detail: String)
    }

    private let controller: any PasteAttemptControlling
    private let timing: PasteAttemptTiming
    private let logger: Logger
    private let sleep: Sleeper
    private let now: Clock
    private var generation = 0
    private var transactionTail: Task<Void, Never>?
    private var latestQueueID: UUID?

    init(
        controller: any PasteAttemptControlling,
        timing: PasteAttemptTiming = PasteAttemptTiming(),
        logger: @escaping Logger = { _ in },
        sleep: @escaping Sleeper = { nanoseconds in
            try? await Task.sleep(nanoseconds: nanoseconds)
        },
        now: @escaping Clock = { Date() }
    ) {
        self.controller = controller
        self.timing = timing
        self.logger = logger
        self.sleep = sleep
        self.now = now
    }

    func cancel() {
        generation += 1
    }

    func perform(_ request: PasteRequest) async -> PasteAttemptReport {
        let attemptID = UUID()
        let attemptGeneration = generation
        let predecessor = transactionTail
        let queueID = UUID()
        log(attemptID, "phase=queued")
        let transaction = Task { @MainActor in
            await predecessor?.value
            guard attemptGeneration == self.generation else {
                return self.report(
                    attemptID,
                    result: .failed(reason: .superseded)
                )
            }
            return await self.performTransaction(
                request,
                attemptID: attemptID,
                attemptGeneration: attemptGeneration
            )
        }
        let completion = Task { @MainActor [weak self] in
            _ = await transaction.value
            guard let self, self.latestQueueID == queueID else {
                return
            }
            self.transactionTail = nil
            self.latestQueueID = nil
        }
        latestQueueID = queueID
        transactionTail = completion
        return await transaction.value
    }

    private func performTransaction(
        _ request: PasteRequest,
        attemptID: UUID,
        attemptGeneration: Int
    ) async -> PasteAttemptReport {
        log(attemptID, "phase=start mode=\(request.outputMode.rawValue) submit=\(request.submitAfterPaste)")

        guard controller.isAccessibilityTrusted() else {
            return copyForManualPaste(
                request.text,
                attemptID: attemptID,
                reason: request.outputMode == .copyOnly
                    ? .copyOnlyMode
                    : .accessibilityUnavailable
            )
        }

        if request.outputMode == .copyOnly {
            let resolution = await resolveTarget(recordingAnchor: request.recordingAnchor)
            guard attemptGeneration == generation else {
                return report(attemptID, result: .failed(reason: .superseded))
            }
            switch resolution {
            case .unsafe:
                return report(
                    attemptID,
                    result: .blocked(
                        reason: .unsafeTarget,
                        transcript: .intentionallyDiscarded
                    )
                )
            case .safetyIndeterminate(let detail):
                log(
                    attemptID,
                    "phase=target result=safety_indeterminate detail=\(detail)"
                )
                return report(
                    attemptID,
                    result: .blocked(
                        reason: .targetSafetyIndeterminate,
                        transcript: .intentionallyDiscarded
                    )
                )
            case .target, .missing:
                break
            }
            return copyForManualPaste(
                request.text,
                attemptID: attemptID,
                reason: .copyOnlyMode
            )
        }

        let initialResolution = await resolveTarget(
            recordingAnchor: request.recordingAnchor
        )
        guard attemptGeneration == generation else {
            return report(attemptID, result: .failed(reason: .superseded))
        }
        switch initialResolution {
        case .unsafe:
            return report(
                attemptID,
                result: .blocked(
                    reason: .unsafeTarget,
                    transcript: .intentionallyDiscarded
                )
            )
        case .safetyIndeterminate(let detail):
            log(
                attemptID,
                "phase=target result=safety_indeterminate detail=\(detail)"
            )
            return report(
                attemptID,
                result: .blocked(
                    reason: .targetSafetyIndeterminate,
                    transcript: .intentionallyDiscarded
                )
            )
        case .missing(let detail):
            log(attemptID, "phase=target result=missing detail=\(detail)")
            return copyForManualPaste(
                request.text,
                attemptID: attemptID,
                reason: .noEditableTarget
            )
        case .target(let target):
            log(
                attemptID,
                "phase=target result=resolved target=\(target.snapshot.redactedDescription)"
            )
        }

        guard controller.canCreatePasteEvents() else {
            return copyForManualPaste(
                request.text,
                attemptID: attemptID,
                reason: .eventPermissionUnavailable
            )
        }

        let dispatchResolution = await resolveTarget(
            recordingAnchor: request.recordingAnchor
        )
        guard attemptGeneration == generation else {
            return report(attemptID, result: .failed(reason: .superseded))
        }
        let dispatchTarget: PasteTargetContext
        switch dispatchResolution {
        case .unsafe:
            return report(
                attemptID,
                result: .blocked(
                    reason: .unsafeTarget,
                    transcript: .intentionallyDiscarded
                )
            )
        case .safetyIndeterminate(let detail):
            log(
                attemptID,
                "phase=dispatch result=safety_indeterminate detail=\(detail)"
            )
            return report(
                attemptID,
                result: .blocked(
                    reason: .targetSafetyIndeterminate,
                    transcript: .intentionallyDiscarded
                )
            )
        case .missing(let detail):
            log(attemptID, "phase=dispatch result=target_missing detail=\(detail)")
            return copyForManualPaste(
                request.text,
                attemptID: attemptID,
                reason: .noEditableTarget
            )
        case .target(let target):
            dispatchTarget = target
        }

        guard controller.frontmostProcessIdentifier()
            == dispatchTarget.snapshot.pid else {
            log(attemptID, "phase=dispatch result=frontmost_changed")
            return copyForManualPaste(
                request.text,
                attemptID: attemptID,
                reason: .noEditableTarget
            )
        }

        let pasteboardWrite = controller.prepareAutoPaste(
            request.text,
            attemptID: attemptID
        )
        let ownershipToken: PasteboardOwnershipToken
        switch pasteboardWrite {
        case .success(let token):
            ownershipToken = token
        case .writeFailed(let disposition):
            return report(
                attemptID,
                result: .failed(
                    reason: .pasteboardWriteFailed(
                        disposition: disposition
                    )
                )
            )
        }
        log(attemptID, "phase=pasteboard result=prepared")

        await sleep(timing.pasteboardSettleNanoseconds)
        guard attemptGeneration == generation else {
            return supersededBeforePasteDispatch(
                attemptID: attemptID
            )
        }

        guard let pasteEvents = controller.makePasteKeyEventPair() else {
            return manualPasteReport(
                attemptID: attemptID,
                reason: .eventPermissionUnavailable,
                token: ownershipToken
            )
        }

        let before = controller.textState(
            for: dispatchTarget,
            precedingUTF16Length: 0
        )
        let expectation = PasteVerificationExpectation(
            before: before,
            insertedText: request.text
        )
        guard attemptGeneration == generation else {
            return supersededBeforePasteDispatch(
                attemptID: attemptID
            )
        }
        guard controller.ownsPasteboard(ownershipToken) else {
            log(
                attemptID,
                "phase=dispatch result=clipboard_ownership_lost_before_keydown"
            )
            return manualPasteReport(
                attemptID: attemptID,
                reason: .verificationUnavailable,
                token: ownershipToken
            )
        }
        switch controller.validateFocus(dispatchTarget) {
        case .matched:
            break
        case .changed:
            log(
                attemptID,
                "phase=dispatch result=focus_changed_before_keydown"
            )
            return manualPasteReport(
                attemptID: attemptID,
                reason: .noEditableTarget,
                token: ownershipToken
            )
        case .unsafe:
            return blockedAfterPasteboardPreparation(
                attemptID: attemptID,
                reason: .unsafeTarget
            )
        case .safetyIndeterminate:
            return blockedAfterPasteboardPreparation(
                attemptID: attemptID,
                reason: .targetSafetyIndeterminate
            )
        }
        guard controller.frontmostProcessIdentifier()
                == dispatchTarget.snapshot.pid else {
            log(
                attemptID,
                "phase=dispatch result=frontmost_changed_before_keydown"
            )
            return manualPasteReport(
                attemptID: attemptID,
                reason: .noEditableTarget,
                token: ownershipToken
            )
        }

        controller.postKeyDown(
            pasteEvents,
            to: dispatchTarget.snapshot.pid
        )
        await sleep(timing.keyUpDelayNanoseconds)
        controller.postKeyUp(
            pasteEvents,
            to: dispatchTarget.snapshot.pid
        )
        log(
            attemptID,
            "phase=dispatch result=posted method=pid_targeted source=combined_session keyCode=\(pasteEvents.virtualKey) target=\(dispatchTarget.snapshot.redactedDescription)"
        )

        guard attemptGeneration == generation else {
            return manualPasteReport(
                attemptID: attemptID,
                reason: .superseded,
                token: ownershipToken
            )
        }
        guard expectation.canVerify else {
            log(attemptID, "phase=verification result=unavailable")
            return manualPasteReport(
                attemptID: attemptID,
                reason: .verificationUnavailable,
                token: ownershipToken
            )
        }

        let verificationStart = now()
        let deadline = verificationStart.addingTimeInterval(
            timing.verificationTimeout
        )
        repeat {
            let after = controller.textState(
                for: dispatchTarget,
                precedingUTF16Length: (request.text as NSString).length
            )
            if expectation.isSatisfied(by: after) {
                let latency = Int(
                    (now().timeIntervalSince(verificationStart) * 1_000)
                        .rounded()
                )
                let clipboard = finishClipboard()
                let submit = await submitIfRequested(
                    request.submitAfterPaste,
                    target: dispatchTarget,
                    attemptGeneration: attemptGeneration,
                    attemptID: attemptID
                )
                log(
                    attemptID,
                    "phase=complete result=verified latencyMs=\(latency) clipboard=\(clipboard.logCode) submit=\(submit.logCode)"
                )
                return report(
                    attemptID,
                    result: .pastedVerified(
                        clipboard: clipboard,
                        submit: submit
                    ),
                    verificationLatencyMilliseconds: latency
                )
            }
            await sleep(timing.verificationPollNanoseconds)
            guard attemptGeneration == generation else {
                return manualPasteReport(
                    attemptID: attemptID,
                    reason: .superseded,
                    token: ownershipToken
                )
            }
        } while now() < deadline

        log(attemptID, "phase=verification result=timed_out")
        return manualPasteReport(
            attemptID: attemptID,
            reason: .verificationTimedOut,
            token: ownershipToken
        )
    }

    private func resolveTarget(
        recordingAnchor: PasteTargetSnapshot?
    ) async -> TargetResolution {
        let deadline = now().addingTimeInterval(
            timing.targetResolutionTimeout
        )
        func deadlineExceeded() -> TargetResolution {
            .safetyIndeterminate(detail: "targetResolutionDeadlineExceeded")
        }
        var lastDetail = "not_probed"
        var lastProbeWasSafetyIndeterminate = false
        let currentPID = controller.currentProcessIdentifier()
        for attempt in 0..<3 {
            guard now() < deadline else {
                return deadlineExceeded()
            }
            let probe = controller.snapshotFocusedTargetProbe()
            guard now() < deadline else {
                return deadlineExceeded()
            }
            lastDetail = probe.detail
            lastProbeWasSafetyIndeterminate = probe.isSafetyIndeterminate
            if let context = probe.context {
                let frontmostPID = controller.frontmostProcessIdentifier()
                let contextIsCurrent = frontmostPID == context.snapshot.pid
                let contextMayBeBehindOwnUI = frontmostPID == currentPID
                    && recordingAnchor?.pid == context.snapshot.pid
                if controller.isUnsafeForClipboard(context.snapshot),
                   contextIsCurrent || contextMayBeBehindOwnUI {
                    return .unsafe(context)
                }
                if controller.isEligible(context.snapshot), contextIsCurrent {
                    return .target(context)
                }
                if !contextIsCurrent {
                    lastDetail += " contextFrontmostMismatch=true"
                }
            }
            if attempt < 2 {
                await sleep(timing.targetRetryNanoseconds)
            }
        }

        if controller.frontmostProcessIdentifier() == currentPID,
           let recordingAnchor,
           controller.isEligible(recordingAnchor),
           controller.activateApplication(for: recordingAnchor) {
            guard now() < deadline else {
                return deadlineExceeded()
            }
            await sleep(timing.targetRetryNanoseconds)
            guard now() < deadline else {
                return deadlineExceeded()
            }
            let probe = controller.snapshotFocusedTargetProbe()
            guard now() < deadline else {
                return deadlineExceeded()
            }
            lastDetail = probe.detail
            lastProbeWasSafetyIndeterminate = probe.isSafetyIndeterminate
            if let context = probe.context {
                let contextIsCurrent =
                    controller.frontmostProcessIdentifier()
                    == context.snapshot.pid
                if controller.isUnsafeForClipboard(context.snapshot),
                   contextIsCurrent {
                    return .unsafe(context)
                }
                if controller.isEligible(context.snapshot), contextIsCurrent {
                    return .target(context)
                }
            }
        }

        if lastProbeWasSafetyIndeterminate {
            return .safetyIndeterminate(detail: lastDetail)
        }
        return .missing(detail: lastDetail)
    }

    private func blockedAfterPasteboardPreparation(
        attemptID: UUID,
        reason: PasteBlockReason
    ) -> PasteAttemptReport {
        log(
            attemptID,
            "phase=dispatch result=blocked_\(reason.logCode) clipboard=kept_non_destructive"
        )
        return report(
            attemptID,
            result: .blocked(
                reason: reason,
                transcript: .recoveryMenu
            )
        )
    }

    private func copyForManualPaste(
        _ text: String,
        attemptID: UUID,
        reason: PasteFailureReason
    ) -> PasteAttemptReport {
        let result = controller.prepareAutoPaste(
            text,
            attemptID: attemptID
        )
        switch result {
        case .success:
            break
        case .writeFailed(let disposition):
            return report(
                attemptID,
                result: .failed(
                    reason: .pasteboardWriteFailed(
                        disposition: disposition
                    )
                )
            )
        }
        log(
            attemptID,
            "phase=complete result=manual_paste_fallback reason=\(reason.logCode)"
        )
        return report(
            attemptID,
            result: .manualPasteFallback(
                reason: reason,
                availability: .clipboard
            )
        )
    }

    private func manualPasteReport(
        attemptID: UUID,
        reason: PasteFailureReason,
        token: PasteboardOwnershipToken
    ) -> PasteAttemptReport {
        let availability: PasteManualPasteAvailability
        if controller.ownsPasteboard(token) {
            availability = .clipboard
        } else {
            availability = .recoveryMenu
        }
        log(
            attemptID,
            "phase=manual_fallback reason=\(reason.logCode) availability=\(availability.logCode)"
        )
        return report(
            attemptID,
            result: .manualPasteFallback(
                reason: reason,
                availability: availability
            )
        )
    }

    private func supersededBeforePasteDispatch(
        attemptID: UUID
    ) -> PasteAttemptReport {
        log(
            attemptID,
            "phase=cancel result=superseded_before_dispatch clipboard=kept_non_destructive"
        )
        return report(
            attemptID,
            result: .failed(reason: .superseded)
        )
    }

    private func finishClipboard() -> PasteClipboardDisposition {
        return .kept
    }

    private func submitIfRequested(
        _ requested: Bool,
        target: PasteTargetContext,
        attemptGeneration: Int,
        attemptID: UUID
    ) async -> PasteSubmitResult {
        guard requested else {
            return .notRequested
        }
        await sleep(timing.submitDelayNanoseconds)
        guard let returnEvents = controller.makeReturnKeyEventPair() else {
            log(attemptID, "phase=submit result=event_unavailable")
            return .eventUnavailable
        }
        guard attemptGeneration == generation,
              controller.validateFocus(target) == .matched,
              controller.frontmostProcessIdentifier()
                == target.snapshot.pid else {
            log(attemptID, "phase=submit result=target_changed")
            return .skippedTargetChanged
        }
        controller.postKeyDown(returnEvents, to: target.snapshot.pid)
        await sleep(timing.keyUpDelayNanoseconds)
        controller.postKeyUp(returnEvents, to: target.snapshot.pid)
        return .sent
    }

    private func report(
        _ attemptID: UUID,
        result: PasteAttemptResult,
        verificationLatencyMilliseconds: Int? = nil
    ) -> PasteAttemptReport {
        let latency = verificationLatencyMilliseconds.map(String.init)
            ?? "unavailable"
        log(
            attemptID,
            "phase=result \(result.logDescription) verificationLatencyMs=\(latency)"
        )
        return PasteAttemptReport(
            attemptID: attemptID,
            result: result,
            verificationLatencyMilliseconds: verificationLatencyMilliseconds
        )
    }

    private func log(_ attemptID: UUID, _ message: String) {
        logger("paste attempt id=\(attemptID.uuidString) \(message)")
    }
}

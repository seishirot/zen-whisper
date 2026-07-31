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
    case pasteboardRestoreFailed
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
        case .pasteboardRestoreFailed:
            return "pasteboard_restore_failed"
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
    case restored
    case kept
    case externalChangePreserved
    case restoreFailed

    var logCode: String {
        switch self {
        case .restored:
            return "restored"
        case .kept:
            return "kept"
        case .externalChangePreserved:
            return "external_change_preserved"
        case .restoreFailed:
            return "restore_failed"
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
    private var retainedRestoreToken: PasteboardRestoreToken?
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
                    : .accessibilityUnavailable,
                retainOriginalClipboard:
                    request.outputMode.restoresClipboardAfterPaste
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
                reason: .copyOnlyMode,
                retainOriginalClipboard: false
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
                reason: .noEditableTarget,
                retainOriginalClipboard: request.outputMode.restoresClipboardAfterPaste
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
                reason: .eventPermissionUnavailable,
                retainOriginalClipboard: request.outputMode.restoresClipboardAfterPaste
            )
        }

        let pasteboardWrite = controller.prepareAutoPaste(
            request.text,
            attemptID: attemptID,
            preservingBaseFrom: retainedRestoreToken
        )
        let restoreToken: PasteboardRestoreToken
        switch pasteboardWrite {
        case .success(let token):
            restoreToken = token
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
        if request.outputMode.restoresClipboardAfterPaste {
            retainedRestoreToken = restoreToken
        } else {
            retainedRestoreToken = nil
        }
        log(attemptID, "phase=pasteboard result=prepared")

        await sleep(timing.pasteboardSettleNanoseconds)
        guard attemptGeneration == generation else {
            return supersededBeforePasteDispatch(
                attemptID: attemptID,
                token: restoreToken
            )
        }

        let dispatchResolution = await resolveTarget(
            recordingAnchor: request.recordingAnchor
        )
        guard attemptGeneration == generation else {
            return supersededBeforePasteDispatch(
                attemptID: attemptID,
                token: restoreToken
            )
        }
        let dispatchTarget: PasteTargetContext
        switch dispatchResolution {
        case .unsafe:
            return blockedAfterPasteboardPreparation(
                attemptID: attemptID,
                reason: .unsafeTarget,
                token: restoreToken
            )
        case .safetyIndeterminate(let detail):
            log(
                attemptID,
                "phase=dispatch result=safety_indeterminate detail=\(detail)"
            )
            return blockedAfterPasteboardPreparation(
                attemptID: attemptID,
                reason: .targetSafetyIndeterminate,
                token: restoreToken
            )
        case .missing(let detail):
            log(attemptID, "phase=dispatch result=target_missing detail=\(detail)")
            return manualPasteReport(
                attemptID: attemptID,
                reason: .noEditableTarget,
                token: restoreToken
            )
        case .target(let target):
            dispatchTarget = target
        }

        guard controller.frontmostProcessIdentifier()
            == dispatchTarget.snapshot.pid else {
            log(attemptID, "phase=dispatch result=frontmost_changed")
            return manualPasteReport(
                attemptID: attemptID,
                reason: .noEditableTarget,
                token: restoreToken
            )
        }

        guard let pasteEvents = controller.makePasteKeyEventPair() else {
            return manualPasteReport(
                attemptID: attemptID,
                reason: .eventPermissionUnavailable,
                token: restoreToken
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
                token: restoreToken
            )
        case .unsafe:
            return blockedAfterPasteboardPreparation(
                attemptID: attemptID,
                reason: .unsafeTarget,
                token: restoreToken
            )
        case .safetyIndeterminate:
            return blockedAfterPasteboardPreparation(
                attemptID: attemptID,
                reason: .targetSafetyIndeterminate,
                token: restoreToken
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
                token: restoreToken
            )
        }
        guard attemptGeneration == generation else {
            return supersededBeforePasteDispatch(
                attemptID: attemptID,
                token: restoreToken
            )
        }
        guard controller.ownsPasteboard(restoreToken) else {
            log(
                attemptID,
                "phase=dispatch result=clipboard_ownership_lost_before_keydown"
            )
            return manualPasteReport(
                attemptID: attemptID,
                reason: .verificationUnavailable,
                token: restoreToken
            )
        }

        controller.postKeyDown(pasteEvents)
        await sleep(timing.keyUpDelayNanoseconds)
        controller.postKeyUp(pasteEvents)
        log(
            attemptID,
            "phase=dispatch result=posted method=annotated_session source=combined_session keyCode=\(pasteEvents.virtualKey) target=\(dispatchTarget.snapshot.redactedDescription)"
        )

        guard attemptGeneration == generation else {
            return manualPasteReport(
                attemptID: attemptID,
                reason: .superseded,
                token: restoreToken
            )
        }
        guard expectation.canVerify else {
            log(attemptID, "phase=verification result=unavailable")
            return manualPasteReport(
                attemptID: attemptID,
                reason: .verificationUnavailable,
                token: restoreToken
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
                let clipboard = finishClipboard(
                    mode: request.outputMode,
                    token: restoreToken
                )
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
                    token: restoreToken
                )
            }
        } while now() < deadline

        log(attemptID, "phase=verification result=timed_out")
        return manualPasteReport(
            attemptID: attemptID,
            reason: .verificationTimedOut,
            token: restoreToken
        )
    }

    private func resolveTarget(
        recordingAnchor: PasteTargetSnapshot?
    ) async -> TargetResolution {
        var lastDetail = "not_probed"
        var lastProbeWasSafetyIndeterminate = false
        let currentPID = controller.currentProcessIdentifier()
        for attempt in 0..<3 {
            let probe = controller.snapshotFocusedTargetProbe()
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
            await sleep(timing.targetRetryNanoseconds)
            let probe = controller.snapshotFocusedTargetProbe()
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
        reason: PasteBlockReason,
        token: PasteboardRestoreToken
    ) -> PasteAttemptReport {
        let restoreResult = controller.restoreIfOwned(token)
        retainedRestoreToken = nil
        log(
            attemptID,
            "phase=dispatch result=blocked_\(reason.logCode) restore=\(restoreResult)"
        )
        switch restoreResult {
        case .restored:
            return report(
                attemptID,
                result: .blocked(
                    reason: reason,
                    transcript: .intentionallyDiscarded
                )
            )
        case .ownershipLost:
            return report(
                attemptID,
                result: .blocked(
                    reason: reason,
                    transcript: .recoveryMenu
                )
            )
        case .failed:
            return report(
                attemptID,
                result: .failed(reason: .pasteboardRestoreFailed)
            )
        }
    }

    private func copyForManualPaste(
        _ text: String,
        attemptID: UUID,
        reason: PasteFailureReason,
        retainOriginalClipboard: Bool
    ) -> PasteAttemptReport {
        let result = controller.prepareAutoPaste(
            text,
            attemptID: attemptID,
            preservingBaseFrom: retainOriginalClipboard
                ? retainedRestoreToken
                : nil
        )
        let token: PasteboardRestoreToken
        switch result {
        case .success(let preparedToken):
            token = preparedToken
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
        retainedRestoreToken = retainOriginalClipboard ? token : nil
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
        token: PasteboardRestoreToken
    ) -> PasteAttemptReport {
        let availability: PasteManualPasteAvailability
        if controller.ownsPasteboard(token) {
            availability = .clipboard
        } else {
            retainedRestoreToken = nil
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
        attemptID: UUID,
        token: PasteboardRestoreToken
    ) -> PasteAttemptReport {
        let restoreResult = controller.restoreIfOwned(token)
        retainedRestoreToken = nil
        log(
            attemptID,
            "phase=cancel result=superseded_before_dispatch restore=\(restoreResult)"
        )
        if restoreResult == .failed {
            return report(
                attemptID,
                result: .failed(reason: .pasteboardRestoreFailed)
            )
        }
        return report(
            attemptID,
            result: .failed(reason: .superseded)
        )
    }

    private func finishClipboard(
        mode: OutputMode,
        token: PasteboardRestoreToken
    ) -> PasteClipboardDisposition {
        guard mode.restoresClipboardAfterPaste else {
            retainedRestoreToken = nil
            return .kept
        }
        switch controller.restoreIfOwned(token) {
        case .restored:
            retainedRestoreToken = nil
            return .restored
        case .ownershipLost:
            retainedRestoreToken = nil
            return .externalChangePreserved
        case .failed:
            retainedRestoreToken = nil
            return .restoreFailed
        }
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
        guard attemptGeneration == generation,
              controller.validateFocus(target) == .matched,
              controller.frontmostProcessIdentifier()
                == target.snapshot.pid else {
            log(attemptID, "phase=submit result=target_changed")
            return .skippedTargetChanged
        }
        guard let returnEvents = controller.makeReturnKeyEventPair() else {
            log(attemptID, "phase=submit result=event_unavailable")
            return .eventUnavailable
        }
        controller.postKeyDown(returnEvents)
        await sleep(timing.keyUpDelayNanoseconds)
        controller.postKeyUp(returnEvents)
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

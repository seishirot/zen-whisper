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
    case pasteboardWriteFailed(restoreSucceeded: Bool)
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
        case .pasteboardWriteFailed(let restoreSucceeded):
            return restoreSucceeded
                ? "pasteboard_write_failed"
                : "pasteboard_write_and_restore_failed"
        case .superseded:
            return "superseded"
        }
    }
}

enum PasteBlockReason: Equatable {
    case unsafeTarget

    var logCode: String {
        "unsafe_target"
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
    case copiedForManualPaste(reason: PasteFailureReason)
    case blocked(reason: PasteBlockReason)
    case failed(reason: PasteFailureReason)

    var logDescription: String {
        switch self {
        case .pastedVerified(let clipboard, let submit):
            return "outcome=pasted_verified clipboard=\(clipboard.logCode) submit=\(submit.logCode)"
        case .copiedForManualPaste(let reason):
            return "outcome=copied_for_manual_paste reason=\(reason.logCode)"
        case .blocked(let reason):
            return "outcome=blocked reason=\(reason.logCode)"
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

        if request.outputMode == .copyOnly {
            let resolution = await resolveTarget(recordingAnchor: request.recordingAnchor)
            if case .unsafe = resolution {
                return report(
                    attemptID,
                    result: .blocked(reason: .unsafeTarget)
                )
            }
            return copyForManualPaste(
                request.text,
                attemptID: attemptID,
                reason: .copyOnlyMode,
                retainOriginalClipboard: false
            )
        }

        guard controller.isAccessibilityTrusted() else {
            return copyForManualPaste(
                request.text,
                attemptID: attemptID,
                reason: .accessibilityUnavailable,
                retainOriginalClipboard: request.outputMode.restoresClipboardAfterPaste
            )
        }

        let initialResolution = await resolveTarget(
            recordingAnchor: request.recordingAnchor
        )
        switch initialResolution {
        case .unsafe:
            return report(attemptID, result: .blocked(reason: .unsafeTarget))
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
        guard case .success(let restoreToken) = pasteboardWrite else {
            let restoreSucceeded: Bool
            if case .writeFailed(let didRestore) = pasteboardWrite {
                restoreSucceeded = didRestore
            } else {
                restoreSucceeded = true
            }
            return report(
                attemptID,
                result: .failed(
                    reason: .pasteboardWriteFailed(
                        restoreSucceeded: restoreSucceeded
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
            return report(attemptID, result: .failed(reason: .superseded))
        }

        let dispatchResolution = await resolveTarget(
            recordingAnchor: request.recordingAnchor
        )
        let dispatchTarget: PasteTargetContext
        switch dispatchResolution {
        case .unsafe:
            let restoreResult = controller.restoreIfOwned(restoreToken)
            retainedRestoreToken = nil
            log(
                attemptID,
                "phase=dispatch result=blocked_unsafe restore=\(restoreResult)"
            )
            return report(attemptID, result: .blocked(reason: .unsafeTarget))
        case .missing(let detail):
            log(attemptID, "phase=dispatch result=target_missing detail=\(detail)")
            return report(
                attemptID,
                result: .copiedForManualPaste(reason: .noEditableTarget)
            )
        case .target(let target):
            dispatchTarget = target
        }

        guard controller.frontmostProcessIdentifier()
            == dispatchTarget.snapshot.pid else {
            log(attemptID, "phase=dispatch result=frontmost_changed")
            return report(
                attemptID,
                result: .copiedForManualPaste(reason: .noEditableTarget)
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
        guard controller.isFocused(dispatchTarget),
              controller.frontmostProcessIdentifier()
                == dispatchTarget.snapshot.pid else {
            log(attemptID, "phase=dispatch result=focus_changed_before_keydown")
            return report(
                attemptID,
                result: .copiedForManualPaste(reason: .noEditableTarget)
            )
        }
        guard let pasteEvents = controller.makePasteKeyEventPair() else {
            return report(
                attemptID,
                result: .copiedForManualPaste(
                    reason: .eventPermissionUnavailable
                )
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
            return report(attemptID, result: .failed(reason: .superseded))
        }
        guard expectation.canVerify else {
            log(attemptID, "phase=verification result=unavailable")
            return report(
                attemptID,
                result: .copiedForManualPaste(
                    reason: .verificationUnavailable
                )
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
                return report(attemptID, result: .failed(reason: .superseded))
            }
        } while now() < deadline

        log(attemptID, "phase=verification result=timed_out")
        return report(
            attemptID,
            result: .copiedForManualPaste(reason: .verificationTimedOut)
        )
    }

    private func resolveTarget(
        recordingAnchor: PasteTargetSnapshot?
    ) async -> TargetResolution {
        var lastDetail = "not_probed"
        let currentPID = controller.currentProcessIdentifier()
        for attempt in 0..<3 {
            let probe = controller.snapshotFocusedTargetProbe()
            lastDetail = probe.detail
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

        return .missing(detail: lastDetail)
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
        guard case .success(let token) = result else {
            let restoreSucceeded: Bool
            if case .writeFailed(let didRestore) = result {
                restoreSucceeded = didRestore
            } else {
                restoreSucceeded = true
            }
            return report(
                attemptID,
                result: .failed(
                    reason: .pasteboardWriteFailed(
                        restoreSucceeded: restoreSucceeded
                    )
                )
            )
        }
        retainedRestoreToken = retainOriginalClipboard ? token : nil
        log(
            attemptID,
            "phase=complete result=copied_for_manual_paste reason=\(reason.logCode)"
        )
        return report(
            attemptID,
            result: .copiedForManualPaste(reason: reason)
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
              controller.isFocused(target),
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

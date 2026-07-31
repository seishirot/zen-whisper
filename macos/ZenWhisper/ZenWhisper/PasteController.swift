import AppKit
import ApplicationServices
import Carbon
import CryptoKit
import Foundation

struct PasteTargetSnapshot: Equatable {
    let pid: pid_t
    let bundleIdentifier: String
    let role: String
    let subrole: String
    let windowTitle: String
    let windowFrame: CGRect
    let elementIdentifier: String
    let elementFrame: CGRect
    let hasEditableValue: Bool
    let canSetSelectedText: Bool
    let canSetSelectedTextRange: Bool
    let hasReadableValue: Bool
    let hasReadableSelectedTextRange: Bool
    let isProtectedContent: Bool
    let searchableText: String
    let discovery: String

    init(
        pid: pid_t,
        bundleIdentifier: String,
        role: String,
        subrole: String,
        windowTitle: String,
        windowFrame: CGRect,
        elementIdentifier: String,
        elementFrame: CGRect,
        hasEditableValue: Bool,
        canSetSelectedText: Bool = false,
        canSetSelectedTextRange: Bool = false,
        hasReadableValue: Bool = false,
        hasReadableSelectedTextRange: Bool = false,
        isProtectedContent: Bool,
        searchableText: String,
        discovery: String
    ) {
        self.pid = pid
        self.bundleIdentifier = bundleIdentifier
        self.role = role
        self.subrole = subrole
        self.windowTitle = windowTitle
        self.windowFrame = windowFrame
        self.elementIdentifier = elementIdentifier
        self.elementFrame = elementFrame
        self.hasEditableValue = hasEditableValue
        self.canSetSelectedText = canSetSelectedText
        self.canSetSelectedTextRange = canSetSelectedTextRange
        self.hasReadableValue = hasReadableValue
        self.hasReadableSelectedTextRange = hasReadableSelectedTextRange
        self.isProtectedContent = isProtectedContent
        self.searchableText = searchableText
        self.discovery = discovery
    }

    var redactedDescription: String {
        "appHash=\(redactedAppIdentityHash(pid: pid, bundleIdentifier: bundleIdentifier)) role=\(role) subrole=\(subrole) editable=\(hasEditableValue) selectedTextSettable=\(canSetSelectedText) selectedRangeSettable=\(canSetSelectedTextRange) valueReadable=\(hasReadableValue) selectedRangeReadable=\(hasReadableSelectedTextRange) protected=\(isProtectedContent) discovery=\(discovery) window=\(frameDescription(windowFrame)) element=\(frameDescription(elementFrame))"
    }

    private func frameDescription(_ frame: CGRect) -> String {
        if frame.isNull {
            return "null"
        }
        return "\(Int(frame.origin.x)),\(Int(frame.origin.y)),\(Int(frame.size.width)),\(Int(frame.size.height))"
    }
}

struct PasteTargetContext {
    let snapshot: PasteTargetSnapshot
    fileprivate let element: AXUIElement?

#if DEBUG
    init(snapshot: PasteTargetSnapshot) {
        self.snapshot = snapshot
        element = nil
    }
#endif

    fileprivate init(snapshot: PasteTargetSnapshot, element: AXUIElement) {
        self.snapshot = snapshot
        self.element = element
    }
}

enum PasteTargetProbeOutcome {
    case target(PasteTargetContext)
    case noTarget
    case safetyIndeterminate
}

struct PasteTargetProbe {
    let outcome: PasteTargetProbeOutcome
    let detail: String

    init(context: PasteTargetContext?, detail: String) {
        if let context {
            outcome = .target(context)
        } else {
            outcome = .noTarget
        }
        self.detail = detail
    }

    init(outcome: PasteTargetProbeOutcome, detail: String) {
        self.outcome = outcome
        self.detail = detail
    }

    var context: PasteTargetContext? {
        guard case .target(let context) = outcome else {
            return nil
        }
        return context
    }

    var isSafetyIndeterminate: Bool {
        guard case .safetyIndeterminate = outcome else {
            return false
        }
        return true
    }

    var snapshot: PasteTargetSnapshot? {
        context?.snapshot
    }
}

private enum PasteTargetSnapshotResolution {
    case context(PasteTargetContext, detail: String)
    case noTarget(detail: String)
    case safetyIndeterminate(detail: String)

    var context: PasteTargetContext? {
        guard case .context(let context, _) = self else {
            return nil
        }
        return context
    }

    var detail: String {
        switch self {
        case .context(_, let detail),
             .noTarget(let detail),
             .safetyIndeterminate(let detail):
            return detail
        }
    }

    var isSafetyIndeterminate: Bool {
        guard case .safetyIndeterminate = self else {
            return false
        }
        return true
    }
}

private func redactedAppIdentityHash(pid: pid_t, bundleIdentifier: String) -> String {
    SHA256.hash(data: Data("\(pid)|\(bundleIdentifier)".utf8))
        .prefix(4)
        .map { String(format: "%02x", $0) }
        .joined()
}

private func redactedAppIdentityDescription(pid: pid_t, bundleIdentifier: String) -> String {
    "appHash=\(redactedAppIdentityHash(pid: pid, bundleIdentifier: bundleIdentifier))"
}

enum PasteboardWriteFailureDisposition: Equatable {
    case originalUntouched
    case restored
    case externalChangePreserved
    case restoreFailed

    var logCode: String {
        switch self {
        case .originalUntouched:
            return "original_untouched"
        case .restored:
            return "restored"
        case .externalChangePreserved:
            return "external_change_preserved"
        case .restoreFailed:
            return "restore_failed"
        }
    }
}

enum PasteboardWriteResult {
    case success(PasteboardRestoreToken)
    case writeFailed(disposition: PasteboardWriteFailureDisposition)
}

enum PlainPasteboardWriteResult {
    case copied
    case writeFailed(disposition: PasteboardWriteFailureDisposition)
}

enum PasteboardRestoreResult: Equatable {
    case restored
    case ownershipLost
    case failed
}

enum PasteFocusValidation: Equatable {
    case matched
    case changed
    case unsafe
    case safetyIndeterminate
}

struct PasteTextState: Equatable {
    let value: String?
    let selectedRange: NSRange?
    let textImmediatelyBeforeSelection: String?

    init(
        value: String?,
        selectedRange: NSRange?,
        textImmediatelyBeforeSelection: String? = nil
    ) {
        self.value = value
        self.selectedRange = selectedRange
        self.textImmediatelyBeforeSelection = textImmediatelyBeforeSelection
    }
}

struct PasteVerificationExpectation: Equatable {
    let before: PasteTextState
    let insertedText: String
    let expectedValue: String?
    let expectedCaretLocation: Int?

    init(before: PasteTextState, insertedText: String) {
        self.before = before
        self.insertedText = insertedText
        if let value = before.value,
           let selectedRange = before.selectedRange,
           selectedRange.location >= 0,
           selectedRange.length >= 0,
           NSMaxRange(selectedRange) <= (value as NSString).length {
            expectedValue = (value as NSString).replacingCharacters(
                in: selectedRange,
                with: insertedText
            )
            expectedCaretLocation = selectedRange.location + (insertedText as NSString).length
        } else {
            expectedValue = nil
            expectedCaretLocation = before.selectedRange.map {
                $0.location + (insertedText as NSString).length
            }
        }
    }

    var canVerify: Bool {
        expectedValue != nil || expectedCaretLocation != nil
    }

    func isSatisfied(by after: PasteTextState) -> Bool {
        if let expectedValue,
           expectedValue != before.value,
           after.value == expectedValue {
            return true
        }
        if let expectedCaretLocation,
           let selectedRange = after.selectedRange,
           selectedRange.location == expectedCaretLocation,
           selectedRange.length == 0 {
            return after.textImmediatelyBeforeSelection == insertedText
        }
        return false
    }
}

struct PasteKeyEventPair {
    fileprivate enum Storage {
        case events(keyDown: CGEvent, keyUp: CGEvent)
#if DEBUG
        case test
#endif
    }

    let virtualKey: CGKeyCode
    fileprivate let storage: Storage

    fileprivate init(
        virtualKey: CGKeyCode,
        keyDown: CGEvent,
        keyUp: CGEvent
    ) {
        self.virtualKey = virtualKey
        storage = .events(keyDown: keyDown, keyUp: keyUp)
    }

#if DEBUG
    init(testVirtualKey: CGKeyCode) {
        virtualKey = testVirtualKey
        storage = .test
    }
#endif
}

protocol PasteAttemptControlling: AnyObject {
    func isAccessibilityTrusted() -> Bool
    func snapshotFocusedTargetProbe() -> PasteTargetProbe
    func isUnsafeForClipboard(_ snapshot: PasteTargetSnapshot) -> Bool
    func isEligible(_ snapshot: PasteTargetSnapshot) -> Bool
    func activateApplication(for anchor: PasteTargetSnapshot) -> Bool
    func currentProcessIdentifier() -> pid_t
    func frontmostProcessIdentifier() -> pid_t?
    func canCreatePasteEvents() -> Bool
    func prepareAutoPaste(
        _ text: String,
        attemptID: UUID,
        preservingBaseFrom retainedToken: PasteboardRestoreToken?
    ) -> PasteboardWriteResult
    func restoreIfOwned(_ token: PasteboardRestoreToken) -> PasteboardRestoreResult
    func ownsPasteboard(_ token: PasteboardRestoreToken) -> Bool
    func textState(
        for context: PasteTargetContext,
        precedingUTF16Length: Int
    ) -> PasteTextState
    func validateFocus(_ approved: PasteTargetContext) -> PasteFocusValidation
    func makePasteKeyEventPair() -> PasteKeyEventPair?
    func makeReturnKeyEventPair() -> PasteKeyEventPair?
    func postKeyDown(_ pair: PasteKeyEventPair)
    func postKeyUp(_ pair: PasteKeyEventPair)
}

final class PasteController {
    private static let pasteAttemptPasteboardType = NSPasteboard.PasteboardType(
        "app.zen-whisper.paste-attempt"
    )
    private let pasteboard: NSPasteboard
    private let clearPasteboard: (NSPasteboard) -> Int
    private let writePasteboardItems: (
        NSPasteboard,
        [NSPasteboardItem]
    ) -> Bool
    private let restorePasteboardItems: (
        NSPasteboard,
        [NSPasteboardItem]
    ) -> Bool

    init(
        pasteboard: NSPasteboard = .general,
        clearPasteboard: @escaping (NSPasteboard) -> Int = {
            $0.clearContents()
        },
        writePasteboardItems: @escaping (
            NSPasteboard,
            [NSPasteboardItem]
        ) -> Bool = { pasteboard, items in
            pasteboard.writeObjects(items)
        },
        restorePasteboardItems: @escaping (
            NSPasteboard,
            [NSPasteboardItem]
        ) -> Bool = { pasteboard, items in
            pasteboard.clearContents()
            guard !items.isEmpty else {
                return true
            }
            return pasteboard.writeObjects(items)
        }
    ) {
        self.pasteboard = pasteboard
        self.clearPasteboard = clearPasteboard
        self.writePasteboardItems = writePasteboardItems
        self.restorePasteboardItems = restorePasteboardItems
    }

    func isAccessibilityTrusted() -> Bool {
        AXIsProcessTrusted()
    }

    func prepareAutoPaste(
        _ text: String,
        attemptID: UUID = UUID(),
        preservingBaseFrom retainedToken: PasteboardRestoreToken? = nil
    ) -> PasteboardWriteResult {
        let previous: PasteboardSnapshot
        if let retainedToken, ownsPasteboard(retainedToken) {
            previous = retainedToken.previous
        } else {
            previous = PasteboardSnapshot(pasteboard: pasteboard)
        }
        let item = NSPasteboardItem()
        guard item.setString(text, forType: .string),
              item.setString(
                attemptID.uuidString,
                forType: Self.pasteAttemptPasteboardType
              ) else {
            return .writeFailed(disposition: .originalUntouched)
        }
        let clearedChangeCount = clearPasteboard(pasteboard)
        guard writePasteboardItems(pasteboard, [item]) else {
            return .writeFailed(
                disposition: recoverAfterFailedWrite(
                    previous: previous,
                    ownedChangeCount: clearedChangeCount
                )
            )
        }
        return .success(PasteboardRestoreToken(
            previous: previous,
            writtenChangeCount: pasteboard.changeCount,
            text: text,
            attemptID: attemptID
        ))
    }

    func copyPlainText(_ text: String) -> PlainPasteboardWriteResult {
        let previous = PasteboardSnapshot(pasteboard: pasteboard)
        let item = NSPasteboardItem()
        guard item.setString(text, forType: .string) else {
            return .writeFailed(disposition: .originalUntouched)
        }
        let clearedChangeCount = clearPasteboard(pasteboard)
        guard writePasteboardItems(pasteboard, [item]) else {
            return .writeFailed(
                disposition: recoverAfterFailedWrite(
                    previous: previous,
                    ownedChangeCount: clearedChangeCount
                )
            )
        }
        return .copied
    }

    private func recoverAfterFailedWrite(
        previous: PasteboardSnapshot,
        ownedChangeCount: Int
    ) -> PasteboardWriteFailureDisposition {
        guard pasteboard.changeCount == ownedChangeCount else {
            return .externalChangePreserved
        }
        return previous.restore(
            to: pasteboard,
            using: restorePasteboardItems
        )
            ? .restored
            : .restoreFailed
    }

    @discardableResult
    private func restore(_ token: PasteboardRestoreToken) -> Bool {
        token.previous.restore(
            to: pasteboard,
            using: restorePasteboardItems
        )
    }

    func restoreIfOwned(_ token: PasteboardRestoreToken) -> PasteboardRestoreResult {
        guard ownsPasteboard(token) else {
            return .ownershipLost
        }
        return restore(token) ? .restored : .failed
    }

    func ownsPasteboard(_ token: PasteboardRestoreToken) -> Bool {
        return pasteboard.changeCount == token.writtenChangeCount
            && pasteboard.string(forType: .string) == token.text
            && pasteboard.string(forType: Self.pasteAttemptPasteboardType)
                == token.attemptID.uuidString
    }

    func snapshotFocusedTarget() -> PasteTargetSnapshot? {
        snapshotFocusedTargetProbe(searchWindowDescendants: false).snapshot
    }

    func snapshotFocusedTargetProbe() -> PasteTargetProbe {
        snapshotFocusedTargetProbe(searchWindowDescendants: true)
    }

    private func snapshotFocusedTargetProbe(searchWindowDescendants: Bool) -> PasteTargetProbe {
        let currentPID = NSRunningApplication.current.processIdentifier
        var detail: [String] = [
            "trusted=\(AXIsProcessTrusted())",
            "postEventAllowed=\(CGPreflightPostEventAccess())"
        ]
        var safetyIndeterminateDetail: String?
        func unresolvedProbe() -> PasteTargetProbe {
            PasteTargetProbe(
                outcome: safetyIndeterminateDetail == nil
                    ? .noTarget
                    : .safetyIndeterminate,
                detail: detail.joined(separator: " ")
            )
        }
        let systemWide = AXUIElementCreateSystemWide()
        let systemFocused = copyElementAttributeResult(systemWide, kAXFocusedUIElementAttribute as CFString)
        detail.append("systemFocused=\(describe(systemFocused))")
        if isIndeterminate(systemFocused) {
            safetyIndeterminateDetail = "systemFocused=\(describe(systemFocused))"
        }
        if let focused = systemFocused.element {
            let result = snapshotWithDetail(from: focused)
            detail.append("systemSnapshot=\(result.detail)")
            if result.isSafetyIndeterminate {
                safetyIndeterminateDetail = result.detail
            }
            if let context = result.context,
               isEligible(context.snapshot) || isUnsafeForClipboard(context.snapshot) {
                return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
            }
        }

        let focusedApp = copyElementAttributeResult(systemWide, kAXFocusedApplicationAttribute as CFString)
        detail.append("systemFocusedApp=\(describe(focusedApp))")
        if isIndeterminate(focusedApp) {
            safetyIndeterminateDetail = "systemFocusedApp=\(describe(focusedApp))"
        }
        if let axApp = focusedApp.element {
            var focusedAppPID: pid_t = 0
            let pidStatus = AXUIElementGetPid(axApp, &focusedAppPID)
            detail.append("focusedAppPidStatus=\(pidStatus.rawValue)")
            if pidStatus != .success {
                safetyIndeterminateDetail =
                    "focusedAppPidStatus=\(pidStatus.rawValue)"
            }
            if pidStatus == .success, focusedAppPID != currentPID {
                detail.append("focusedApp=\(redactedAppIdentityDescription(pid: focusedAppPID, bundleIdentifier: "<ax-focused>"))")
                let focused = copyElementAttributeResult(axApp, kAXFocusedUIElementAttribute as CFString)
                detail.append("focusedAppElement=\(describe(focused))")
                if isIndeterminate(focused) {
                    safetyIndeterminateDetail =
                        "focusedAppElement=\(describe(focused))"
                }
                if let focusedElement = focused.element {
                    let result = snapshotWithDetail(from: focusedElement, pid: focusedAppPID)
                    detail.append("focusedAppSnapshot=\(result.detail)")
                    if result.isSafetyIndeterminate {
                        safetyIndeterminateDetail = result.detail
                    }
                    if let context = result.context,
                       isEligible(context.snapshot) || isUnsafeForClipboard(context.snapshot) {
                        return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
                    }
                }
                let windowResult = snapshotFromWindowDescendant(
                    axApp: axApp,
                    pid: focusedAppPID,
                    label: "focusedApp",
                    searchWindowDescendants: searchWindowDescendants,
                    detail: &detail
                )
                if windowResult.isSafetyIndeterminate {
                    safetyIndeterminateDetail = windowResult.detail
                }
                if let context = windowResult.context {
                    return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
                }
            }
        }

        guard let app = NSWorkspace.shared.frontmostApplication else {
            detail.append("frontmost=nil")
            return unresolvedProbe()
        }
        detail.append(
            "frontmost=\(redactedAppIdentityDescription(pid: app.processIdentifier, bundleIdentifier: app.bundleIdentifier ?? "<nil>"))"
        )
        guard app.processIdentifier != NSRunningApplication.current.processIdentifier else {
            detail.append("frontmost=current")
            return unresolvedProbe()
        }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        let focused = copyElementAttributeResult(axApp, kAXFocusedUIElementAttribute as CFString)
        detail.append("frontmostElement=\(describe(focused))")
        if isIndeterminate(focused) {
            safetyIndeterminateDetail =
                "frontmostElement=\(describe(focused))"
        }
        if let focusedElement = focused.element {
            let result = snapshotWithDetail(from: focusedElement, pid: app.processIdentifier)
            detail.append("frontmostSnapshot=\(result.detail)")
            if result.isSafetyIndeterminate {
                safetyIndeterminateDetail = result.detail
            }
            if let context = result.context,
               isEligible(context.snapshot) || isUnsafeForClipboard(context.snapshot) {
                return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
            }
        }
        let windowResult = snapshotFromWindowDescendant(
            axApp: axApp,
            pid: app.processIdentifier,
            label: "frontmost",
            searchWindowDescendants: searchWindowDescendants,
            detail: &detail
        )
        if windowResult.isSafetyIndeterminate {
            safetyIndeterminateDetail = windowResult.detail
        }
        if let context = windowResult.context {
            return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
        }
        return unresolvedProbe()
    }

    private func snapshotFromWindowDescendant(
        axApp: AXUIElement,
        pid: pid_t,
        label: String,
        searchWindowDescendants: Bool,
        detail: inout [String]
    ) -> PasteTargetSnapshotResolution {
        guard searchWindowDescendants else {
            return .noTarget(detail: "windowSearchDisabled")
        }
        var indeterminateDetail: String?
        let focusedWindow = copyElementAttributeResult(axApp, kAXFocusedWindowAttribute as CFString)
        detail.append("\(label)FocusedWindow=\(describe(focusedWindow))")
        if isIndeterminate(focusedWindow) {
            indeterminateDetail = "focusedWindow=\(describe(focusedWindow))"
        }
        let focusedResult = searchWindow(
            focusedWindow.element,
            pid: pid,
            label: label,
            detail: &detail
        )
        if let context = focusedResult.context {
            return .context(context, detail: focusedResult.detail)
        }
        if focusedResult.isSafetyIndeterminate {
            indeterminateDetail = focusedResult.detail
        }

        let mainWindow = copyElementAttributeResult(
            axApp,
            kAXMainWindowAttribute as CFString
        )
        detail.append("\(label)MainWindow=\(describe(mainWindow))")
        if isIndeterminate(mainWindow) {
            indeterminateDetail = "mainWindow=\(describe(mainWindow))"
        }
        let mainResult = searchWindow(
            mainWindow.element,
            pid: pid,
            label: label,
            detail: &detail
        )
        if let context = mainResult.context {
            return .context(context, detail: mainResult.detail)
        }
        if mainResult.isSafetyIndeterminate {
            indeterminateDetail = mainResult.detail
        }
        if let indeterminateDetail {
            return .safetyIndeterminate(detail: indeterminateDetail)
        }
        return .noTarget(detail: "noWindowDescendantTarget")
    }

    private func searchWindow(
        _ window: AXUIElement?,
        pid: pid_t,
        label: String,
        detail: inout [String]
    ) -> PasteTargetSnapshotResolution {
        guard let window else {
            return .noTarget(detail: "windowMissing")
        }
        let result = snapshotEditableDescendant(in: window, pid: pid)
        detail.append("\(label)WindowSearch=\(result.detail)")
        return result
    }

    private func snapshotEditableDescendant(
        in root: AXUIElement,
        pid: pid_t,
        maxDepth: Int = 10,
        maxNodes: Int = 300
    ) -> PasteTargetSnapshotResolution {
        let result = searchPasteTree(
            root: root,
            maxDepth: maxDepth,
            maxNodes: maxNodes,
            nodeKey: { CFHash($0) },
            inspect: { element in
                let focused = copyBoolAttributeResult(
                    element,
                    kAXFocusedAttribute as CFString
                )
                let resolution = snapshotWithDetail(
                    from: element,
                    pid: pid,
                    discovery: "windowDescendant"
                )
                if let context = resolution.context {
                    let snapshot = context.snapshot
                    if isUnsafeForClipboard(snapshot) {
                        return PasteTreeNodeObservation(
                            focused: focused,
                            resolution: .candidate(
                                context: context,
                                kind: .unsafe,
                                sample: "role=\(snapshot.role):subrole=\(snapshot.subrole)",
                                foundDetail: snapshot.redactedDescription
                            )
                        )
                    }
                    if isEligible(snapshot) {
                        return PasteTreeNodeObservation(
                            focused: focused,
                            resolution: .candidate(
                                context: context,
                                kind: .eligible,
                                sample: "role=\(snapshot.role):subrole=\(snapshot.subrole)",
                                foundDetail: snapshot.redactedDescription
                            )
                        )
                    }
                    return PasteTreeNodeObservation(
                        focused: focused,
                        resolution: .noCandidate(
                            detail: "role=\(snapshot.role):subrole=\(snapshot.subrole):editable=\(snapshot.hasEditableValue)"
                        )
                    )
                }
                return PasteTreeNodeObservation(
                    focused: focused,
                    resolution: resolution.isSafetyIndeterminate
                        ? .safetyIndeterminate(detail: resolution.detail)
                        : .noCandidate(detail: resolution.detail)
                )
            },
            readChildren: { element in
                childAttributes.map { attribute in
                    let result = copyElementArrayAttributeResult(
                        element,
                        attribute
                    )
                    let failureDetail = isIndeterminate(result)
                        ? "childrenAttribute=\(attribute) error=\(result.error.rawValue) wrongType=\(result.wrongType)"
                        : nil
                    return PasteTreeChildRead(
                        nodes: result.elements,
                        failureDetail: failureDetail
                    )
                }
            }
        )
        switch result {
        case .found(let context, let detail):
            return .context(context, detail: detail)
        case .noTarget(let detail):
            return .noTarget(detail: detail)
        case .safetyIndeterminate(let detail):
            return .safetyIndeterminate(detail: detail)
        }
    }

    private var childAttributes: [CFString] {
        [
            kAXChildrenAttribute as CFString,
            "AXVisibleChildren" as CFString,
            "AXContents" as CFString
        ]
    }

    private var unsafeSearchTerms: [String] {
        [
            "password",
            "passcode",
            "secure",
            "secret",
            "token",
            "api key",
            "api-key",
            "apikey",
            "private key",
            "ssh key",
            "recovery code",
            "auth code",
            "authentication code",
            "verification code",
            "one-time",
            "otp",
            "mfa",
            "2fa",
            "pin",
            "credit card",
            "card number",
            "cvv",
            "cvc",
            "パスワード",
            "暗証番号",
            "秘密",
            "トークン",
            "認証コード",
            "確認コード"
        ]
    }

    private func snapshotWithDetail(
        from focused: AXUIElement,
        pid fallbackPID: pid_t? = nil,
        discovery: String = "focused"
    ) -> PasteTargetSnapshotResolution {
        var pid = fallbackPID ?? 0
        if fallbackPID == nil {
            let status = AXUIElementGetPid(focused, &pid)
            guard status == .success else {
                return .safetyIndeterminate(
                    detail: "pidStatus=\(status.rawValue)"
                )
            }
        }
        guard pid != NSRunningApplication.current.processIdentifier else {
            return .noTarget(detail: "selfPid")
        }
        let roleResult = copyStringAttributeResult(
            focused,
            kAXRoleAttribute as CFString
        )
        let subroleResult = copyStringAttributeResult(
            focused,
            kAXSubroleAttribute as CFString
        )
        let enabledResult = copyBoolAttributeResult(
            focused,
            kAXEnabledAttribute as CFString
        )
        let protectedResult = copyBoolAttributeResult(
            focused,
            "AXProtectedContent" as CFString
        )
        let safetyAttributes: PasteTargetSafetyAttributes
        switch resolvePasteTargetSafetyAttributes(
            role: roleResult,
            subrole: subroleResult,
            enabled: enabledResult,
            protectedContent: protectedResult
        ) {
        case .resolved(let attributes):
            safetyAttributes = attributes
        case .retry(let detail):
            return .safetyIndeterminate(detail: detail)
        }
        let role = safetyAttributes.role
        let subrole = safetyAttributes.subrole
        let enabled = safetyAttributes.enabled
        guard enabled else {
            return .noTarget(
                detail: "disabled role=\(role) subrole=\(subrole)"
            )
        }
        let windowResult = copyElementAttributeResult(
            focused,
            kAXWindowAttribute as CFString
        )
        guard windowResult.error == .success
                || windowResult.error.isBenignMissingAttribute,
              !windowResult.wrongType else {
            return .safetyIndeterminate(
                detail: "window=\(describe(windowResult))"
            )
        }
        let window = windowResult.element
        let windowTitleResult = window.map {
            copyStringAttributeResult($0, kAXTitleAttribute as CFString)
        }
        guard windowTitleResult?.failureDetail == nil else {
            return .safetyIndeterminate(
                detail: "windowTitle=\(windowTitleResult?.detail ?? "unknown")"
            )
        }
        let windowTitle = windowTitleResult?.value ?? ""
        let windowFrame = window.flatMap { frame(of: $0) } ?? .null
        let elementFrame = frame(of: focused) ?? .null
        let identifierResult = copyStringAttributeResult(
            focused,
            kAXIdentifierAttribute as CFString
        )
        guard identifierResult.failureDetail == nil else {
            return .safetyIndeterminate(
                detail: "identifier=\(identifierResult.detail)"
            )
        }
        let elementIdentifier = identifierResult.value ?? ""
        let searchableResult = searchableMetadata(
            for: focused,
            window: window,
            role: role,
            subrole: subrole
        )
        guard searchableResult.failure == nil else {
            return .safetyIndeterminate(
                detail: "metadata=\(searchableResult.failure ?? "unknown")"
            )
        }
        let searchable = searchableResult.text
        let hasEditableValue = canSetAttribute(focused, kAXValueAttribute as CFString)
        let canSetSelectedText = canSetAttribute(focused, kAXSelectedTextAttribute as CFString)
        let canSetSelectedTextRange = canSetAttribute(
            focused,
            kAXSelectedTextRangeAttribute as CFString
        )
        let hasReadableValue = hasReadableAttribute(focused, kAXValueAttribute as CFString)
        let hasReadableSelectedTextRange = hasReadableAttribute(
            focused,
            kAXSelectedTextRangeAttribute as CFString
        )
        let isProtectedContent = safetyAttributes.isProtectedContent
        let snapshot = PasteTargetSnapshot(
            pid: pid,
            bundleIdentifier: NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? "",
            role: role,
            subrole: subrole,
            windowTitle: windowTitle,
            windowFrame: windowFrame,
            elementIdentifier: elementIdentifier,
            elementFrame: elementFrame,
            hasEditableValue: hasEditableValue,
            canSetSelectedText: canSetSelectedText,
            canSetSelectedTextRange: canSetSelectedTextRange,
            hasReadableValue: hasReadableValue,
            hasReadableSelectedTextRange: hasReadableSelectedTextRange,
            isProtectedContent: isProtectedContent,
            searchableText: searchable,
            discovery: discovery
        )
        return .context(
            PasteTargetContext(snapshot: snapshot, element: focused),
            detail: snapshot.redactedDescription
        )
    }

    private func searchableMetadata(
        for focused: AXUIElement,
        window: AXUIElement?,
        role: String,
        subrole: String
    ) -> (text: String, failure: String?) {
        var pieces = [role, subrole]
        let elementMetadata = metadataPieces(from: focused, prefix: "element")
        guard elementMetadata.failure == nil else {
            return ("", elementMetadata.failure)
        }
        pieces.append(contentsOf: elementMetadata.pieces)

        let parentResult = copyElementAttributeResult(
            focused,
            kAXParentAttribute as CFString
        )
        guard parentResult.error == .success
                || parentResult.error.isBenignMissingAttribute,
              !parentResult.wrongType else {
            return ("", "parent=\(describe(parentResult))")
        }
        if let parent = parentResult.element {
            let parentMetadata = metadataPieces(from: parent, prefix: "parent")
            guard parentMetadata.failure == nil else {
                return ("", parentMetadata.failure)
            }
            pieces.append(contentsOf: parentMetadata.pieces)
        }
        if let window {
            let windowMetadata = metadataPieces(from: window, prefix: "window")
            guard windowMetadata.failure == nil else {
                return ("", windowMetadata.failure)
            }
            pieces.append(contentsOf: windowMetadata.pieces)
        }
        return (pieces.joined(separator: " "), nil)
    }

    private func metadataPieces(
        from element: AXUIElement,
        prefix: String
    ) -> (pieces: [String], failure: String?) {
        var pieces: [String] = []
        for attribute in metadataAttributes {
            let result = copyStringAttributeResult(element, attribute)
            if let failure = result.failureDetail {
                return (
                    [],
                    "\(prefix).\(attribute)=\(failure)"
                )
            }
            if let value = result.value {
                pieces.append("\(prefix):\(value)")
            }
        }
        return (pieces, nil)
    }

    private var metadataAttributes: [CFString] {
        [
            kAXTitleAttribute as CFString,
            kAXDescriptionAttribute as CFString,
            kAXIdentifierAttribute as CFString,
            "AXPlaceholderValue" as CFString,
            "AXHelp" as CFString,
            "AXRoleDescription" as CFString
        ]
    }

    func canCreatePasteEvents() -> Bool {
        guard let virtualKey = KeyboardLayoutKeyCodeResolver.keyCode(for: "v") else {
            return false
        }
        return canCreateKeyEvents(virtualKey: virtualKey)
    }

    func makePasteKeyEventPair() -> PasteKeyEventPair? {
        guard let virtualKey = KeyboardLayoutKeyCodeResolver.keyCode(for: "v") else {
            return nil
        }
        return makeKeyEventPair(virtualKey: virtualKey, flags: .maskCommand)
    }

    func makeReturnKeyEventPair() -> PasteKeyEventPair? {
        makeKeyEventPair(virtualKey: CGKeyCode(kVK_Return), flags: [])
    }

    func postKeyDown(_ pair: PasteKeyEventPair) {
        guard case .events(let keyDown, _) = pair.storage else {
            return
        }
        keyDown.post(tap: .cgAnnotatedSessionEventTap)
    }

    func postKeyUp(_ pair: PasteKeyEventPair) {
        guard case .events(_, let keyUp) = pair.storage else {
            return
        }
        keyUp.post(tap: .cgAnnotatedSessionEventTap)
    }

    func textState(
        for context: PasteTargetContext,
        precedingUTF16Length: Int = 0
    ) -> PasteTextState {
        guard let element = context.element else {
            return PasteTextState(value: nil, selectedRange: nil)
        }
        let selectedRange = copyRangeAttribute(
            element,
            kAXSelectedTextRangeAttribute as CFString
        )
        let precedingText: String?
        if precedingUTF16Length > 0,
           let selectedRange,
           selectedRange.length == 0,
           selectedRange.location >= precedingUTF16Length {
            precedingText = copyStringForRange(
                element,
                range: NSRange(
                    location: selectedRange.location - precedingUTF16Length,
                    length: precedingUTF16Length
                )
            )
        } else {
            precedingText = nil
        }
        return PasteTextState(
            value: copyStringAttribute(element, kAXValueAttribute as CFString),
            selectedRange: selectedRange,
            textImmediatelyBeforeSelection: precedingText
        )
    }

    func validateFocus(
        _ approved: PasteTargetContext
    ) -> PasteFocusValidation {
        guard let approvedElement = approved.element else {
            return .changed
        }
        let probe = snapshotFocusedTargetProbe()
        switch probe.outcome {
        case .noTarget:
            return .changed
        case .safetyIndeterminate:
            return .safetyIndeterminate
        case .target(let current):
            guard let currentElement = current.element else {
                return .changed
            }
            return dispatchFocusValidation(
                approved: approved.snapshot,
                current: current.snapshot,
                elementsEqual: CFEqual(approvedElement, currentElement)
            )
        }
    }

    func dispatchFocusValidation(
        approved: PasteTargetSnapshot,
        current: PasteTargetSnapshot,
        elementsEqual: Bool
    ) -> PasteFocusValidation {
        if isUnsafeForClipboard(current) {
            return .unsafe
        }
        guard isEligible(current) else {
            return .changed
        }
        if elementsEqual {
            return .matched
        }
        guard approved.pid == current.pid,
              !approved.elementIdentifier.isEmpty,
              approved.elementIdentifier == current.elementIdentifier else {
            return .changed
        }
        return approved.role == current.role
            && approved.subrole == current.subrole
            ? .matched
            : .changed
    }

    func currentProcessIdentifier() -> pid_t {
        NSRunningApplication.current.processIdentifier
    }

    func frontmostProcessIdentifier() -> pid_t? {
        NSWorkspace.shared.frontmostApplication?.processIdentifier
    }

    @discardableResult
    func activateApplication(for anchor: PasteTargetSnapshot) -> Bool {
        guard let app = NSRunningApplication(processIdentifier: anchor.pid),
              !app.isTerminated else {
            return false
        }
        return app.activate(options: [])
    }

    private func canCreateKeyEvents(virtualKey: CGKeyCode) -> Bool {
        guard CGPreflightPostEventAccess() else {
            return false
        }
        return makeKeyEventPair(virtualKey: virtualKey, flags: []) != nil
    }

    private func makeKeyEventPair(
        virtualKey: CGKeyCode,
        flags: CGEventFlags
    ) -> PasteKeyEventPair? {
        guard CGPreflightPostEventAccess(),
              let source = CGEventSource(stateID: .combinedSessionState),
              let keyDown = CGEvent(
                keyboardEventSource: source,
                virtualKey: virtualKey,
                keyDown: true
              ),
              let keyUp = CGEvent(
                keyboardEventSource: source,
                virtualKey: virtualKey,
                keyDown: false
              ) else {
            return nil
        }
        keyDown.flags = flags
        keyUp.flags = flags
        return PasteKeyEventPair(
            virtualKey: virtualKey,
            keyDown: keyDown,
            keyUp: keyUp
        )
    }

    func isEligible(_ snapshot: PasteTargetSnapshot) -> Bool {
        let hasTextInputCapability = snapshot.hasEditableValue
            || snapshot.canSetSelectedText
            || snapshot.canSetSelectedTextRange
            || snapshot.hasReadableSelectedTextRange
        guard hasTextInputCapability else {
            return false
        }
        if isUnsafeForClipboard(snapshot) {
            return false
        }
        if snapshot.role == "AXTextField" || snapshot.role == "AXTextArea" || snapshot.role == "AXComboBox" {
            return true
        }
        if snapshot.subrole == "AXSearchField" {
            return true
        }
        if snapshot.role == "AXWebArea" {
            return snapshot.canSetSelectedText
        }
        return false
    }

    func isUnsafeForClipboard(_ snapshot: PasteTargetSnapshot) -> Bool {
        let lower = snapshot.searchableText.lowercased()
        if unsafeSearchTerms.contains(where: { unsafeTermMatches($0, in: lower) }) {
            return true
        }
        if snapshot.isProtectedContent {
            return true
        }
        if snapshot.role.contains("AXSecureTextField") || snapshot.subrole.contains("AXSecureTextField") {
            return true
        }
        return false
    }

    private func unsafeTermMatches(_ term: String, in lower: String) -> Bool {
        if isShortLatinTerm(term) {
            return lower.range(
                of: "\\b\(NSRegularExpression.escapedPattern(for: term))\\b",
                options: .regularExpression
            ) != nil
        }
        return lower.contains(term)
    }

    private func isShortLatinTerm(_ term: String) -> Bool {
        term.count <= 4 && term.range(of: #"^[a-z0-9 ]+$"#, options: .regularExpression) != nil
    }

    func sameLogicalTarget(_ lhs: PasteTargetSnapshot, _ rhs: PasteTargetSnapshot) -> Bool {
        return lhs.pid == rhs.pid
            && identityMatches(lhs.bundleIdentifier, rhs.bundleIdentifier)
            && lhs.role == rhs.role
            && lhs.subrole == rhs.subrole
            && identityMatches(lhs.elementIdentifier, rhs.elementIdentifier)
    }

    private func identityMatches(_ lhs: String, _ rhs: String) -> Bool {
        if lhs.isEmpty || rhs.isEmpty {
            return true
        }
        return lhs == rhs
    }
}

extension PasteController: PasteAttemptControlling {}

private struct AXElementAttributeResult {
    let element: AXUIElement?
    let error: AXError
    let wrongType: Bool
}

private struct AXElementArrayAttributeResult {
    let elements: [AXUIElement]
    let error: AXError
    let wrongType: Bool
}

private func isIndeterminate(
    _ result: AXElementAttributeResult
) -> Bool {
    result.wrongType
        || (
            result.error != .success
                && !result.error.isBenignMissingAttribute
        )
}

private func isIndeterminate(
    _ result: AXElementArrayAttributeResult
) -> Bool {
    result.wrongType
        || (
            result.error != .success
                && !result.error.isBenignMissingAttribute
        )
}

enum AXScalarAttributeResult<Value> {
    case value(Value)
    case missing
    case failed(String)

    var value: Value? {
        guard case .value(let value) = self else {
            return nil
        }
        return value
    }

    var failureDetail: String? {
        guard case .failed(let detail) = self else {
            return nil
        }
        return detail
    }

    var detail: String {
        switch self {
        case .value:
            return "ok"
        case .missing:
            return "missing"
        case .failed(let detail):
            return detail
        }
    }
}

struct PasteTargetSafetyAttributes: Equatable {
    let role: String
    let subrole: String
    let enabled: Bool
    let isProtectedContent: Bool
}

enum PasteDescendantFocusResolution: Equatable {
    case focused(Bool)
    case safetyIndeterminate(String)
}

enum PasteTreeCandidateKind: Equatable {
    case eligible
    case unsafe
}

enum PasteTreeNodeResolution<Context> {
    case candidate(
        context: Context,
        kind: PasteTreeCandidateKind,
        sample: String,
        foundDetail: String
    )
    case noCandidate(detail: String)
    case safetyIndeterminate(detail: String)

    var couldReceivePaste: Bool {
        switch self {
        case .candidate, .safetyIndeterminate:
            return true
        case .noCandidate:
            return false
        }
    }
}

struct PasteTreeNodeObservation<Context> {
    let focused: AXScalarAttributeResult<Bool>
    let resolution: PasteTreeNodeResolution<Context>
}

struct PasteTreeChildRead<Node> {
    let nodes: [Node]
    let failureDetail: String?
}

enum PasteTreeSearchResult<Context> {
    case found(Context, detail: String)
    case noTarget(detail: String)
    case safetyIndeterminate(detail: String)
}

func searchPasteTree<Node, NodeKey: Hashable, Context>(
    root: Node,
    maxDepth: Int,
    maxNodes: Int,
    nodeKey: (Node) -> NodeKey,
    inspect: (Node) -> PasteTreeNodeObservation<Context>,
    readChildren: (Node) -> [PasteTreeChildRead<Node>]
) -> PasteTreeSearchResult<Context> {
    var queue: [(node: Node, depth: Int)] = [(root, 0)]
    var visited = 0
    var sample: [String] = []
    var eligibleCandidates = Set<NodeKey>()
    var unsafeCandidates = Set<NodeKey>()
    var seenNodes = Set<NodeKey>()
    var traversalIndeterminateDetail: String?
    var depthWasTruncated = false

    while !queue.isEmpty, visited < maxNodes {
        let item = queue.removeFirst()
        let key = nodeKey(item.node)
        guard seenNodes.insert(key).inserted else {
            continue
        }
        visited += 1

        let observation = inspect(item.node)
        let focused: Bool
        switch resolvePasteDescendantFocus(
            focused: observation.focused,
            candidateCouldReceivePaste:
                observation.resolution.couldReceivePaste
        ) {
        case .focused(let value):
            focused = value
        case .safetyIndeterminate(let detail):
            return .safetyIndeterminate(detail: detail)
        }

        switch observation.resolution {
        case .safetyIndeterminate(let detail):
            if focused {
                return .safetyIndeterminate(
                    detail: "focusedDescendant \(detail)"
                )
            }
            if sample.count < 8 {
                sample.append("d\(item.depth):\(detail)")
            }
        case .candidate(
            let context,
            let kind,
            let candidateSample,
            let foundDetail
        ):
            if focused {
                let prefix = kind == .unsafe
                    ? "foundFocusedUnsafe"
                    : "foundFocused"
                return .found(
                    context,
                    detail: "\(prefix) depth=\(item.depth) visited=\(visited) \(foundDetail)"
                )
            }
            switch kind {
            case .eligible:
                eligibleCandidates.insert(key)
                if sample.count < 8 {
                    sample.append(
                        "d\(item.depth):eligible \(candidateSample)"
                    )
                }
            case .unsafe:
                unsafeCandidates.insert(key)
                if sample.count < 8 {
                    sample.append(
                        "d\(item.depth):unsafe \(candidateSample)"
                    )
                }
            }
        case .noCandidate(let detail):
            if sample.count < 8 {
                sample.append("d\(item.depth):\(detail)")
            }
        }

        for children in readChildren(item.node) {
            if let failureDetail = children.failureDetail {
                traversalIndeterminateDetail = failureDetail
            }
            guard !children.nodes.isEmpty else {
                continue
            }
            if item.depth < maxDepth {
                queue.append(
                    contentsOf: children.nodes.map {
                        ($0, item.depth + 1)
                    }
                )
            } else {
                depthWasTruncated = true
            }
        }
    }

    let outcome: String
    if eligibleCandidates.isEmpty, unsafeCandidates.isEmpty {
        outcome = "notFound"
    } else {
        outcome = "noFocusedCandidate eligible=\(eligibleCandidates.count) unsafe=\(unsafeCandidates.count)"
    }
    if let traversalIndeterminateDetail {
        return .safetyIndeterminate(
            detail: "\(traversalIndeterminateDetail) \(outcome) visited=\(visited)"
        )
    }
    if pasteTreeSearchWasIncomplete(
        queuedNodeCount: queue.count,
        depthWasTruncated: depthWasTruncated
    ) {
        return .safetyIndeterminate(
            detail: "searchIncomplete queued=\(queue.count) depthTruncated=\(depthWasTruncated) visited=\(visited)"
        )
    }
    return .noTarget(
        detail: "\(outcome) visited=\(visited) sample=\(sample.joined(separator: ","))"
    )
}

func pasteTreeSearchWasIncomplete(
    queuedNodeCount: Int,
    depthWasTruncated: Bool
) -> Bool {
    queuedNodeCount > 0 || depthWasTruncated
}

func resolvePasteDescendantFocus(
    focused: AXScalarAttributeResult<Bool>,
    candidateCouldReceivePaste: Bool
) -> PasteDescendantFocusResolution {
    if candidateCouldReceivePaste,
       focused.value == nil {
        return .safetyIndeterminate(
            "focusedState=\(focused.detail)"
        )
    }
    return .focused(focused.value ?? false)
}

enum PasteTargetSafetyAttributeResolution: Equatable {
    case resolved(PasteTargetSafetyAttributes)
    case retry(String)
}

func resolvePasteTargetSafetyAttributes(
    role: AXScalarAttributeResult<String>,
    subrole: AXScalarAttributeResult<String>,
    enabled: AXScalarAttributeResult<Bool>,
    protectedContent: AXScalarAttributeResult<Bool>
) -> PasteTargetSafetyAttributeResolution {
    guard role.failureDetail == nil, let roleValue = role.value else {
        return .retry("role=\(role.detail)")
    }
    guard subrole.failureDetail == nil else {
        return .retry("subrole=\(subrole.detail)")
    }
    guard enabled.failureDetail == nil else {
        return .retry("enabled=\(enabled.detail)")
    }
    guard protectedContent.failureDetail == nil else {
        return .retry("protected=\(protectedContent.detail)")
    }
    return .resolved(
        PasteTargetSafetyAttributes(
            role: roleValue,
            subrole: subrole.value ?? "",
            enabled: enabled.value ?? true,
            isProtectedContent: protectedContent.value ?? false
        )
    )
}

extension AXError {
    var isBenignMissingAttribute: Bool {
        self == .attributeUnsupported || self == .noValue
    }
}

private func copyElementAttributeResult(
    _ element: AXUIElement,
    _ attribute: CFString
) -> AXElementAttributeResult {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute, &value)
    guard error == .success else {
        return AXElementAttributeResult(element: nil, error: error, wrongType: false)
    }
    guard let value, CFGetTypeID(value) == AXUIElementGetTypeID() else {
        return AXElementAttributeResult(element: nil, error: error, wrongType: true)
    }
    return AXElementAttributeResult(element: (value as! AXUIElement), error: error, wrongType: false)
}

private func copyElementArrayAttributeResult(
    _ element: AXUIElement,
    _ attribute: CFString
) -> AXElementArrayAttributeResult {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute, &value)
    guard error == .success else {
        return AXElementArrayAttributeResult(elements: [], error: error, wrongType: false)
    }
    guard let rawItems = value as? [AnyObject] else {
        return AXElementArrayAttributeResult(elements: [], error: error, wrongType: true)
    }
    let elements = rawItems.compactMap { item -> AXUIElement? in
        let cfItem = item as CFTypeRef
        guard CFGetTypeID(cfItem) == AXUIElementGetTypeID() else {
            return nil
        }
        return (item as! AXUIElement)
    }
    return AXElementArrayAttributeResult(
        elements: elements,
        error: error,
        wrongType: elements.count != rawItems.count
    )
}

private func describe(_ result: AXElementAttributeResult) -> String {
    if result.element != nil {
        return "ok"
    }
    if result.wrongType {
        return "wrongType"
    }
    return "error=\(result.error.rawValue)"
}

private func copyElementAttribute(_ element: AXUIElement, _ attribute: CFString) -> AXUIElement? {
    copyElementAttributeResult(element, attribute).element
}

private func copyStringAttributeResult(
    _ element: AXUIElement,
    _ attribute: CFString
) -> AXScalarAttributeResult<String> {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute, &value)
    guard error == .success else {
        if error.isBenignMissingAttribute {
            return .missing
        }
        return .failed("error=\(error.rawValue)")
    }
    if let string = value as? String {
        return .value(string)
    }
    if let attributed = value as? NSAttributedString {
        return .value(attributed.string)
    }
    return .failed("wrongType")
}

private func copyStringAttribute(_ element: AXUIElement, _ attribute: CFString) -> String? {
    copyStringAttributeResult(element, attribute).value
}

private func copyBoolAttributeResult(
    _ element: AXUIElement,
    _ attribute: CFString
) -> AXScalarAttributeResult<Bool> {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute, &value)
    guard error == .success else {
        if error.isBenignMissingAttribute {
            return .missing
        }
        return .failed("error=\(error.rawValue)")
    }
    guard let bool = value as? Bool else {
        return .failed("wrongType")
    }
    return .value(bool)
}

private func copyBoolAttribute(_ element: AXUIElement, _ attribute: CFString) -> Bool? {
    copyBoolAttributeResult(element, attribute).value
}

private func frame(of element: AXUIElement) -> CGRect? {
    guard let positionValue = copyAXValue(element, kAXPositionAttribute as CFString),
          let sizeValue = copyAXValue(element, kAXSizeAttribute as CFString) else {
        return nil
    }
    var point = CGPoint.zero
    var size = CGSize.zero
    AXValueGetValue(positionValue, .cgPoint, &point)
    AXValueGetValue(sizeValue, .cgSize, &size)
    return CGRect(origin: point, size: size)
}

private func copyAXValue(_ element: AXUIElement, _ attribute: CFString) -> AXValue? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success else {
        return nil
    }
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else {
        return nil
    }
    return (value as! AXValue)
}

private func copyRangeAttribute(_ element: AXUIElement, _ attribute: CFString) -> NSRange? {
    guard let value = copyAXValue(element, attribute),
          AXValueGetType(value) == .cfRange else {
        return nil
    }
    var range = CFRange()
    guard AXValueGetValue(value, .cfRange, &range),
          range.location >= 0,
          range.length >= 0 else {
        return nil
    }
    return NSRange(location: range.location, length: range.length)
}

private func copyStringForRange(
    _ element: AXUIElement,
    range: NSRange
) -> String? {
    var cfRange = CFRange(location: range.location, length: range.length)
    guard let rangeValue = AXValueCreate(.cfRange, &cfRange) else {
        return nil
    }
    var value: CFTypeRef?
    guard AXUIElementCopyParameterizedAttributeValue(
        element,
        kAXStringForRangeParameterizedAttribute as CFString,
        rangeValue,
        &value
    ) == .success else {
        return nil
    }
    if let string = value as? String {
        return string
    }
    return (value as? NSAttributedString)?.string
}

private func canSetAttribute(_ element: AXUIElement, _ attribute: CFString) -> Bool {
    var settable = DarwinBoolean(false)
    return AXUIElementIsAttributeSettable(element, attribute, &settable) == .success
        && settable.boolValue
}

private func hasReadableAttribute(_ element: AXUIElement, _ attribute: CFString) -> Bool {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, attribute, &value) == .success
}

struct PasteboardRestoreToken {
    fileprivate let previous: PasteboardSnapshot
    fileprivate let writtenChangeCount: Int
    fileprivate let text: String
    fileprivate let attemptID: UUID

    fileprivate init(
        previous: PasteboardSnapshot,
        writtenChangeCount: Int,
        text: String,
        attemptID: UUID
    ) {
        self.previous = previous
        self.writtenChangeCount = writtenChangeCount
        self.text = text
        self.attemptID = attemptID
    }

#if DEBUG
    init(testIdentifier: UUID = UUID()) {
        previous = PasteboardSnapshot(items: [])
        writtenChangeCount = -1
        text = ""
        attemptID = testIdentifier
    }
#endif
}

fileprivate struct PasteboardSnapshot {
    private let items: [NSPasteboardItem]

#if DEBUG
    fileprivate init(items: [NSPasteboardItem]) {
        self.items = items
    }
#endif

    init(pasteboard: NSPasteboard) {
        items = pasteboard.pasteboardItems?.compactMap { item in
            let clone = NSPasteboardItem()
            for type in item.types {
                if let data = item.data(forType: type) {
                    _ = clone.setData(data, forType: type)
                } else if let string = item.string(forType: type) {
                    _ = clone.setString(string, forType: type)
                }
            }
            return clone.types.isEmpty ? nil : clone
        } ?? []
    }

    @discardableResult
    func restore(
        to pasteboard: NSPasteboard,
        using restoreItems: (
            NSPasteboard,
            [NSPasteboardItem]
        ) -> Bool
    ) -> Bool {
        restoreItems(pasteboard, items)
    }
}

enum KeyboardLayoutKeyCodeResolver {
    static func keyCode(for character: Character) -> CGKeyCode? {
        guard let inputSource = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
              let rawLayoutData = TISGetInputSourceProperty(
                inputSource,
                kTISPropertyUnicodeKeyLayoutData
              ) else {
            return nil
        }

        let layoutData = unsafeBitCast(rawLayoutData, to: CFData.self)
        guard let bytes = CFDataGetBytePtr(layoutData) else {
            return nil
        }
        let keyboardLayout = UnsafeRawPointer(bytes)
            .assumingMemoryBound(to: UCKeyboardLayout.self)
        let keyboardType = UInt32(LMGetKbdType())

        return keyCode(
            for: character,
            modifierState: UInt32(cmdKey >> 8)
        ) { rawKeyCode, modifierState in
            var deadKeyState: UInt32 = 0
            var translated = [UniChar](repeating: 0, count: 4)
            var translatedCount = 0
            let status = UCKeyTranslate(
                keyboardLayout,
                rawKeyCode,
                UInt16(kUCKeyActionDisplay),
                modifierState,
                keyboardType,
                OptionBits(kUCKeyTranslateNoDeadKeysBit),
                &deadKeyState,
                translated.count,
                &translatedCount,
                &translated
            )
            guard status == noErr, translatedCount == 1 else {
                return nil
            }
            return translated[0]
        }
    }

    static func keyCode(
        for character: Character,
        modifierState: UInt32,
        translating translate: (UInt16, UInt32) -> UniChar?
    ) -> CGKeyCode? {
        guard let scalar = String(character).lowercased().utf16.first else {
            return nil
        }
        for rawKeyCode in UInt16(0)...UInt16(127)
        where translate(rawKeyCode, modifierState) == scalar {
            return CGKeyCode(rawKeyCode)
        }
        return nil
    }
}

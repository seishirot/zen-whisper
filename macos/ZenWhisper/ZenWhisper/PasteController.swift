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

struct PasteTargetProbe {
    let context: PasteTargetContext?
    let detail: String

    var snapshot: PasteTargetSnapshot? {
        context?.snapshot
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

enum PasteboardWriteResult {
    case success(PasteboardRestoreToken)
    case writeFailed(restoreSucceeded: Bool)
}

enum PasteboardRestoreResult: Equatable {
    case restored
    case ownershipLost
    case failed
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
        if let expectedValue, after.value == expectedValue {
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
    func textState(
        for context: PasteTargetContext,
        precedingUTF16Length: Int
    ) -> PasteTextState
    func isFocused(_ approved: PasteTargetContext) -> Bool
    func makePasteKeyEventPair() -> PasteKeyEventPair?
    func makeReturnKeyEventPair() -> PasteKeyEventPair?
    func postKeyDown(_ pair: PasteKeyEventPair)
    func postKeyUp(_ pair: PasteKeyEventPair)
}

final class PasteController {
    private static let pasteAttemptPasteboardType = NSPasteboard.PasteboardType(
        "app.zen-whisper.paste-attempt"
    )

    func isAccessibilityTrusted() -> Bool {
        AXIsProcessTrusted()
    }

    func prepareAutoPaste(
        _ text: String,
        attemptID: UUID = UUID(),
        preservingBaseFrom retainedToken: PasteboardRestoreToken? = nil
    ) -> PasteboardWriteResult {
        let pasteboard = NSPasteboard.general
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
            return .writeFailed(restoreSucceeded: true)
        }
        pasteboard.clearContents()
        guard pasteboard.writeObjects([item]) else {
            return .writeFailed(restoreSucceeded: previous.restore(to: pasteboard))
        }
        return .success(PasteboardRestoreToken(
            previous: previous,
            writtenChangeCount: pasteboard.changeCount,
            text: text,
            attemptID: attemptID
        ))
    }

    @discardableResult
    private func restore(_ token: PasteboardRestoreToken) -> Bool {
        token.previous.restore(to: NSPasteboard.general)
    }

    func restoreIfOwned(_ token: PasteboardRestoreToken) -> PasteboardRestoreResult {
        guard ownsPasteboard(token) else {
            return .ownershipLost
        }
        return restore(token) ? .restored : .failed
    }

    func ownsPasteboard(_ token: PasteboardRestoreToken) -> Bool {
        let pasteboard = NSPasteboard.general
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
        let systemWide = AXUIElementCreateSystemWide()
        let systemFocused = copyElementAttributeResult(systemWide, kAXFocusedUIElementAttribute as CFString)
        detail.append("systemFocused=\(describe(systemFocused))")
        if let focused = systemFocused.element {
            let result = snapshotWithDetail(from: focused)
            detail.append("systemSnapshot=\(result.detail)")
            if let context = result.context,
               isEligible(context.snapshot) || isUnsafeForClipboard(context.snapshot) {
                return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
            }
        }

        let focusedApp = copyElementAttributeResult(systemWide, kAXFocusedApplicationAttribute as CFString)
        detail.append("systemFocusedApp=\(describe(focusedApp))")
        if let axApp = focusedApp.element {
            var focusedAppPID: pid_t = 0
            let pidStatus = AXUIElementGetPid(axApp, &focusedAppPID)
            detail.append("focusedAppPidStatus=\(pidStatus.rawValue)")
            if pidStatus == .success, focusedAppPID != currentPID {
                detail.append("focusedApp=\(redactedAppIdentityDescription(pid: focusedAppPID, bundleIdentifier: "<ax-focused>"))")
                let focused = copyElementAttributeResult(axApp, kAXFocusedUIElementAttribute as CFString)
                detail.append("focusedAppElement=\(describe(focused))")
                if let focusedElement = focused.element {
                    let result = snapshotWithDetail(from: focusedElement, pid: focusedAppPID)
                    detail.append("focusedAppSnapshot=\(result.detail)")
                    if let context = result.context,
                       isEligible(context.snapshot) || isUnsafeForClipboard(context.snapshot) {
                        return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
                    }
                }
                if let context = snapshotFromWindowDescendant(
                    axApp: axApp,
                    pid: focusedAppPID,
                    label: "focusedApp",
                    searchWindowDescendants: searchWindowDescendants,
                    detail: &detail
                ) {
                    return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
                }
            }
        }

        guard let app = NSWorkspace.shared.frontmostApplication else {
            detail.append("frontmost=nil")
            return PasteTargetProbe(context: nil, detail: detail.joined(separator: " "))
        }
        detail.append(
            "frontmost=\(redactedAppIdentityDescription(pid: app.processIdentifier, bundleIdentifier: app.bundleIdentifier ?? "<nil>"))"
        )
        guard app.processIdentifier != NSRunningApplication.current.processIdentifier else {
            detail.append("frontmost=current")
            return PasteTargetProbe(context: nil, detail: detail.joined(separator: " "))
        }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        let focused = copyElementAttributeResult(axApp, kAXFocusedUIElementAttribute as CFString)
        detail.append("frontmostElement=\(describe(focused))")
        if let focusedElement = focused.element {
            let result = snapshotWithDetail(from: focusedElement, pid: app.processIdentifier)
            detail.append("frontmostSnapshot=\(result.detail)")
            if let context = result.context,
               isEligible(context.snapshot) || isUnsafeForClipboard(context.snapshot) {
                return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
            }
        }
        if let context = snapshotFromWindowDescendant(
            axApp: axApp,
            pid: app.processIdentifier,
            label: "frontmost",
            searchWindowDescendants: searchWindowDescendants,
            detail: &detail
        ) {
            return PasteTargetProbe(context: context, detail: detail.joined(separator: " "))
        }
        return PasteTargetProbe(context: nil, detail: detail.joined(separator: " "))
    }

    private func snapshotFromWindowDescendant(
        axApp: AXUIElement,
        pid: pid_t,
        label: String,
        searchWindowDescendants: Bool,
        detail: inout [String]
    ) -> PasteTargetContext? {
        let focusedWindow = copyElementAttributeResult(axApp, kAXFocusedWindowAttribute as CFString)
        detail.append("\(label)FocusedWindow=\(describe(focusedWindow))")
        if searchWindowDescendants, let context = searchWindow(focusedWindow.element, pid: pid, label: label, detail: &detail) {
            return context
        }

        let mainWindow = copyElementAttributeResult(axApp, kAXMainWindowAttribute as CFString)
        detail.append("\(label)MainWindow=\(describe(mainWindow))")
        if searchWindowDescendants, let context = searchWindow(mainWindow.element, pid: pid, label: label, detail: &detail) {
            return context
        }
        return nil
    }

    private func searchWindow(
        _ window: AXUIElement?,
        pid: pid_t,
        label: String,
        detail: inout [String]
    ) -> PasteTargetContext? {
        guard let window else {
            return nil
        }
        let result = snapshotEditableDescendant(in: window, pid: pid)
        detail.append("\(label)WindowSearch=\(result.detail)")
        return result.context
    }

    private func snapshotEditableDescendant(
        in root: AXUIElement,
        pid: pid_t,
        maxDepth: Int = 10,
        maxNodes: Int = 300
    ) -> (context: PasteTargetContext?, detail: String) {
        var queue: [(element: AXUIElement, depth: Int)] = [(root, 0)]
        var visited = 0
        var sample: [String] = []
        var candidates: [PasteTargetContext] = []
        var unsafeCandidates: [PasteTargetContext] = []
        var seenNodes = Set<CFHashCode>()

        while !queue.isEmpty, visited < maxNodes {
            let item = queue.removeFirst()
            let nodeKey = CFHash(item.element)
            guard !seenNodes.contains(nodeKey) else {
                continue
            }
            seenNodes.insert(nodeKey)
            visited += 1

            let result = snapshotWithDetail(from: item.element, pid: pid, discovery: "windowDescendant")
            if let context = result.context {
                let snapshot = context.snapshot
                let focused =
                    copyBoolAttribute(item.element, kAXFocusedAttribute as CFString)
                    ?? false
                if isUnsafeForClipboard(snapshot) {
                    if focused {
                        return (
                            context,
                            "foundFocusedUnsafe depth=\(item.depth) visited=\(visited) \(snapshot.redactedDescription)"
                        )
                    }
                    if !unsafeCandidates.contains(where: {
                        $0.snapshot == snapshot
                    }) {
                        unsafeCandidates.append(context)
                    }
                    if sample.count < 8 {
                        sample.append(
                            "d\(item.depth):unsafe role=\(snapshot.role):subrole=\(snapshot.subrole)"
                        )
                    }
                } else if isEligible(snapshot) {
                    if focused {
                        return (context, "foundFocused depth=\(item.depth) visited=\(visited) \(snapshot.redactedDescription)")
                    }
                    if !candidates.contains(where: { $0.snapshot == snapshot }) {
                        candidates.append(context)
                    }
                    if sample.count < 8 {
                        sample.append("d\(item.depth):eligible role=\(snapshot.role):subrole=\(snapshot.subrole)")
                    }
                } else if sample.count < 8 {
                    sample.append("d\(item.depth):role=\(snapshot.role):subrole=\(snapshot.subrole):editable=\(snapshot.hasEditableValue)")
                }
            } else if sample.count < 8 {
                sample.append("d\(item.depth):\(result.detail)")
            }

            guard item.depth < maxDepth else {
                continue
            }
            for attribute in childAttributes {
                let children = copyElementArrayAttributeResult(item.element, attribute)
                guard !children.elements.isEmpty else {
                    continue
                }
                queue.append(contentsOf: children.elements.map { ($0, item.depth + 1) })
            }
        }

        if let unsafe = unsafeCandidates.first {
            return (
                unsafe,
                "foundUnsafeCandidate count=\(unsafeCandidates.count) visited=\(visited) \(unsafe.snapshot.redactedDescription)"
            )
        }
        if candidates.count == 1, let context = candidates.first {
            return (
                context,
                "foundUniqueEligible visited=\(visited) \(context.snapshot.redactedDescription)"
            )
        }
        let outcome = candidates.isEmpty ? "notFound" : "ambiguous eligible=\(candidates.count)"
        return (nil, "\(outcome) visited=\(visited) sample=\(sample.joined(separator: ","))")
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
    ) -> (context: PasteTargetContext?, detail: String) {
        var pid = fallbackPID ?? 0
        if fallbackPID == nil {
            let status = AXUIElementGetPid(focused, &pid)
            guard status == .success else {
                return (nil, "pidStatus=\(status.rawValue)")
            }
        }
        guard pid != NSRunningApplication.current.processIdentifier else {
            return (nil, "selfPid")
        }
        let role = copyStringAttribute(focused, kAXRoleAttribute as CFString) ?? ""
        let subrole = copyStringAttribute(focused, kAXSubroleAttribute as CFString) ?? ""
        let enabled = copyBoolAttribute(focused, kAXEnabledAttribute as CFString) ?? true
        guard enabled else {
            return (nil, "disabled role=\(role) subrole=\(subrole)")
        }
        let window = copyElementAttribute(focused, kAXWindowAttribute as CFString)
        let windowTitle = window.flatMap { copyStringAttribute($0, kAXTitleAttribute as CFString) } ?? ""
        let windowFrame = window.flatMap { frame(of: $0) } ?? .null
        let elementFrame = frame(of: focused) ?? .null
        let elementIdentifier = copyStringAttribute(focused, kAXIdentifierAttribute as CFString) ?? ""
        let searchable = searchableMetadata(for: focused, window: window, role: role, subrole: subrole)
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
        let isProtectedContent = copyBoolAttribute(focused, "AXProtectedContent" as CFString) ?? false
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
        return (
            PasteTargetContext(snapshot: snapshot, element: focused),
            snapshot.redactedDescription
        )
    }

    private func searchableMetadata(
        for focused: AXUIElement,
        window: AXUIElement?,
        role: String,
        subrole: String
    ) -> String {
        var pieces = [role, subrole]
        pieces.append(contentsOf: metadataPieces(from: focused, prefix: "element"))
        if let parent = copyElementAttribute(focused, kAXParentAttribute as CFString) {
            pieces.append(contentsOf: metadataPieces(from: parent, prefix: "parent"))
        }
        if let window {
            pieces.append(contentsOf: metadataPieces(from: window, prefix: "window"))
        }
        return pieces.joined(separator: " ")
    }

    private func metadataPieces(from element: AXUIElement, prefix: String) -> [String] {
        metadataAttributes.compactMap { attribute in
            copyStringAttribute(element, attribute).map { "\(prefix):\($0)" }
        }
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

    func isFocused(_ approved: PasteTargetContext) -> Bool {
        guard let approvedElement = approved.element,
              let current = snapshotFocusedTargetProbe().context,
              let currentElement = current.element else {
            return false
        }
        if CFEqual(approvedElement, currentElement) {
            return true
        }
        let approvedSnapshot = approved.snapshot
        let currentSnapshot = current.snapshot
        guard approvedSnapshot.pid == currentSnapshot.pid,
              !approvedSnapshot.elementIdentifier.isEmpty,
              approvedSnapshot.elementIdentifier == currentSnapshot.elementIdentifier else {
            return false
        }
        return approvedSnapshot.role == currentSnapshot.role
            && approvedSnapshot.subrole == currentSnapshot.subrole
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
    return AXElementArrayAttributeResult(elements: elements, error: error, wrongType: false)
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

private func copyStringAttribute(_ element: AXUIElement, _ attribute: CFString) -> String? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success else {
        return nil
    }
    if let string = value as? String {
        return string
    }
    return (value as? NSAttributedString)?.string
}

private func copyBoolAttribute(_ element: AXUIElement, _ attribute: CFString) -> Bool? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success else {
        return nil
    }
    return value as? Bool
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
    func restore(to pasteboard: NSPasteboard) -> Bool {
        pasteboard.clearContents()
        guard !items.isEmpty else {
            return true
        }
        return pasteboard.writeObjects(items)
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

        return keyCode(for: character) { rawKeyCode in
            var deadKeyState: UInt32 = 0
            var translated = [UniChar](repeating: 0, count: 4)
            var translatedCount = 0
            let status = UCKeyTranslate(
                keyboardLayout,
                rawKeyCode,
                UInt16(kUCKeyActionDisplay),
                0,
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
        translating translate: (UInt16) -> UniChar?
    ) -> CGKeyCode? {
        guard let scalar = String(character).lowercased().utf16.first else {
            return nil
        }
        for rawKeyCode in UInt16(0)...UInt16(127)
        where translate(rawKeyCode) == scalar {
            return CGKeyCode(rawKeyCode)
        }
        return nil
    }
}

import AppKit
import ApplicationServices
import Carbon
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
    let isProtectedContent: Bool
    let searchableText: String
    let discovery: String

    var redactedDescription: String {
        "pid=\(pid) bundle=\(bundleIdentifier) role=\(role) subrole=\(subrole) editable=\(hasEditableValue) protected=\(isProtectedContent) discovery=\(discovery) window=\(frameDescription(windowFrame)) element=\(frameDescription(elementFrame))"
    }

    private func frameDescription(_ frame: CGRect) -> String {
        if frame.isNull {
            return "null"
        }
        return "\(Int(frame.origin.x)),\(Int(frame.origin.y)),\(Int(frame.size.width)),\(Int(frame.size.height))"
    }
}

struct PasteTargetProbe {
    let snapshot: PasteTargetSnapshot?
    let detail: String
}

enum PasteDecision: Equatable {
    case paste
    case copyOnly(String)
    case skipCopy(String)
}

enum PasteboardWriteResult {
    case success(PasteboardRestoreToken)
    case writeFailed(restoreSucceeded: Bool)
}

final class PasteController {
    func isAccessibilityTrusted() -> Bool {
        AXIsProcessTrusted()
    }

    func copy(_ text: String, restoreAfter delay: TimeInterval? = nil) -> Bool {
        guard let restoreToken = copyForAutoPaste(text) else {
            return false
        }
        if let delay {
            scheduleRestore(restoreToken, after: delay)
        }
        return true
    }

    func copyForAutoPaste(_ text: String) -> PasteboardRestoreToken? {
        guard case .success(let token) = prepareAutoPaste(text) else {
            return nil
        }
        return token
    }

    func prepareAutoPaste(_ text: String) -> PasteboardWriteResult {
        let pasteboard = NSPasteboard.general
        let previous = PasteboardSnapshot(pasteboard: pasteboard)
        pasteboard.clearContents()
        guard pasteboard.setString(text, forType: .string) else {
            return .writeFailed(restoreSucceeded: previous.restore(to: pasteboard))
        }
        return .success(PasteboardRestoreToken(
            previous: previous,
            writtenChangeCount: pasteboard.changeCount,
            text: text
        ))
    }

    func scheduleRestore(
        _ token: PasteboardRestoreToken,
        after delay: TimeInterval,
        completion: @escaping (Bool) -> Void = { _ in }
    ) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            let current = NSPasteboard.general
            guard current.changeCount == token.writtenChangeCount,
                  current.string(forType: .string) == token.text else {
                completion(false)
                return
            }
            completion(self.restore(token))
        }
    }

    @discardableResult
    func restore(_ token: PasteboardRestoreToken) -> Bool {
        token.previous.restore(to: NSPasteboard.general)
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
            if let snapshot = result.snapshot {
                return PasteTargetProbe(snapshot: snapshot, detail: detail.joined(separator: " "))
            }
        }

        let focusedApp = copyElementAttributeResult(systemWide, kAXFocusedApplicationAttribute as CFString)
        detail.append("systemFocusedApp=\(describe(focusedApp))")
        if let axApp = focusedApp.element {
            var focusedAppPID: pid_t = 0
            let pidStatus = AXUIElementGetPid(axApp, &focusedAppPID)
            detail.append("focusedAppPidStatus=\(pidStatus.rawValue) focusedAppPid=\(focusedAppPID)")
            if pidStatus == .success, focusedAppPID != currentPID {
                let focused = copyElementAttributeResult(axApp, kAXFocusedUIElementAttribute as CFString)
                detail.append("focusedAppElement=\(describe(focused))")
                if let focusedElement = focused.element {
                    let result = snapshotWithDetail(from: focusedElement, pid: focusedAppPID)
                    detail.append("focusedAppSnapshot=\(result.detail)")
                    if let snapshot = result.snapshot {
                        return PasteTargetProbe(snapshot: snapshot, detail: detail.joined(separator: " "))
                    }
                }
                if let snapshot = snapshotFromWindowDescendant(
                    axApp: axApp,
                    pid: focusedAppPID,
                    label: "focusedApp",
                    searchWindowDescendants: searchWindowDescendants,
                    detail: &detail
                ) {
                    return PasteTargetProbe(snapshot: snapshot, detail: detail.joined(separator: " "))
                }
            }
        }

        guard let app = NSWorkspace.shared.frontmostApplication else {
            detail.append("frontmost=nil")
            return PasteTargetProbe(snapshot: nil, detail: detail.joined(separator: " "))
        }
        detail.append("frontmostPid=\(app.processIdentifier) frontmostBundle=\(app.bundleIdentifier ?? "<nil>")")
        guard app.processIdentifier != NSRunningApplication.current.processIdentifier else {
            detail.append("frontmost=current")
            return PasteTargetProbe(snapshot: nil, detail: detail.joined(separator: " "))
        }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        let focused = copyElementAttributeResult(axApp, kAXFocusedUIElementAttribute as CFString)
        detail.append("frontmostElement=\(describe(focused))")
        if let focusedElement = focused.element {
            let result = snapshotWithDetail(from: focusedElement, pid: app.processIdentifier)
            detail.append("frontmostSnapshot=\(result.detail)")
            if let snapshot = result.snapshot {
                return PasteTargetProbe(snapshot: snapshot, detail: detail.joined(separator: " "))
            }
        }
        if let snapshot = snapshotFromWindowDescendant(
            axApp: axApp,
            pid: app.processIdentifier,
            label: "frontmost",
            searchWindowDescendants: searchWindowDescendants,
            detail: &detail
        ) {
            return PasteTargetProbe(snapshot: snapshot, detail: detail.joined(separator: " "))
        }
        return PasteTargetProbe(snapshot: nil, detail: detail.joined(separator: " "))
    }

    private func snapshotFromWindowDescendant(
        axApp: AXUIElement,
        pid: pid_t,
        label: String,
        searchWindowDescendants: Bool,
        detail: inout [String]
    ) -> PasteTargetSnapshot? {
        let focusedWindow = copyElementAttributeResult(axApp, kAXFocusedWindowAttribute as CFString)
        detail.append("\(label)FocusedWindow=\(describe(focusedWindow))")
        if searchWindowDescendants, let snapshot = searchWindow(focusedWindow.element, pid: pid, label: label, detail: &detail) {
            return snapshot
        }

        let mainWindow = copyElementAttributeResult(axApp, kAXMainWindowAttribute as CFString)
        detail.append("\(label)MainWindow=\(describe(mainWindow))")
        if searchWindowDescendants, let snapshot = searchWindow(mainWindow.element, pid: pid, label: label, detail: &detail) {
            return snapshot
        }
        return nil
    }

    private func searchWindow(
        _ window: AXUIElement?,
        pid: pid_t,
        label: String,
        detail: inout [String]
    ) -> PasteTargetSnapshot? {
        guard let window else {
            return nil
        }
        let result = snapshotEditableDescendant(in: window, pid: pid)
        detail.append("\(label)WindowSearch=\(result.detail)")
        return result.snapshot
    }

    private func snapshotEditableDescendant(
        in root: AXUIElement,
        pid: pid_t,
        maxDepth: Int = 8,
        maxNodes: Int = 180
    ) -> (snapshot: PasteTargetSnapshot?, detail: String) {
        var queue: [(element: AXUIElement, depth: Int)] = [(root, 0)]
        var visited = 0
        var sample: [String] = []
        var candidates: [PasteTargetSnapshot] = []
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
            if let snapshot = result.snapshot {
                if isEligible(snapshot) {
                    let focused = copyBoolAttribute(item.element, kAXFocusedAttribute as CFString) ?? false
                    if focused {
                        return (snapshot, "foundFocused depth=\(item.depth) visited=\(visited) \(snapshot.redactedDescription)")
                    }
                    if !candidates.contains(snapshot) {
                        candidates.append(snapshot)
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

        let outcome = candidates.isEmpty ? "notFound" : "unverified eligible=\(candidates.count)"
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
    ) -> (snapshot: PasteTargetSnapshot?, detail: String) {
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
        let hasEditableValue = canSetValue(focused)
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
            isProtectedContent: isProtectedContent,
            searchableText: searchable,
            discovery: discovery
        )
        return (snapshot, snapshot.redactedDescription)
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

    func decide(
        start: PasteTargetSnapshot?,
        stop: PasteTargetSnapshot?,
        current: PasteTargetSnapshot?
    ) -> PasteDecision {
        guard [start, stop, current].compactMap({ $0 }).allSatisfy({ !isUnsafeForClipboard($0) }) else {
            return .skipCopy("target is unsafe")
        }
        guard let stop else {
            if start == nil {
                return .copyOnly("missing recording start AX target")
            }
            return .copyOnly("missing recording stop AX target")
        }
        guard let start else {
            return .copyOnly("missing recording start AX target")
        }
        guard sameTarget(start, stop) else {
            return .copyOnly("target changed during recording")
        }
        guard let current else {
            return .copyOnly("missing current AX target")
        }
        guard isEligible(start), isEligible(stop), isEligible(current) else {
            return .copyOnly("target is not editable")
        }
        guard sameTarget(stop, current) else {
            return .copyOnly("target changed")
        }
        return .paste
    }

    func paste(to pid: pid_t) -> Bool {
        guard canCreatePasteEvents() else {
            return false
        }
        return postPasteEvents(to: pid)
    }

    func pressReturn(to pid: pid_t) -> Bool {
        guard canCreateKeyEvents(virtualKey: CGKeyCode(kVK_Return)) else {
            return false
        }
        return postKeyEvents(to: pid, virtualKey: CGKeyCode(kVK_Return), flags: [])
    }

    func canCreatePasteEvents() -> Bool {
        canCreateKeyEvents(virtualKey: 9)
    }

    private func canCreateKeyEvents(virtualKey: CGKeyCode) -> Bool {
        guard CGPreflightPostEventAccess() else {
            return false
        }
        guard let source = CGEventSource(stateID: .hidSystemState) else {
            return false
        }
        let keyDown = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: true)
        let keyUp = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: false)
        return keyDown != nil && keyUp != nil
    }

    private func postPasteEvents(to pid: pid_t) -> Bool {
        postKeyEvents(to: pid, virtualKey: 9, flags: .maskCommand)
    }

    private func postKeyEvents(to pid: pid_t, virtualKey: CGKeyCode, flags: CGEventFlags) -> Bool {
        guard let source = CGEventSource(stateID: .hidSystemState),
              let keyDown = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: true),
              let keyUp = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: false) else {
            return false
        }
        keyDown.flags = flags
        keyUp.flags = flags
        keyDown.postToPid(pid)
        keyUp.postToPid(pid)
        return true
    }

    func isEligible(_ snapshot: PasteTargetSnapshot) -> Bool {
        guard snapshot.hasEditableValue else {
            return false
        }
        if isUnsafeForClipboard(snapshot) {
            return false
        }
        if snapshot.role == "AXWebArea" {
            return false
        }
        if snapshot.role == "AXTextField" || snapshot.role == "AXTextArea" || snapshot.role == "AXComboBox" {
            return true
        }
        if snapshot.subrole == "AXSearchField" {
            return true
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

    private func optionalFramesMatch(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
        if lhs.isNull || rhs.isNull {
            return true
        }
        let tolerance: CGFloat = 8
        return abs(lhs.origin.x - rhs.origin.x) <= tolerance
            && abs(lhs.origin.y - rhs.origin.y) <= tolerance
            && abs(lhs.size.width - rhs.size.width) <= tolerance
            && abs(lhs.size.height - rhs.size.height) <= tolerance
    }

    private func sameTarget(_ lhs: PasteTargetSnapshot, _ rhs: PasteTargetSnapshot) -> Bool {
        guard !lhs.elementFrame.isNull, !rhs.elementFrame.isNull else {
            return false
        }
        return lhs.pid == rhs.pid
            && identityMatches(lhs.bundleIdentifier, rhs.bundleIdentifier)
            && lhs.role == rhs.role
            && lhs.subrole == rhs.subrole
            && identityMatches(lhs.windowTitle, rhs.windowTitle)
            && identityMatches(lhs.elementIdentifier, rhs.elementIdentifier)
            && optionalFramesMatch(lhs.windowFrame, rhs.windowFrame)
            && optionalFramesMatch(lhs.elementFrame, rhs.elementFrame)
    }

    private func identityMatches(_ lhs: String, _ rhs: String) -> Bool {
        if lhs.isEmpty || rhs.isEmpty {
            return true
        }
        return lhs == rhs
    }
}

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
    return value as? String
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

private func canSetValue(_ element: AXUIElement) -> Bool {
    var settable = DarwinBoolean(false)
    return AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable) == .success
        && settable.boolValue
}

struct PasteboardRestoreToken {
    fileprivate let previous: PasteboardSnapshot
    fileprivate let writtenChangeCount: Int
    fileprivate let text: String
}

fileprivate struct PasteboardSnapshot {
    private let items: [NSPasteboardItem]

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

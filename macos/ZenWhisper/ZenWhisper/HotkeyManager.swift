import Carbon
import Foundation

enum HotkeyError: Error {
    case registrationFailed(OSStatus)
    case handlerInstallFailed(OSStatus)
    case unregistrationFailed(OSStatus)
    case handlerRemovalFailed(OSStatus)
}

final class HotkeyManager {
    static let primarySignature = OSType(0x5A575350)
    static let submitSignature = OSType(0x5A575353)

    private let signature: OSType
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private var action: (() -> Void)?
    private var registeredShortcut: HotkeyShortcut?
    private var registeredHotKeyID: EventHotKeyID?
    private var nextHotKeyID: UInt32 = 1

    var hasActiveRegistration: Bool {
        hotKeyRef != nil
    }

    var activeShortcut: HotkeyShortcut? {
        hotKeyRef == nil ? nil : registeredShortcut
    }

    init(signature: OSType = HotkeyManager.primarySignature) {
        self.signature = signature
    }

    func register(shortcut: HotkeyShortcut, action: @escaping () -> Void) throws {
        try installHandlerIfNeeded()

        var newHotKeyRef: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: signature, id: nextHotKeyID)
        let status = RegisterEventHotKey(
            shortcut.keyCode,
            shortcut.modifiers,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &newHotKeyRef
        )
        if status != noErr {
            cleanupHandlerIfIdle()
            throw HotkeyError.registrationFailed(status)
        }

        let oldHotKeyRef = hotKeyRef
        hotKeyRef = newHotKeyRef
        registeredHotKeyID = hotKeyID
        registeredShortcut = shortcut
        self.action = action
        nextHotKeyID = nextHotKeyID == UInt32.max ? 1 : nextHotKeyID + 1

        if let oldHotKeyRef {
            UnregisterEventHotKey(oldHotKeyRef)
        }
    }

    func suspend() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
            registeredHotKeyID = nil
        }
    }

    func suspendReportingFailure() throws {
        guard let hotKeyRef else {
            return
        }
        let status = UnregisterEventHotKey(hotKeyRef)
        guard status == noErr else {
            throw HotkeyError.unregistrationFailed(status)
        }
        self.hotKeyRef = nil
        registeredHotKeyID = nil
    }

    func resume() throws {
        guard hotKeyRef == nil,
              let registeredShortcut,
              let action else {
            return
        }
        try register(shortcut: registeredShortcut, action: action)
    }

    func unregister() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
        }
        if let handlerRef {
            RemoveEventHandler(handlerRef)
        }
        hotKeyRef = nil
        handlerRef = nil
        action = nil
        registeredShortcut = nil
        registeredHotKeyID = nil
    }

    func unregisterReportingFailure() throws {
        if let hotKeyRef {
            let status = UnregisterEventHotKey(hotKeyRef)
            guard status == noErr else {
                throw HotkeyError.unregistrationFailed(status)
            }
            self.hotKeyRef = nil
            registeredHotKeyID = nil
        }
        if let handlerRef {
            let status = RemoveEventHandler(handlerRef)
            guard status == noErr else {
                throw HotkeyError.handlerRemovalFailed(status)
            }
            self.handlerRef = nil
        }
        action = nil
        registeredShortcut = nil
        registeredHotKeyID = nil
    }

    private func installHandlerIfNeeded() throws {
        guard handlerRef == nil else {
            return
        }

        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: OSType(kEventHotKeyPressed))
        let selfPointer = Unmanaged.passUnretained(self).toOpaque()
        let handlerStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, userData in
                guard let event, let userData else {
                    return OSStatus(eventNotHandledErr)
                }
                let manager = Unmanaged<HotkeyManager>.fromOpaque(userData).takeUnretainedValue()
                guard manager.handles(event: event) else {
                    return OSStatus(eventNotHandledErr)
                }
                manager.action?()
                return noErr
            },
            1,
            &eventType,
            selfPointer,
            &handlerRef
        )
        if handlerStatus != noErr {
            handlerRef = nil
            throw HotkeyError.handlerInstallFailed(handlerStatus)
        }
    }

    private func cleanupHandlerIfIdle() {
        guard hotKeyRef == nil,
              registeredShortcut == nil,
              let handlerRef else {
            return
        }
        RemoveEventHandler(handlerRef)
        self.handlerRef = nil
        action = nil
    }

    private func handles(event: EventRef) -> Bool {
        guard let registeredHotKeyID else {
            return false
        }
        var eventHotKeyID = EventHotKeyID()
        let status = GetEventParameter(
            event,
            EventParamName(kEventParamDirectObject),
            EventParamType(typeEventHotKeyID),
            nil,
            MemoryLayout<EventHotKeyID>.size,
            nil,
            &eventHotKeyID
        )
        guard status == noErr else {
            return false
        }
        return eventHotKeyID.signature == registeredHotKeyID.signature
            && eventHotKeyID.id == registeredHotKeyID.id
    }
}

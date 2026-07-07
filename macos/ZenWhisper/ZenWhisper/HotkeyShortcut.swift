import AppKit
import Carbon
import Foundation

struct HotkeyShortcut: Codable, Equatable {
    let keyCode: UInt32
    let modifiers: UInt32
    let keyLabel: String

    static let shiftSpace = HotkeyShortcut(
        keyCode: UInt32(kVK_Space),
        modifiers: UInt32(shiftKey),
        keyLabel: "Space"
    )
    static let controlOptionCommandSpace = HotkeyShortcut(
        keyCode: UInt32(kVK_Space),
        modifiers: UInt32(controlKey | optionKey | cmdKey),
        keyLabel: "Space"
    )
    static let controlOptionCommandReturn = HotkeyShortcut(
        keyCode: UInt32(kVK_Return),
        modifiers: UInt32(controlKey | optionKey | cmdKey),
        keyLabel: "Return"
    )
    static let presets = [shiftSpace, controlOptionCommandSpace]
    static let submitPresets = [controlOptionCommandReturn]

    private static let modifierMask = UInt32(shiftKey | controlKey | optionKey | cmdKey)
    private static let strongModifierMask = UInt32(controlKey | optionKey | cmdKey)

    var storageValue: String {
        if self == Self.shiftSpace {
            return "shift+space"
        }
        if self == Self.controlOptionCommandSpace {
            return "ctrl+option+cmd+space"
        }
        if self == Self.controlOptionCommandReturn {
            return "ctrl+option+cmd+return"
        }
        return "keycode:\(keyCode):\(modifiers & Self.modifierMask):\(keyLabel)"
    }

    var label: String {
        let modifierParts = modifierLabels(for: modifiers)
        guard !modifierParts.isEmpty else {
            return keyLabel
        }
        return (modifierParts + [keyLabel]).joined(separator: "+")
    }

    var isUsable: Bool {
        modifiers & Self.modifierMask != 0
            && (modifiers & Self.strongModifierMask != 0 || self == Self.shiftSpace)
    }

    init(keyCode: UInt32, modifiers: UInt32, keyLabel: String) {
        self.keyCode = keyCode
        self.modifiers = modifiers & Self.modifierMask
        self.keyLabel = keyLabel.isEmpty ? Self.keyLabel(forKeyCode: keyCode) : keyLabel
    }

    static func == (lhs: HotkeyShortcut, rhs: HotkeyShortcut) -> Bool {
        lhs.keyCode == rhs.keyCode && lhs.modifiers == rhs.modifiers
    }

    static func fromStorageValue(_ rawValue: String?) -> HotkeyShortcut {
        guard let rawValue, !rawValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return .shiftSpace
        }
        return parseStorageValue(rawValue)
            ?? parseComboString(rawValue)
            ?? .shiftSpace
    }

    static func optionalFromStorageValue(_ rawValue: String?) -> HotkeyShortcut? {
        guard let rawValue else {
            return nil
        }
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.lowercased() != "off" else {
            return nil
        }
        return parseStorageValue(trimmed) ?? parseComboString(trimmed)
    }

    static func fromEvent(_ event: NSEvent) -> HotkeyShortcut? {
        let modifiers = carbonModifiers(from: event.modifierFlags)
        let shortcut = HotkeyShortcut(
            keyCode: UInt32(event.keyCode),
            modifiers: modifiers,
            keyLabel: keyLabel(for: event)
        )
        return shortcut.isUsable ? shortcut : nil
    }

    static func parseComboString(_ rawValue: String) -> HotkeyShortcut? {
        let parts = rawValue
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .replacingOccurrences(of: " ", with: "")
            .split(separator: "+")
            .map(String.init)
        guard parts.count >= 2 else {
            return nil
        }

        var modifiers: UInt32 = 0
        var keyParts: [String] = []
        for part in parts {
            switch part {
            case "shift":
                modifiers |= UInt32(shiftKey)
            case "ctrl", "control":
                modifiers |= UInt32(controlKey)
            case "option", "opt", "alt":
                modifiers |= UInt32(optionKey)
            case "cmd", "command", "meta":
                modifiers |= UInt32(cmdKey)
            default:
                keyParts.append(part)
            }
        }

        guard keyParts.count == 1,
              let keyCode = keyCode(for: keyParts[0]) else {
            return nil
        }
        let shortcut = HotkeyShortcut(
            keyCode: keyCode,
            modifiers: modifiers,
            keyLabel: keyLabel(forKeyName: keyParts[0], keyCode: keyCode)
        )
        return shortcut.isUsable ? shortcut : nil
    }

    private static func parseStorageValue(_ rawValue: String) -> HotkeyShortcut? {
        let parts = rawValue.split(separator: ":", maxSplits: 3, omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 4,
              parts[0] == "keycode",
              let keyCode = UInt32(parts[1]),
              let modifiers = UInt32(parts[2]) else {
            return nil
        }
        let shortcut = HotkeyShortcut(keyCode: keyCode, modifiers: modifiers, keyLabel: parts[3])
        return shortcut.isUsable ? shortcut : nil
    }

    private static func carbonModifiers(from flags: NSEvent.ModifierFlags) -> UInt32 {
        var modifiers: UInt32 = 0
        if flags.contains(.shift) {
            modifiers |= UInt32(shiftKey)
        }
        if flags.contains(.control) {
            modifiers |= UInt32(controlKey)
        }
        if flags.contains(.option) {
            modifiers |= UInt32(optionKey)
        }
        if flags.contains(.command) {
            modifiers |= UInt32(cmdKey)
        }
        return modifiers
    }

    private func modifierLabels(for modifiers: UInt32) -> [String] {
        var labels: [String] = []
        if modifiers & UInt32(controlKey) != 0 {
            labels.append("Ctrl")
        }
        if modifiers & UInt32(optionKey) != 0 {
            labels.append("Option")
        }
        if modifiers & UInt32(shiftKey) != 0 {
            labels.append("Shift")
        }
        if modifiers & UInt32(cmdKey) != 0 {
            labels.append("Cmd")
        }
        return labels
    }

    private static func keyLabel(for event: NSEvent) -> String {
        if let label = specialKeyLabels[UInt32(event.keyCode)] {
            return label
        }
        if let characters = event.charactersIgnoringModifiers,
           let first = characters.unicodeScalars.first,
           !CharacterSet.controlCharacters.contains(first) {
            return String(characters.prefix(1)).uppercased()
        }
        return keyLabel(forKeyCode: UInt32(event.keyCode))
    }

    private static func keyLabel(forKeyName keyName: String, keyCode: UInt32) -> String {
        if let label = namedKeyLabels[keyName] {
            return label
        }
        if keyName.count == 1 {
            return keyName.uppercased()
        }
        return keyLabel(forKeyCode: keyCode)
    }

    private static func keyLabel(forKeyCode keyCode: UInt32) -> String {
        specialKeyLabels[keyCode] ?? "Key \(keyCode)"
    }

    private static func keyCode(for keyName: String) -> UInt32? {
        if let keyCode = namedKeyCodes[keyName] {
            return keyCode
        }
        if let keyCode = ansiKeyCodes[keyName] {
            return keyCode
        }
        return nil
    }

    private static let namedKeyLabels: [String: String] = [
        "space": "Space",
        "return": "Return",
        "enter": "Return",
        "tab": "Tab",
        "escape": "Escape",
        "esc": "Escape",
        "delete": "Delete",
        "backspace": "Delete",
        "forwarddelete": "Forward Delete",
        "f1": "F1",
        "f2": "F2",
        "f3": "F3",
        "f4": "F4",
        "f5": "F5",
        "f6": "F6",
        "f7": "F7",
        "f8": "F8",
        "f9": "F9",
        "f10": "F10",
        "f11": "F11",
        "f12": "F12",
        "left": "Left",
        "right": "Right",
        "up": "Up",
        "down": "Down"
    ]

    private static let namedKeyCodes: [String: UInt32] = [
        "space": UInt32(kVK_Space),
        "return": UInt32(kVK_Return),
        "enter": UInt32(kVK_Return),
        "tab": UInt32(kVK_Tab),
        "escape": UInt32(kVK_Escape),
        "esc": UInt32(kVK_Escape),
        "delete": UInt32(kVK_Delete),
        "backspace": UInt32(kVK_Delete),
        "forwarddelete": UInt32(kVK_ForwardDelete),
        "f1": UInt32(kVK_F1),
        "f2": UInt32(kVK_F2),
        "f3": UInt32(kVK_F3),
        "f4": UInt32(kVK_F4),
        "f5": UInt32(kVK_F5),
        "f6": UInt32(kVK_F6),
        "f7": UInt32(kVK_F7),
        "f8": UInt32(kVK_F8),
        "f9": UInt32(kVK_F9),
        "f10": UInt32(kVK_F10),
        "f11": UInt32(kVK_F11),
        "f12": UInt32(kVK_F12),
        "left": UInt32(kVK_LeftArrow),
        "right": UInt32(kVK_RightArrow),
        "up": UInt32(kVK_UpArrow),
        "down": UInt32(kVK_DownArrow)
    ]

    private static let specialKeyLabels: [UInt32: String] = [
        UInt32(kVK_Space): "Space",
        UInt32(kVK_Return): "Return",
        UInt32(kVK_Tab): "Tab",
        UInt32(kVK_Escape): "Escape",
        UInt32(kVK_Delete): "Delete",
        UInt32(kVK_ForwardDelete): "Forward Delete",
        UInt32(kVK_F1): "F1",
        UInt32(kVK_F2): "F2",
        UInt32(kVK_F3): "F3",
        UInt32(kVK_F4): "F4",
        UInt32(kVK_F5): "F5",
        UInt32(kVK_F6): "F6",
        UInt32(kVK_F7): "F7",
        UInt32(kVK_F8): "F8",
        UInt32(kVK_F9): "F9",
        UInt32(kVK_F10): "F10",
        UInt32(kVK_F11): "F11",
        UInt32(kVK_F12): "F12",
        UInt32(kVK_LeftArrow): "Left",
        UInt32(kVK_RightArrow): "Right",
        UInt32(kVK_UpArrow): "Up",
        UInt32(kVK_DownArrow): "Down"
    ]

    private static let ansiKeyCodes: [String: UInt32] = [
        "a": UInt32(kVK_ANSI_A),
        "b": UInt32(kVK_ANSI_B),
        "c": UInt32(kVK_ANSI_C),
        "d": UInt32(kVK_ANSI_D),
        "e": UInt32(kVK_ANSI_E),
        "f": UInt32(kVK_ANSI_F),
        "g": UInt32(kVK_ANSI_G),
        "h": UInt32(kVK_ANSI_H),
        "i": UInt32(kVK_ANSI_I),
        "j": UInt32(kVK_ANSI_J),
        "k": UInt32(kVK_ANSI_K),
        "l": UInt32(kVK_ANSI_L),
        "m": UInt32(kVK_ANSI_M),
        "n": UInt32(kVK_ANSI_N),
        "o": UInt32(kVK_ANSI_O),
        "p": UInt32(kVK_ANSI_P),
        "q": UInt32(kVK_ANSI_Q),
        "r": UInt32(kVK_ANSI_R),
        "s": UInt32(kVK_ANSI_S),
        "t": UInt32(kVK_ANSI_T),
        "u": UInt32(kVK_ANSI_U),
        "v": UInt32(kVK_ANSI_V),
        "w": UInt32(kVK_ANSI_W),
        "x": UInt32(kVK_ANSI_X),
        "y": UInt32(kVK_ANSI_Y),
        "z": UInt32(kVK_ANSI_Z),
        "0": UInt32(kVK_ANSI_0),
        "1": UInt32(kVK_ANSI_1),
        "2": UInt32(kVK_ANSI_2),
        "3": UInt32(kVK_ANSI_3),
        "4": UInt32(kVK_ANSI_4),
        "5": UInt32(kVK_ANSI_5),
        "6": UInt32(kVK_ANSI_6),
        "7": UInt32(kVK_ANSI_7),
        "8": UInt32(kVK_ANSI_8),
        "9": UInt32(kVK_ANSI_9)
    ]
}

import Foundation

struct SettingsSnapshot: Equatable {
    var hotkey: HotkeyShortcut
    var submitHotkey: HotkeyShortcut?
    var language: String
    var engine: String
    var lastModelByEngine: [String: String]
    var silenceAutoStopEnabled: Bool
    var microphoneDeviceUID: String?
    var outputMode: OutputMode
    var allowUnverifiedPasteFallback: Bool
}

enum OutputMode: String, CaseIterable, Codable {
    case pasteRestoreClipboard = "paste_restore_clipboard"
    case pasteKeepClipboard = "paste_keep_clipboard"
    case copyOnly = "copy_only"

    var label: String {
        switch self {
        case .pasteRestoreClipboard:
            return "Paste + Restore Clipboard"
        case .pasteKeepClipboard:
            return "Paste + Keep Clipboard"
        case .copyOnly:
            return "Copy Only"
        }
    }

    var shouldAttemptPaste: Bool {
        self != .copyOnly
    }

    var restoresClipboardAfterPaste: Bool {
        self == .pasteRestoreClipboard
    }
}

final class SettingsStore {
    private enum Key {
        static let hotkey = "hotkey"
        static let submitHotkey = "submitHotkey"
        static let language = "language"
        static let engine = "engine"
        static let lastModelByEngine = "lastModelByEngine"
        static let silenceAutoStopEnabled = "silenceAutoStopEnabled"
        static let microphoneDeviceUID = "microphoneDeviceUID"
        static let outputMode = "outputMode"
        static let allowUnverifiedPasteFallback = "allowUnverifiedPasteFallback"
    }

    private let defaults: UserDefaults
    private let registry: ModelRegistry

    init(defaults: UserDefaults = .standard, registry: ModelRegistry) {
        self.defaults = defaults
        self.registry = registry
    }

    func load() -> SettingsSnapshot {
        let hotkey = HotkeyShortcut.fromStorageValue(defaults.string(forKey: Key.hotkey))
        let storedSubmitHotkey = HotkeyShortcut.optionalFromStorageValue(defaults.string(forKey: Key.submitHotkey))
        let submitHotkey = storedSubmitHotkey == hotkey ? nil : storedSubmitHotkey
        if storedSubmitHotkey != nil, submitHotkey == nil {
            defaults.removeObject(forKey: Key.submitHotkey)
        }
        let engine = registry.validEngine(defaults.string(forKey: Key.engine))
        let language = registry.validLanguage(defaults.string(forKey: Key.language), for: engine)
        let storedModels = defaults.dictionary(forKey: Key.lastModelByEngine) as? [String: String] ?? [:]
        let models = registry.coerceModels(storedModels)
        let hasSilenceSetting = defaults.object(forKey: Key.silenceAutoStopEnabled) != nil
        let microphoneDeviceUID = defaults.string(forKey: Key.microphoneDeviceUID)
        let outputMode = OutputMode(rawValue: defaults.string(forKey: Key.outputMode) ?? "")
            ?? .pasteRestoreClipboard
        return SettingsSnapshot(
            hotkey: hotkey,
            submitHotkey: submitHotkey,
            language: language,
            engine: engine,
            lastModelByEngine: models,
            silenceAutoStopEnabled: hasSilenceSetting ? defaults.bool(forKey: Key.silenceAutoStopEnabled) : true,
            microphoneDeviceUID: microphoneDeviceUID,
            outputMode: outputMode,
            allowUnverifiedPasteFallback: defaults.bool(forKey: Key.allowUnverifiedPasteFallback)
        )
    }

    func save(_ snapshot: SettingsSnapshot) {
        defaults.set(snapshot.hotkey.storageValue, forKey: Key.hotkey)
        if let submitHotkey = snapshot.submitHotkey {
            defaults.set(submitHotkey.storageValue, forKey: Key.submitHotkey)
        } else {
            defaults.removeObject(forKey: Key.submitHotkey)
        }
        let engine = registry.validEngine(snapshot.engine)
        defaults.set(registry.validLanguage(snapshot.language, for: engine), forKey: Key.language)
        defaults.set(engine, forKey: Key.engine)
        defaults.set(registry.coerceModels(snapshot.lastModelByEngine), forKey: Key.lastModelByEngine)
        defaults.set(snapshot.silenceAutoStopEnabled, forKey: Key.silenceAutoStopEnabled)
        if let microphoneDeviceUID = snapshot.microphoneDeviceUID, !microphoneDeviceUID.isEmpty {
            defaults.set(microphoneDeviceUID, forKey: Key.microphoneDeviceUID)
        } else {
            defaults.removeObject(forKey: Key.microphoneDeviceUID)
        }
        defaults.set(snapshot.outputMode.rawValue, forKey: Key.outputMode)
        defaults.set(snapshot.allowUnverifiedPasteFallback, forKey: Key.allowUnverifiedPasteFallback)
    }
}

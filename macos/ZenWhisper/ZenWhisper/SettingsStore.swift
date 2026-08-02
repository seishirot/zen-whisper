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
    var enhancement: EnhancementSelection = .off
}

enum OutputMode: String, CaseIterable, Codable {
    case pasteRestoreClipboard = "paste_restore_clipboard"
    case pasteKeepClipboard = "paste_keep_clipboard"
    case copyOnly = "copy_only"

    var label: String {
        switch self {
        case .pasteRestoreClipboard:
            return "Paste + Keep Clipboard (Legacy)"
        case .pasteKeepClipboard:
            return "Paste + Keep Clipboard"
        case .copyOnly:
            return "Copy Only"
        }
    }

    var shouldAttemptPaste: Bool {
        self != .copyOnly
    }

    static var allCases: [OutputMode] {
        [.pasteKeepClipboard, .copyOnly]
    }

    var safeReplacement: OutputMode {
        self == .pasteRestoreClipboard ? .pasteKeepClipboard : self
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
        static let legacyAllowUnverifiedPasteFallback =
            "allowUnverifiedPasteFallback"
        static let enhancementProfile = "enhancementProfile"
        static let enhancementPostprocessor = "enhancementPostprocessor"
        static let enhancementPostprocessorApprovalRevision =
            "enhancementPostprocessorApprovalRevision"
    }

    private let defaults: UserDefaults
    private let registry: ModelRegistry
    private(set) var didMigrateClipboardRestoreMode = false

    init(defaults: UserDefaults = .standard, registry: ModelRegistry) {
        self.defaults = defaults
        self.registry = registry
    }

    func load() -> SettingsSnapshot {
        didMigrateClipboardRestoreMode = false
        defaults.removeObject(forKey: Key.legacyAllowUnverifiedPasteFallback)
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
        let storedOutputMode = defaults.string(forKey: Key.outputMode)
            .flatMap(OutputMode.init(rawValue:))
        let needsOutputModeMigration = storedOutputMode == nil
            || storedOutputMode == .pasteRestoreClipboard
        let outputMode = (storedOutputMode ?? .pasteRestoreClipboard)
            .safeReplacement
        if needsOutputModeMigration {
            defaults.set(outputMode.rawValue, forKey: Key.outputMode)
            didMigrateClipboardRestoreMode = true
        }
        let profileID = defaults.string(forKey: Key.enhancementProfile)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let enhancement = EnhancementSelection(
            profileID: profileID?.isEmpty == false ? profileID : nil,
            postprocessing: PostprocessingSelection(
                storageValue: defaults.string(
                    forKey: Key.enhancementPostprocessor
                )
            ),
            approvedPostprocessorRevision: defaults.string(
                forKey: Key.enhancementPostprocessorApprovalRevision
            )
        )
        return SettingsSnapshot(
            hotkey: hotkey,
            submitHotkey: submitHotkey,
            language: language,
            engine: engine,
            lastModelByEngine: models,
            silenceAutoStopEnabled: hasSilenceSetting ? defaults.bool(forKey: Key.silenceAutoStopEnabled) : true,
            microphoneDeviceUID: microphoneDeviceUID,
            outputMode: outputMode,
            enhancement: enhancement
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
        defaults.set(
            snapshot.outputMode.safeReplacement.rawValue,
            forKey: Key.outputMode
        )
        defaults.removeObject(forKey: Key.legacyAllowUnverifiedPasteFallback)
        if let profileID = snapshot.enhancement.profileID,
           !profileID.isEmpty {
            defaults.set(profileID, forKey: Key.enhancementProfile)
        } else {
            defaults.removeObject(forKey: Key.enhancementProfile)
        }
        defaults.set(
            snapshot.enhancement.postprocessing.storageValue,
            forKey: Key.enhancementPostprocessor
        )
        let approvalRevision = snapshot.enhancement
            .approvedPostprocessorRevision?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if snapshot.enhancement.postprocessing.isCLI,
           let approvalRevision,
           !approvalRevision.isEmpty {
            defaults.set(
                approvalRevision,
                forKey: Key.enhancementPostprocessorApprovalRevision
            )
        } else {
            defaults.removeObject(
                forKey: Key.enhancementPostprocessorApprovalRevision
            )
        }
    }
}

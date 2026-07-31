import Foundation

enum SettingsApplyPlan: Equatable {
    case none
    case saveOnly
    case preload
    case restart
}

enum SettingsValidationIssue: Equatable {
    case primarySubmitHotkeyCollision(HotkeyShortcut)

    var message: String {
        switch self {
        case .primarySubmitHotkeyCollision(let shortcut):
            return "\(shortcut.label) cannot be used for both the primary and submit hotkeys."
        }
    }
}

struct SettingsEditorState {
    private let registry: ModelRegistry

    private(set) var baselineSettings: SettingsSnapshot
    var draftSettings: SettingsSnapshot
    private(set) var baselineLaunchAtLoginStatus: LoginItemStatus
    var draftLaunchAtLoginEnabled: Bool
    private(set) var launchAtLoginOverrideRequested = false

    init(
        settings: SettingsSnapshot,
        launchAtLoginStatus: LoginItemStatus,
        registry: ModelRegistry
    ) {
        let normalizedSettings = Self.normalized(settings, using: registry)
        self.registry = registry
        baselineSettings = normalizedSettings
        draftSettings = normalizedSettings
        baselineLaunchAtLoginStatus = launchAtLoginStatus
        draftLaunchAtLoginEnabled = launchAtLoginStatus.isEnabled
    }

    var normalizedDraftSettings: SettingsSnapshot {
        Self.normalized(draftSettings, using: registry)
    }

    var runtimeSettingsChanged: Bool {
        normalizedDraftSettings != baselineSettings
    }

    var launchAtLoginChanged: Bool {
        switch baselineLaunchAtLoginStatus {
        case .enabled:
            return !draftLaunchAtLoginEnabled
        case .disabled:
            return draftLaunchAtLoginEnabled
        case .invalid:
            return launchAtLoginOverrideRequested
        }
    }

    var launchAtLoginStatusIssue: String? {
        guard case .invalid(let reason) = baselineLaunchAtLoginStatus,
              !launchAtLoginOverrideRequested else {
            return nil
        }
        return reason
    }

    var launchAtLoginSelectionIsIndeterminate: Bool {
        launchAtLoginStatusIssue != nil
    }

    var isDirty: Bool {
        runtimeSettingsChanged || launchAtLoginChanged
    }

    var validationIssue: SettingsValidationIssue? {
        let settings = normalizedDraftSettings
        guard let submitHotkey = settings.submitHotkey,
              submitHotkey == settings.hotkey else {
            return nil
        }
        return .primarySubmitHotkeyCollision(submitHotkey)
    }

    var applyPlan: SettingsApplyPlan {
        guard isDirty else {
            return .none
        }

        let settings = normalizedDraftSettings
        let baselineEngine = registry.validEngine(baselineSettings.engine)
        let draftEngine = registry.validEngine(settings.engine)
        let baselineModel = registry.validModel(
            baselineSettings.lastModelByEngine[baselineEngine],
            for: baselineEngine
        )
        let draftModel = registry.validModel(
            settings.lastModelByEngine[draftEngine],
            for: draftEngine
        )

        if draftEngine != baselineEngine || draftModel != baselineModel {
            return .restart
        }
        if settings.language != baselineSettings.language {
            return .preload
        }
        return .saveOnly
    }

    func canSave(isBusy: Bool) -> Bool {
        guard isDirty, validationIssue == nil else {
            return false
        }
        return !isBusy || !runtimeSettingsChanged
    }

    mutating func normalizeDraft() {
        draftSettings = normalizedDraftSettings
    }

    mutating func reset() {
        draftSettings = baselineSettings
        draftLaunchAtLoginEnabled = baselineLaunchAtLoginStatus.isEnabled
        launchAtLoginOverrideRequested = false
    }

    mutating func cancel() {
        reset()
    }

    mutating func markApplied() {
        normalizeDraft()
        baselineSettings = draftSettings
        baselineLaunchAtLoginStatus = draftLaunchAtLoginEnabled ? .enabled : .disabled
        launchAtLoginOverrideRequested = false
    }

    mutating func setDraftLaunchAtLoginEnabled(_ enabled: Bool) {
        draftLaunchAtLoginEnabled = enabled
        launchAtLoginOverrideRequested = true
    }

    mutating func synchronize(
        authoritativeSettings: SettingsSnapshot,
        launchAtLoginStatus: LoginItemStatus
    ) {
        let authoritativeSettings = Self.normalized(authoritativeSettings, using: registry)
        let localSettings = normalizedDraftSettings
        let previousBaseline = baselineSettings
        let launchAtLoginWasEdited = launchAtLoginChanged

        draftSettings = Self.normalized(
            SettingsSnapshot(
                hotkey: Self.mergeField(
                    local: localSettings.hotkey,
                    baseline: previousBaseline.hotkey,
                    authoritative: authoritativeSettings.hotkey
                ),
                submitHotkey: Self.mergeField(
                    local: localSettings.submitHotkey,
                    baseline: previousBaseline.submitHotkey,
                    authoritative: authoritativeSettings.submitHotkey
                ),
                language: Self.mergeField(
                    local: localSettings.language,
                    baseline: previousBaseline.language,
                    authoritative: authoritativeSettings.language
                ),
                engine: Self.mergeField(
                    local: localSettings.engine,
                    baseline: previousBaseline.engine,
                    authoritative: authoritativeSettings.engine
                ),
                lastModelByEngine: Self.mergeModels(
                    local: localSettings.lastModelByEngine,
                    baseline: previousBaseline.lastModelByEngine,
                    authoritative: authoritativeSettings.lastModelByEngine
                ),
                silenceAutoStopEnabled: Self.mergeField(
                    local: localSettings.silenceAutoStopEnabled,
                    baseline: previousBaseline.silenceAutoStopEnabled,
                    authoritative: authoritativeSettings.silenceAutoStopEnabled
                ),
                microphoneDeviceUID: Self.mergeField(
                    local: localSettings.microphoneDeviceUID,
                    baseline: previousBaseline.microphoneDeviceUID,
                    authoritative: authoritativeSettings.microphoneDeviceUID
                ),
                outputMode: Self.mergeField(
                    local: localSettings.outputMode,
                    baseline: previousBaseline.outputMode,
                    authoritative: authoritativeSettings.outputMode
                ),
                enhancement: Self.mergeField(
                    local: localSettings.enhancement,
                    baseline: previousBaseline.enhancement,
                    authoritative: authoritativeSettings.enhancement
                )
            ),
            using: registry
        )

        if !launchAtLoginWasEdited {
            draftLaunchAtLoginEnabled = launchAtLoginStatus.isEnabled
        }
        baselineSettings = authoritativeSettings
        baselineLaunchAtLoginStatus = launchAtLoginStatus
        switch launchAtLoginStatus {
        case .invalid:
            launchAtLoginOverrideRequested = launchAtLoginWasEdited
        case .enabled, .disabled:
            launchAtLoginOverrideRequested = false
        }
    }

    static func normalized(
        _ settings: SettingsSnapshot,
        using registry: ModelRegistry
    ) -> SettingsSnapshot {
        let engine = registry.validEngine(settings.engine)
        let enhancement = EnhancementSelection(
            profileID: settings.enhancement.profileID,
            postprocessing: settings.enhancement.postprocessing,
            approvedPostprocessorRevision:
                settings.enhancement.approvedPostprocessorRevision
        )
        return SettingsSnapshot(
            hotkey: settings.hotkey,
            submitHotkey: settings.submitHotkey,
            language: registry.validLanguage(settings.language, for: engine),
            engine: engine,
            lastModelByEngine: registry.coerceModels(settings.lastModelByEngine),
            silenceAutoStopEnabled: settings.silenceAutoStopEnabled,
            microphoneDeviceUID: settings.microphoneDeviceUID?.isEmpty == true
                ? nil
                : settings.microphoneDeviceUID,
            outputMode: settings.outputMode,
            enhancement: enhancement
        )
    }

    private static func mergeField<Value: Equatable>(
        local: Value,
        baseline: Value,
        authoritative: Value
    ) -> Value {
        local == baseline ? authoritative : local
    }

    private static func mergeModels(
        local: [String: String],
        baseline: [String: String],
        authoritative: [String: String]
    ) -> [String: String] {
        var merged = authoritative
        let engineIDs = Set(local.keys)
            .union(baseline.keys)
            .union(authoritative.keys)

        for engineID in engineIDs where local[engineID] != baseline[engineID] {
            if let localModel = local[engineID] {
                merged[engineID] = localModel
            } else {
                merged.removeValue(forKey: engineID)
            }
        }
        return merged
    }
}

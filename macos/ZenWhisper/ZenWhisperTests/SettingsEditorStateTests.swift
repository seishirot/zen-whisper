import XCTest
@testable import ZenWhisper

final class SettingsEditorStateTests: XCTestCase {
    private var registry: ModelRegistry!

    override func setUpWithError() throws {
        registry = try ModelRegistry.loadDefault()
    }

    func testInitializationNormalizesRegistryValuesWithoutCoercingMicrophone() {
        var settings = makeSettings()
        settings.engine = "missing-engine"
        settings.language = "missing-language"
        settings.lastModelByEngine = ["mlx-qwen3-asr": "missing-model"]
        settings.microphoneDeviceUID = "unavailable-device-uid"

        let state = SettingsEditorState(
            settings: settings,
            launchAtLoginStatus: .disabled,
            registry: registry
        )

        XCTAssertEqual(state.draftSettings.engine, registry.defaultEngine)
        XCTAssertEqual(
            state.draftSettings.language,
            registry.validLanguage(nil, for: registry.defaultEngine)
        )
        XCTAssertEqual(
            state.draftSettings.lastModelByEngine,
            registry.coerceModels(["mlx-qwen3-asr": "missing-model"])
        )
        XCTAssertEqual(state.draftSettings.microphoneDeviceUID, "unavailable-device-uid")
        XCTAssertEqual(state.baselineSettings, state.draftSettings)
        XCTAssertFalse(state.isDirty)
    }

    func testNormalizationTreatsEmptyMicrophoneAsSystemDefault() {
        var settings = makeSettings()
        settings.microphoneDeviceUID = ""

        let state = SettingsEditorState(
            settings: settings,
            launchAtLoginStatus: .disabled,
            registry: registry
        )

        XCTAssertNil(state.draftSettings.microphoneDeviceUID)
        XCTAssertFalse(state.isDirty)
    }

    func testDirtyDetectionResetCancelAndMarkApplied() {
        var state = makeState()

        state.draftSettings.silenceAutoStopEnabled.toggle()
        XCTAssertTrue(state.runtimeSettingsChanged)
        XCTAssertTrue(state.isDirty)

        state.draftSettings.silenceAutoStopEnabled.toggle()
        XCTAssertFalse(state.runtimeSettingsChanged)
        XCTAssertFalse(state.isDirty)

        state.draftSettings.outputMode = .copyOnly
        state.reset()
        XCTAssertEqual(state.draftSettings, state.baselineSettings)
        XCTAssertFalse(state.isDirty)

        state.draftLaunchAtLoginEnabled.toggle()
        state.cancel()
        XCTAssertEqual(
            state.draftLaunchAtLoginEnabled,
            state.baselineLaunchAtLoginStatus.isEnabled
        )
        XCTAssertFalse(state.isDirty)

        state.draftSettings.outputMode = .copyOnly
        state.draftLaunchAtLoginEnabled = true
        state.markApplied()
        XCTAssertEqual(state.baselineSettings.outputMode, .copyOnly)
        XCTAssertEqual(state.baselineLaunchAtLoginStatus, .enabled)
        XCTAssertFalse(state.isDirty)
    }

    func testSynchronizePreservesEditedFieldsAndRefreshesUntouchedFields() {
        var state = makeState()
        state.draftSettings.hotkey = .controlOptionCommandSpace
        state.draftSettings.lastModelByEngine["mlx-qwen3-asr"] =
            "mlx-community/Qwen3-ASR-1.7B-8bit"
        state.draftSettings.microphoneDeviceUID = "locally-missing-device"
        state.draftLaunchAtLoginEnabled = true

        var authoritative = makeSettings()
        authoritative.hotkey = .shiftCommandSpace
        authoritative.language = "en"
        authoritative.lastModelByEngine["mlx-whisper"] =
            "mlx-community/whisper-large-v3-turbo"
        authoritative.silenceAutoStopEnabled = false
        authoritative.microphoneDeviceUID = "external-device"
        authoritative.outputMode = .copyOnly

        state.synchronize(
            authoritativeSettings: authoritative,
            launchAtLoginStatus: .disabled
        )

        XCTAssertEqual(state.baselineSettings, authoritative)
        XCTAssertEqual(state.draftSettings.hotkey, .controlOptionCommandSpace)
        XCTAssertEqual(state.draftSettings.language, "en")
        XCTAssertEqual(
            state.draftSettings.lastModelByEngine["mlx-qwen3-asr"],
            "mlx-community/Qwen3-ASR-1.7B-8bit"
        )
        XCTAssertFalse(state.draftSettings.silenceAutoStopEnabled)
        XCTAssertEqual(state.draftSettings.microphoneDeviceUID, "locally-missing-device")
        XCTAssertEqual(state.draftSettings.outputMode, .copyOnly)
        XCTAssertTrue(state.draftLaunchAtLoginEnabled)
        XCTAssertTrue(state.isDirty)
    }

    func testSynchronizeRefreshesUntouchedLaunchAtLoginValue() {
        var state = makeState()

        state.synchronize(
            authoritativeSettings: makeSettings(),
            launchAtLoginStatus: .enabled
        )

        XCTAssertEqual(state.baselineLaunchAtLoginStatus, .enabled)
        XCTAssertTrue(state.draftLaunchAtLoginEnabled)
        XCTAssertFalse(state.isDirty)
    }

    func testSaveEligibilityBlocksBusyRuntimeChangesButAllowsLaunchOnlyChange() {
        var state = makeState()

        XCTAssertFalse(state.canSave(isBusy: false))

        state.draftSettings.outputMode = .copyOnly
        XCTAssertTrue(state.canSave(isBusy: false))
        XCTAssertFalse(state.canSave(isBusy: true))

        state.reset()
        state.draftLaunchAtLoginEnabled.toggle()
        XCTAssertTrue(state.canSave(isBusy: false))
        XCTAssertTrue(state.canSave(isBusy: true))
    }

    func testAppStateCentralizesSettingsBusyPolicy() {
        let busyStates: [AppState] = [
            .recording(elapsed: 1, voiceActive: true),
            .preloading(message: "model"),
            .transcribing,
            .postprocessing,
            .repairingBackend,
        ]
        XCTAssertTrue(busyStates.allSatisfy(\.blocksSettingsChanges))

        let availableStates: [AppState] = [
            .idle,
            .inputWaiting,
            .pasteUnavailable("Accessibility"),
            .copied(pasteDispatched: false, reason: nil),
            .copySkipped("reason"),
            .copyFailed("reason"),
            .enhancementWarning("reason"),
            .modelUnavailable("reason"),
            .backendRepairRequired("reason"),
            .microphoneError("reason"),
            .hotkeyError("reason"),
            .appSignatureChanged,
            .error("reason"),
        ]
        XCTAssertTrue(availableStates.allSatisfy { !$0.blocksSettingsChanges })
    }

    func testApplyPlanUsesRestartPrecedenceAndIgnoresInactiveModelChanges() {
        var state = makeState()
        XCTAssertEqual(state.applyPlan, .none)

        state.draftSettings.outputMode = .copyOnly
        XCTAssertEqual(state.applyPlan, .saveOnly)

        state.reset()
        state.draftLaunchAtLoginEnabled.toggle()
        XCTAssertEqual(state.applyPlan, .saveOnly)

        state.reset()
        state.draftSettings.language = "en"
        XCTAssertEqual(state.applyPlan, .preload)

        state.draftSettings.engine = "mlx-qwen3-asr"
        XCTAssertEqual(state.applyPlan, .restart)

        state.reset()
        state.draftSettings.lastModelByEngine["mlx-qwen3-asr"] =
            "mlx-community/Qwen3-ASR-1.7B-8bit"
        XCTAssertEqual(state.applyPlan, .saveOnly)

        state = makeState(engine: "mlx-qwen3-asr")
        state.draftSettings.lastModelByEngine["mlx-qwen3-asr"] =
            "mlx-community/Qwen3-ASR-1.7B-8bit"
        state.draftSettings.language = "en"
        XCTAssertEqual(state.applyPlan, .restart)
    }

    func testDuplicatePrimaryAndSubmitHotkeysAreInvalid() {
        var state = makeState()
        state.draftSettings.submitHotkey = state.draftSettings.hotkey

        XCTAssertEqual(
            state.validationIssue,
            .primarySubmitHotkeyCollision(state.draftSettings.hotkey)
        )
        XCTAssertFalse(state.canSave(isBusy: false))
        XCTAssertTrue(
            state.validationIssue?.message.contains(state.draftSettings.hotkey.label) == true
        )

        state.draftSettings.submitHotkey = nil
        XCTAssertNil(state.validationIssue)
    }

    func testNormalizeDraftMakesSemanticNoOpClean() {
        var state = makeState()
        state.draftSettings.engine = "missing-engine"
        state.draftSettings.language = "missing-language"
        state.draftSettings.lastModelByEngine = [:]
        state.draftSettings.microphoneDeviceUID = ""

        XCTAssertFalse(state.isDirty)
        state.normalizeDraft()
        XCTAssertEqual(state.draftSettings, state.baselineSettings)
        XCTAssertNil(state.draftSettings.microphoneDeviceUID)
    }

    func testPartialSaveAdvancesRuntimeBaselineAndKeepsFailedLaunchChangeDirty() {
        var state = makeState()
        state.draftSettings.outputMode = .copyOnly
        state.draftLaunchAtLoginEnabled = true
        let appliedRuntimeSettings = state.draftSettings

        state.synchronize(
            authoritativeSettings: appliedRuntimeSettings,
            launchAtLoginStatus: .disabled
        )

        XCTAssertFalse(state.runtimeSettingsChanged)
        XCTAssertTrue(state.launchAtLoginChanged)
        XCTAssertTrue(state.isDirty)
        XCTAssertTrue(state.canSave(isBusy: true))
    }

    func testInvalidLaunchAtLoginStatusRequiresExplicitReplacement() {
        var state = SettingsEditorState(
            settings: makeSettings(),
            launchAtLoginStatus: .invalid("unreadable plist"),
            registry: registry
        )

        XCTAssertTrue(state.launchAtLoginSelectionIsIndeterminate)
        XCTAssertEqual(state.launchAtLoginStatusIssue, "unreadable plist")
        XCTAssertFalse(state.isDirty)

        state.setDraftLaunchAtLoginEnabled(false)
        XCTAssertFalse(state.launchAtLoginSelectionIsIndeterminate)
        XCTAssertTrue(state.launchAtLoginChanged)

        state.cancel()
        XCTAssertTrue(state.launchAtLoginSelectionIsIndeterminate)
        XCTAssertFalse(state.isDirty)

        state.setDraftLaunchAtLoginEnabled(true)
        state.synchronize(
            authoritativeSettings: makeSettings(),
            launchAtLoginStatus: .enabled
        )
        XCTAssertFalse(state.launchAtLoginChanged)
        XCTAssertNil(state.launchAtLoginStatusIssue)
    }

    func testHotkeyRecorderSessionRejectsCallbacksAfterCancellation() {
        var session = SettingsHotkeyRecorderSession()
        let discardedGeneration = session.begin()

        XCTAssertTrue(session.isActive)
        XCTAssertTrue(session.cancel())
        XCTAssertFalse(session.isActive)
        XCTAssertFalse(session.finish(generation: discardedGeneration))

        let currentGeneration = session.begin()
        XCTAssertTrue(session.finish(generation: currentGeneration))
        XCTAssertFalse(session.finish(generation: currentGeneration))
    }

    private func makeState(
        engine: String = "mlx-whisper",
        launchAtLoginEnabled: Bool = false
    ) -> SettingsEditorState {
        SettingsEditorState(
            settings: makeSettings(engine: engine),
            launchAtLoginStatus: launchAtLoginEnabled ? .enabled : .disabled,
            registry: registry
        )
    }

    private func makeSettings(engine: String = "mlx-whisper") -> SettingsSnapshot {
        SettingsSnapshot(
            hotkey: .shiftSpace,
            submitHotkey: .shiftCommandSpace,
            language: "ja",
            engine: engine,
            lastModelByEngine: [
                "mlx-whisper": "mlx-community/whisper-large-v3-turbo",
                "mlx-qwen3-asr": "mlx-community/Qwen3-ASR-0.6B-8bit",
            ],
            silenceAutoStopEnabled: true,
            microphoneDeviceUID: nil,
            outputMode: .pasteRestoreClipboard,
            allowUnverifiedPasteFallback: false
        )
    }
}

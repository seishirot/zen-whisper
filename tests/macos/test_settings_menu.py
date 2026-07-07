from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_settings_menu_shows_selected_values_and_disables_while_busy() -> None:
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")

    assert 'languageMenuItem.title = "Language: \\(selectedLanguageLabel)"' in status_controller
    assert "items: registry.supportedLanguages(for: engine.id)" in status_controller
    assert 'recognitionModelMenuItem.title = "Recognition Model: \\(selectedModelLabel)"' in status_controller
    assert "private func recognitionModelMenuItems(from registry: ModelRegistry)" in status_controller
    assert "registry.engines.flatMap { engine in" in status_controller
    assert 'modelMenuKey(engineID: entry.engine.id, modelID: entry.model.id)' in status_controller
    assert '"\\(entry.model.label) (\\(entry.engine.label))"' in status_controller
    assert 'hotkeyMenuItem.title = "Hotkey: \\(settings.hotkey.label)"' in status_controller
    assert "private func hotkeyMenu(selected: HotkeyShortcut) -> NSMenu" in status_controller
    assert "HotkeyShortcut.presets" in status_controller
    assert 'NSMenuItem(title: "Record Custom Shortcut...", action: #selector(recordCustomHotkey)' in status_controller
    assert "onRecordCustomHotkey?()" in status_controller
    assert 'submitHotkeyMenuItem.title = settings.submitHotkey.map { "Submit Hotkey: \\($0.label)" } ?? "Submit Hotkey: Off"' in status_controller
    assert "private func submitHotkeyMenu(selected: HotkeyShortcut?) -> NSMenu" in status_controller
    assert "HotkeyShortcut.submitPresets" in status_controller
    assert "onSelectSubmitHotkey?(nil)" in status_controller
    assert "HotkeyShortcut.optionalFromStorageValue(trimmed)" in status_controller
    assert "onRecordCustomSubmitHotkey?()" in status_controller
    assert 'outputModeMenuItem.title = "Output: \\(settings.outputMode.label)"' in status_controller
    assert "items: OutputMode.allCases.map" in status_controller
    assert "onSelectOutputMode?(mode)" in status_controller
    assert 'NSMenuItem(title: "Launch at Login", action: #selector(toggleLaunchAtLogin)' in status_controller
    assert "func updateLaunchAtLogin(enabled: Bool)" in status_controller
    assert "launchAtLoginMenuItem.isEnabled = true" in status_controller
    assert "private func applySettingsEnabledState()" in status_controller
    assert "let enabled = registry != nil && settings != nil && !isBusy(currentState)" in status_controller
    assert "case .recording, .preloading, .transcribing, .repairingBackend:" in status_controller
    assert "microphoneMenuItem.isEnabled = settings != nil" in status_controller
    assert "engineMenuItem" not in status_controller
    assert "modelMenuItem" not in status_controller


def test_app_delegate_wires_settings_menu_to_saved_settings() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")

    assert "statusController.updateSettings(registry: registry, settings: settings)" in app_delegate
    assert "statusController.onSelectLanguage" in app_delegate
    assert "statusController.onSelectModel" in app_delegate
    assert "statusController.onSelectHotkey" in app_delegate
    assert "statusController.onRecordCustomHotkey" in app_delegate
    assert "statusController.onSelectSubmitHotkey" in app_delegate
    assert "statusController.onRecordCustomSubmitHotkey" in app_delegate
    assert "private let submitHotkeyManager = HotkeyManager(signature: HotkeyManager.submitSignature)" in app_delegate
    assert "private var hotkeyRecorder: HotkeyRecorderWindowController?" in app_delegate
    assert "private var submitHotkeyRecorder: HotkeyRecorderWindowController?" in app_delegate
    assert "private func recordCustomHotkey()" in app_delegate
    assert "private func recordCustomSubmitHotkey()" in app_delegate
    assert "private func closeHotkeyRecorders()" in app_delegate
    assert "hotkeyRecorder?.close()" in app_delegate
    assert "submitHotkeyRecorder?.close()" in app_delegate
    assert "hotkeyManager.suspend()" in app_delegate
    assert "submitHotkeyManager.suspend()" in app_delegate
    assert "private func resumeHotkeysAfterRecorder()" in app_delegate
    assert "try hotkeyManager.resume()" in app_delegate
    assert "try submitHotkeyManager.resume()" in app_delegate
    assert "if shortcut == self.settings.hotkey" in app_delegate
    assert "statusController.onToggleSilenceAutoStop" in app_delegate
    assert "statusController.onSelectOutputMode" in app_delegate
    assert "private func selectOutputMode(_ mode: OutputMode)" in app_delegate
    assert "statusController.onSelectMicrophone" in app_delegate
    assert "statusController.onToggleLaunchAtLogin" in app_delegate
    assert "private let loginItemManager = LoginItemManager()" in app_delegate
    assert "private func setLaunchAtLogin(_ enabled: Bool)" in app_delegate
    assert "private func refreshLaunchAtLoginState()" in app_delegate
    assert "statusController.onCopyDiagnostics" in app_delegate
    assert "private func copyDiagnostics()" in app_delegate
    assert "private var startupDiagnostics: [String] = []" in app_delegate
    assert 'let message = "startup failed: \\(String(describing: error))"' in app_delegate
    assert '"startup issues:"' in app_delegate
    assert "startupDiagnostics.joined(separator: \"\\n\")" in app_delegate
    assert "backend-startup.log:" in app_delegate
    assert "private var appLogger: AppLogger?" in app_delegate
    assert "private func logInfo(_ message: String)" in app_delegate
    assert "appLogger?.info(message)" in app_delegate
    assert "private func saveSettingsOnly()" in app_delegate
    assert "settingsStore.save(settings)" in app_delegate
    assert "private func saveSettingsAndPreloadSelectedModel()" in app_delegate
    assert "private func saveSettingsAndRestartBackendForModelChange()" in app_delegate
    assert "private func restartBackendForModelChange()" in app_delegate
    assert "preloadSelectedModel()" in app_delegate
    assert "restarting backend after recognition model change" in app_delegate
    assert "backend.stop()" in app_delegate
    assert "self.startBackend()" in app_delegate
    assert "settings.language = registry.validLanguage(settings.language, for: engine)" in app_delegate
    assert "statusController.onSelectEngine" not in app_delegate
    assert "private func selectEngine" not in app_delegate


def test_model_change_restarts_backend_to_release_loaded_model_memory() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    select_model = app_delegate[
        app_delegate.index("private func selectModel(engine: String, model: String)"):
        app_delegate.index("private func selectHotkey")
    ]
    restart = app_delegate[
        app_delegate.index("private func restartBackendForModelChange()"):
        app_delegate.index("private func repairBackend()")
    ]

    assert "let selectionChanged = engine != previousEngine || model != previousModel" in select_model
    assert "saveSettingsAndRestartBackendForModelChange()" in select_model
    assert "saveSettingsOnly()" in select_model
    assert "backend.stop()" in restart
    assert "self.startBackend()" in restart
    assert "preloadSelectedModel()" not in select_model


def test_submit_hotkey_mode_is_preserved_until_recording_stops() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    toggle_recording = app_delegate[
        app_delegate.index("private func toggleRecording(submitAfterPaste: Bool = false)"):
        app_delegate.index("private func startRecording()")
    ]
    timer_refresh = app_delegate[
        app_delegate.index("@objc private func refreshRecordingTimer"):
        app_delegate.index("private func stopRecordingAndTranscribe")
    ]

    assert "let shouldSubmitAfterPaste = submitAfterPasteForCurrentRecording || submitAfterPaste" in toggle_recording
    assert "stopRecordingAndTranscribe(submitAfterPaste: shouldSubmitAfterPaste)" in toggle_recording
    assert "submitAfterPasteForCurrentRecording = submitAfterPaste" in toggle_recording
    assert "stopRecordingAndTranscribe(submitAfterPaste: submitAfterPasteForCurrentRecording)" in timer_refresh


def test_microphone_menu_lists_devices_and_recorder_uses_selected_uid() -> None:
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")
    recorder = (SWIFT_SRC / "AudioRecorder.swift").read_text(encoding="utf-8")
    settings = (SWIFT_SRC / "SettingsStore.swift").read_text(encoding="utf-8")

    assert 'microphoneMenuItem.title = "Microphone: System Default"' in status_controller
    assert "AudioDeviceManager.inputDevices()" in status_controller
    assert 'NSMenuItem(title: "System Default", action: #selector(selectMicrophone(_:))' in status_controller
    assert "onSelectMicrophone?(uid.isEmpty ? nil : uid)" in status_controller
    assert "func start(deviceUID: String? = nil) throws" in recorder
    assert "try AudioDeviceManager.applyInputDevice(uid: deviceUID, to: input)" in recorder
    assert "var microphoneDeviceUID: String?" in settings
    assert 'static let microphoneDeviceUID = "microphoneDeviceUID"' in settings
    assert "defaults.string(forKey: Key.microphoneDeviceUID)" in settings
    assert "AudioDeviceManager.validInputDeviceUID(snapshot.microphoneDeviceUID)" not in settings
    assert 'microphoneMenuItem.title = "Microphone: Unavailable"' in status_controller


def test_recovery_actions_are_tucked_under_troubleshooting_with_contextual_primary_action() -> None:
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")

    assert 'NSMenuItem(title: "Troubleshooting", action: nil, keyEquivalent: "")' in status_controller
    assert "troubleshootingMenuItem.submenu = troubleshootingMenu()" in status_controller
    assert "private func troubleshootingMenu() -> NSMenu" in status_controller
    assert 'NSMenuItem(title: "Copy Diagnostics", action: #selector(copyDiagnostics)' in status_controller
    assert "var onCopyDiagnostics: (() -> Void)?" in status_controller
    assert "primaryRecoveryMenuItem.isHidden = true" in status_controller
    assert "private func updatePrimaryRecovery(for state: AppState)" in status_controller
    assert 'return ("Retry Model Load", .retryPreload)' in status_controller
    assert 'return ("Repair Backend", .repairBackend)' in status_controller
    assert 'return ("Open Microphone Settings", .openMicrophoneSettings)' in status_controller
    assert 'return ("Retry Microphone", .retryMicrophone)' in status_controller
    assert 'return ("Open Accessibility Settings", .openAccessibilitySettings)' in status_controller
    assert 'return ("Accept Signature Change", .acceptSignatureChange)' in status_controller


def test_menus_do_not_let_appkit_auto_enable_stateful_actions() -> None:
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")

    assert "menu.autoenablesItems = false" in status_controller
    assert "statusMenuItem.isEnabled = false" in status_controller
    assert status_controller.count("submenu.autoenablesItems = false") >= 3
    assert "retryPreloadMenuItem.isEnabled = canRetryPreload(state)" in status_controller
    assert "repairMenuItem.isEnabled = canRepairBackend(state)" in status_controller
    assert "acceptSignatureMenuItem.isEnabled = canAcceptSignatureChange(state)" in status_controller
    assert "updateMicrophoneRecovery(for: state)" in status_controller
    assert "retryMicMenuItem.isEnabled = false" in status_controller
    assert "retryMicMenuItem.isEnabled = true" in status_controller
    assert "retryMicMenuItem.isHidden = true" in status_controller
    assert "retryMicMenuItem.isHidden = false" in status_controller

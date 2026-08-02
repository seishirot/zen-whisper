from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def _read_swift(filename: str) -> str:
    return (SWIFT_SRC / filename).read_text(encoding="utf-8")


def test_settings_window_covers_native_settings_and_preserves_unavailable_mic() -> None:
    controller = _read_swift("SettingsWindowController.swift")

    assert "final class SettingsWindowController: NSWindowController, NSWindowDelegate" in controller
    for control in (
        "primaryHotkeyPopup",
        "submitHotkeyPopup",
        "enginePopup",
        "modelPopup",
        "languagePopup",
        "microphonePopup",
        "outputModePopup",
        "silenceAutoStopCheckbox",
        "launchAtLoginCheckbox",
    ):
        assert control in controller

    assert "engineChoices = registry.engines.map(\\.id)" in controller
    assert "registry.validModel(" in controller
    assert "registry.supportedLanguages(for: engine.id)" in controller
    assert 'microphonePopup.addItem(withTitle: "System Default")' in controller
    assert '!audioInputDevices.contains(where: { $0.uid == selectedUID })' in controller
    assert '"Unavailable — \\(abbreviatedUID(selectedUID))"' in controller
    assert "microphonePopup.lastItem?.representedObject = selectedUID" in controller
    assert "Its full device identifier is preserved." in controller
    assert "microphonePopup.selectItem(at: 0)" in controller


def test_settings_window_protects_dirty_save_cancel_and_close() -> None:
    controller = _read_swift("SettingsWindowController.swift")

    assert "typealias SaveHandler = (SettingsSaveRequest) -> SettingsSaveOutcome" in controller
    assert "enum SettingsSaveOutcome" in controller
    assert "var onSave: SaveHandler?" in controller
    assert "var onCancelHotkeyRecording: (() -> Void)?" in controller
    assert "func windowShouldClose(_ sender: NSWindow) -> Bool" in controller
    assert "guard editorState.isDirty else" in controller
    assert "confirmDiscardBeforeClosing()" in controller
    assert "editorState.cancel()" in controller
    assert "editorState.canSave(isBusy: isBusy)" in controller
    assert "let result = onSave(request)" in controller
    assert "switch result" in controller
    assert "case .success(let authoritative):" in controller
    assert "case .partial(let authoritative, let error):" in controller
    assert "case .failure(let error):" in controller
    assert '"Could not save settings: \\(error.localizedDescription)"' in controller
    assert '"Some settings were saved, but Launch at Login could not be updated: ' in controller
    assert "cancelActiveHotkeyRecording()" in controller
    assert "hotkeyRecorderSession.finish(generation: generation)" in controller
    assert 'alert.messageText = "Discard Unsaved Changes?"' in controller
    assert 'alert.addButton(withTitle: "Keep Editing")' in controller
    assert 'alert.addButton(withTitle: "Discard Changes")' in controller
    assert "alert.beginSheetModal(for: window)" in controller
    assert "self.allowConfirmedClose = true" in controller


def test_settings_window_and_menu_share_the_central_busy_policy() -> None:
    controller = _read_swift("SettingsWindowController.swift")
    status_controller = _read_swift("StatusController.swift")
    app_state = _read_swift("AppState.swift")

    assert "var blocksSettingsChanges: Bool" in app_state
    assert (
        "case .recording, .preloading, .transcribing, .postprocessing, "
        ".repairingBackend:"
    ) in app_state
    assert "state.blocksSettingsChanges" in status_controller
    assert "microphoneMenuItem.isEnabled = enabled" in status_controller

    enabled_state = controller[
        controller.index("private func refreshEnabledState()"):
        controller.index("private func clearSaveError()")
    ]
    assert "let runtimeControlsEnabled = !isBusy && !isSaving" in enabled_state
    assert "microphonePopup" in enabled_state
    assert "control.isEnabled = runtimeControlsEnabled" in enabled_state
    assert "launchAtLoginCheckbox.isEnabled = !isSaving" in enabled_state
    assert "editorState.canSave(isBusy: isBusy)" in enabled_state
    assert "setArrangedView(busyMessageLabel, visible: isBusy)" in enabled_state

    assert "var onRequestAudioInputDevices: (() -> [AudioInputDevice])?" in controller
    assert "func windowDidBecomeKey(_ notification: Notification)" in controller
    assert "refreshAudioInputDevices()" in controller


def test_settings_window_supports_keyboard_voiceover_and_resizing() -> None:
    controller = _read_swift("SettingsWindowController.swift")

    assert "styleMask: [.titled, .closable, .miniaturizable, .resizable]" in controller
    assert "window.contentMinSize = Layout.minimumContentSize" in controller
    assert "window.setFrameUsingName(Layout.frameAutosaveName)" in controller
    assert "window.setFrameAutosaveName(" in controller
    assert "shouldCenterWindowOnFirstShow" in controller
    assert "minimumHeight: Layout.generalSectionMinimumHeight" in controller
    assert "buttonRow.bottomAnchor.constraint(" in controller
    assert "func windowDidResize(_ notification: Notification)" in controller
    assert "window.contentMinSize = NSSize(" in controller
    assert "updateWindowSizeConstraints()" in controller
    assert 'cancelButton.keyEquivalent = "\\u{1b}"' in controller
    assert 'saveButton.keyEquivalent = "\\r"' in controller
    assert "popup.setAccessibilityLabel(label)" in controller
    assert "button.setAccessibilityLabel(label)" in controller
    assert "window.setAccessibilityLabel(" in controller
    for identifier in (
        "settings.window",
        "settings.primaryHotkey",
        "settings.recordPrimaryHotkey",
        "settings.submitHotkey",
        "settings.recordSubmitHotkey",
        "settings.engine",
        "settings.model",
        "settings.language",
        "settings.microphone",
        "settings.outputMode",
        "settings.silenceAutoStop",
        "settings.launchAtLogin",
        "settings.launchAtLoginMessage",
        "settings.busyMessage",
        "settings.validationMessage",
        "settings.save",
        "settings.cancel",
    ):
        assert f'= "{identifier}"' in controller


def test_app_delegate_is_the_authoritative_runtime_and_persistence_boundary() -> None:
    app_delegate = _read_swift("AppDelegate.swift")

    assert "private var settingsWindowController: SettingsWindowController?" in app_delegate
    assert "statusController.onOpenSettings = { [weak self] in self?.showSettings() }" in app_delegate
    assert "if let settingsWindowController" in app_delegate
    assert "settingsWindowController = newController" in app_delegate
    assert "controller.showSettings()" in app_delegate
    assert "newController.onSave = { [weak self] request in" in app_delegate
    assert "return self.applySettingsWindowRequest(request)" in app_delegate
    assert "newController.onRecordPrimaryHotkey = { [weak self] current, completion in" in app_delegate
    assert "self?.recordSettingsPrimaryHotkey(" in app_delegate
    assert "newController.onRecordSubmitHotkey = { [weak self] current, completion in" in app_delegate
    assert "self?.recordSettingsSubmitHotkey(" in app_delegate
    assert "newController.onCancelHotkeyRecording = { [weak self] in" in app_delegate
    assert "self?.closeHotkeyRecorders()" in app_delegate
    assert "newController.onRequestAudioInputDevices = {" in app_delegate

    assert "private func synchronizeSettingsWindow()" in app_delegate
    assert "settingsWindowController.synchronize(" in app_delegate
    assert "authoritativeSettings: settings" in app_delegate
    assert "audioInputDevices: AudioDeviceManager.inputDevices()" in app_delegate
    assert "isBusy: state.blocksSettingsChanges" in app_delegate

    assert "private func applySettingsWindowRequest(" in app_delegate
    assert "if request.runtimeSettingsChanged" in app_delegate
    assert "try applyRuntimeSettings(request.settings)" in app_delegate
    assert "if request.launchAtLoginChanged" in app_delegate
    assert "try applyLaunchAtLogin(request.launchAtLoginEnabled)" in app_delegate
    assert "let authoritative = AuthoritativeSettings(" in app_delegate
    assert "return .success(authoritative)" in app_delegate
    assert "return .partial(" in app_delegate
    assert "return .failure(error)" in app_delegate

    assert (
        "private func applyRuntimeSettings(_ proposedSettings: SettingsSnapshot) "
        "throws -> Bool"
    ) in app_delegate
    assert "editor.normalizeDraft()" in app_delegate
    assert "if let issue = editor.validationIssue" in app_delegate
    assert "guard !state.blocksSettingsChanges || !editor.runtimeSettingsChanged else" in app_delegate
    assert "try replaceHotkeyRegistrations(" in app_delegate
    assert "settings = canonicalSettings" in app_delegate
    assert "saveSettingsOnly()" in app_delegate
    assert "switch editor.applyPlan" in app_delegate
    assert "restartBackendForModelChange()" in app_delegate
    assert "preloadSelectedModel()" in app_delegate

    assert "private func applyLaunchAtLogin(_ enabled: Bool) throws" in app_delegate
    assert "try loginItemManager.setEnabled(enabled)" in app_delegate
    assert "_ = launchAtLoginState.didApply(enabled: enabled)" in app_delegate
    assert "statusController.updateLaunchAtLogin(status: requestedStatus)" in app_delegate
    assert "private var launchAtLoginState = LoginItemStatusState()" in app_delegate
    assert "private func reconcileLaunchAtLoginFailure(" in app_delegate
    assert "requestedEnabled: Bool" in app_delegate
    assert "launchAtLoginState.didFail(" in app_delegate

    save_only = app_delegate[
        app_delegate.index("private func saveSettingsOnly()"):
        app_delegate.index("private func saveSettingsAndPreloadSelectedModel()")
    ]
    assert "settingsStore.save(settings)" in save_only
    assert "statusController.updateSettings(registry: registry, settings: settings)" in save_only
    assert "synchronizeSettingsWindow()" in save_only
    assert "settingsWindowController?.updateBusyState(newState.blocksSettingsChanges)" in app_delegate

    controller = _read_swift("SettingsWindowController.swift")
    assert "func updateBusyState(_ isBusy: Bool)" in controller
    assert "self.isBusy = isBusy" in controller
    assert "refreshEnabledState()" in controller

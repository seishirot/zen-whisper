import AppKit
import ApplicationServices
import Foundation

@MainActor
final class StatusController: NSObject, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let statusMenuItem = NSMenuItem(title: "Starting", action: nil, keyEquivalent: "")
    private let toggleMenuItem = NSMenuItem(title: "Start Recording", action: #selector(toggleRecording), keyEquivalent: "")
    private let primaryRecoveryMenuItem = NSMenuItem(title: "", action: #selector(primaryRecovery), keyEquivalent: "")
    private let retryPreloadMenuItem = NSMenuItem(title: "Retry Model Load", action: #selector(retryPreload), keyEquivalent: "")
    private let repairMenuItem = NSMenuItem(title: "Repair Backend", action: #selector(repairBackend), keyEquivalent: "")
    private let acceptSignatureMenuItem = NSMenuItem(title: "Accept Signature Change", action: #selector(acceptSignatureChange), keyEquivalent: "")
    private let retryMicMenuItem = NSMenuItem(title: "Retry Microphone", action: #selector(recoverMicrophone), keyEquivalent: "")
    private let copyDiagnosticsMenuItem = NSMenuItem(title: "Copy Diagnostics", action: #selector(copyDiagnostics), keyEquivalent: "")
    private let troubleshootingMenuItem = NSMenuItem(title: "Troubleshooting", action: nil, keyEquivalent: "")
    private let languageMenuItem = NSMenuItem(title: "Language", action: nil, keyEquivalent: "")
    private let recognitionModelMenuItem = NSMenuItem(title: "Recognition Model", action: nil, keyEquivalent: "")
    private let hotkeyMenuItem = NSMenuItem(title: "Hotkey", action: nil, keyEquivalent: "")
    private let submitHotkeyMenuItem = NSMenuItem(title: "Submit Hotkey", action: nil, keyEquivalent: "")
    private let silenceAutoStopMenuItem = NSMenuItem(title: "Auto-stop on Silence", action: #selector(toggleSilenceAutoStop), keyEquivalent: "")
    private let outputModeMenuItem = NSMenuItem(title: "Output", action: nil, keyEquivalent: "")
    private let microphoneMenuItem = NSMenuItem(title: "Microphone", action: nil, keyEquivalent: "")
    private let launchAtLoginMenuItem = NSMenuItem(title: "Launch at Login", action: #selector(toggleLaunchAtLogin), keyEquivalent: "")
    private var currentState: AppState = .idle
    private var animationTimer: Timer?
    private var animationFrame = 0
    private var registry: ModelRegistry?
    private var settings: SettingsSnapshot?
    private var launchAtLoginEnabled = false
    private var primaryRecoveryAction: RecoveryAction?
    private var microphoneRecoveryAction: RecoveryAction?

    private enum RecoveryAction {
        case retryPreload
        case repairBackend
        case acceptSignatureChange
        case retryMicrophone
        case openMicrophoneSettings
        case openAccessibilitySettings
    }

    var onToggleRecording: (() -> Void)?
    var onRetryPreload: (() -> Void)?
    var onRepairBackend: (() -> Void)?
    var onAcceptSignatureChange: (() -> Void)?
    var onRetryMicrophone: (() -> Void)?
    var onSelectLanguage: ((String) -> Void)?
    var onSelectModel: ((String, String) -> Void)?
    var onSelectHotkey: ((HotkeyShortcut) -> Void)?
    var onRecordCustomHotkey: (() -> Void)?
    var onSelectSubmitHotkey: ((HotkeyShortcut?) -> Void)?
    var onRecordCustomSubmitHotkey: (() -> Void)?
    var onToggleSilenceAutoStop: ((Bool) -> Void)?
    var onSelectOutputMode: ((OutputMode) -> Void)?
    var onSelectMicrophone: ((String?) -> Void)?
    var onToggleLaunchAtLogin: ((Bool) -> Void)?
    var onMenuWillOpen: (() -> Void)?
    var onOpenLogs: (() -> Void)?
    var onCopyDiagnostics: (() -> Void)?
    var onQuit: (() -> Void)?

    override init() {
        super.init()
        configureMenu()
        menu.delegate = self
        statusItem.menu = menu
        update(state: .idle)
    }

    func menuWillOpen(_ menu: NSMenu) {
        onMenuWillOpen?()
        rebuildSettingsMenus()
    }

    func updateSettings(registry: ModelRegistry, settings: SettingsSnapshot) {
        self.registry = registry
        self.settings = settings
        rebuildSettingsMenus()
        applySettingsEnabledState()
    }

    func updateLaunchAtLogin(enabled: Bool) {
        launchAtLoginEnabled = enabled
        launchAtLoginMenuItem.state = enabled ? .on : .off
    }

    func update(state: AppState) {
        currentState = state
        renderStatusItem()
        updateAnimationTimer(for: state)
        statusMenuItem.title = menuText(for: state)
        toggleMenuItem.title = toggleTitle(for: state)
        toggleMenuItem.isEnabled = state.canToggleRecording
        retryPreloadMenuItem.isEnabled = canRetryPreload(state)
        repairMenuItem.isEnabled = canRepairBackend(state)
        acceptSignatureMenuItem.title = acceptSignatureTitle(for: state)
        acceptSignatureMenuItem.isEnabled = canAcceptSignatureChange(state)
        updateMicrophoneRecovery(for: state)
        updatePrimaryRecovery(for: state)
        applySettingsEnabledState()
    }

    private func renderStatusItem() {
        let frame = isAnimated(currentState) ? animationFrame : 0
        let title = currentState.title
        statusItem.button?.image = StatusIconFactory.image(for: currentState, animationFrame: frame)
        statusItem.button?.attributedTitle = statusTitle(title)
        statusItem.button?.imagePosition = title.isEmpty ? .imageOnly : .imageLeft
        statusItem.button?.toolTip = "zen-whisper"
    }

    private func statusTitle(_ title: String) -> NSAttributedString {
        guard !title.isEmpty else {
            return NSAttributedString(string: "")
        }
        return NSAttributedString(
            string: " \(title)",
            attributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .regular),
                .foregroundColor: NSColor.labelColor,
                .baselineOffset: -1.0
            ]
        )
    }

    private func updateAnimationTimer(for state: AppState) {
        guard isAnimated(state) else {
            animationTimer?.invalidate()
            animationTimer = nil
            animationFrame = 0
            return
        }
        guard animationTimer == nil else {
            return
        }
        let timer = Timer(timeInterval: 0.12, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.animationFrame = (self.animationFrame + 1) % 24
                self.renderStatusItem()
            }
        }
        animationTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func isAnimated(_ state: AppState) -> Bool {
        switch state {
        case .preloading, .transcribing, .repairingBackend:
            return true
        case .idle, .inputWaiting, .pasteUnavailable, .recording, .copied, .copySkipped, .copyFailed, .modelUnavailable,
             .backendRepairRequired, .microphoneError, .hotkeyError, .appSignatureChanged, .error:
            return false
        }
    }

    private func configureMenu() {
        menu.autoenablesItems = false
        statusMenuItem.isEnabled = false
        toggleMenuItem.target = self
        menu.addItem(toggleMenuItem)
        menu.addItem(statusMenuItem)

        primaryRecoveryMenuItem.target = self
        primaryRecoveryMenuItem.isHidden = true
        menu.addItem(primaryRecoveryMenuItem)

        menu.addItem(.separator())
        menu.addItem(languageMenuItem)
        menu.addItem(recognitionModelMenuItem)
        menu.addItem(hotkeyMenuItem)
        menu.addItem(submitHotkeyMenuItem)

        silenceAutoStopMenuItem.target = self
        menu.addItem(silenceAutoStopMenuItem)
        menu.addItem(outputModeMenuItem)
        menu.addItem(microphoneMenuItem)
        launchAtLoginMenuItem.target = self
        launchAtLoginMenuItem.state = launchAtLoginEnabled ? .on : .off
        menu.addItem(launchAtLoginMenuItem)

        menu.addItem(.separator())
        troubleshootingMenuItem.submenu = troubleshootingMenu()
        menu.addItem(troubleshootingMenuItem)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        rebuildSettingsMenus()
    }

    private func troubleshootingMenu() -> NSMenu {
        let submenu = NSMenu()
        submenu.autoenablesItems = false
        retryPreloadMenuItem.target = self
        submenu.addItem(retryPreloadMenuItem)

        repairMenuItem.target = self
        submenu.addItem(repairMenuItem)

        acceptSignatureMenuItem.target = self
        submenu.addItem(acceptSignatureMenuItem)

        retryMicMenuItem.target = self
        submenu.addItem(retryMicMenuItem)

        submenu.addItem(.separator())
        copyDiagnosticsMenuItem.target = self
        submenu.addItem(copyDiagnosticsMenuItem)
        let openMic = NSMenuItem(title: "Open Microphone Settings", action: #selector(openMicrophoneSettings), keyEquivalent: "")
        openMic.target = self
        submenu.addItem(openMic)
        let openAX = NSMenuItem(title: "Open Accessibility Settings", action: #selector(openAccessibilitySettings), keyEquivalent: "")
        openAX.target = self
        submenu.addItem(openAX)
        let openLogs = NSMenuItem(title: "Open Logs", action: #selector(openLogs), keyEquivalent: "")
        openLogs.target = self
        submenu.addItem(openLogs)
        return submenu
    }

    private func rebuildSettingsMenus() {
        guard let registry, let settings else {
            languageMenuItem.title = "Language"
            languageMenuItem.isEnabled = false
            recognitionModelMenuItem.title = "Recognition Model"
            recognitionModelMenuItem.isEnabled = false
            hotkeyMenuItem.title = "Hotkey"
            hotkeyMenuItem.isEnabled = false
            submitHotkeyMenuItem.title = "Submit Hotkey"
            submitHotkeyMenuItem.isEnabled = false
            silenceAutoStopMenuItem.isEnabled = false
            silenceAutoStopMenuItem.state = .off
            outputModeMenuItem.title = "Output"
            outputModeMenuItem.isEnabled = false
            microphoneMenuItem.title = "Microphone: System Default"
            microphoneMenuItem.submenu = microphoneMenu(selectedUID: nil)
            launchAtLoginMenuItem.state = launchAtLoginEnabled ? .on : .off
            return
        }

        let engine = registry.engine(settings.engine)
        let language = registry.validLanguage(settings.language, for: engine.id)
        let selectedLanguageLabel = registry.languages[language]?.label ?? language
        languageMenuItem.title = "Language: \(selectedLanguageLabel)"
        languageMenuItem.submenu = menu(
            items: registry.supportedLanguages(for: engine.id),
            selected: language,
            action: #selector(selectLanguage(_:))
        )

        let model = registry.validModel(settings.lastModelByEngine[engine.id], for: engine.id)
        let modelLabel = engine.models.first(where: { $0.id == model })?.label ?? model
        let selectedModelKey = modelMenuKey(engineID: engine.id, modelID: model)
        let modelItems = recognitionModelMenuItems(from: registry)
        let selectedModelLabel = modelItems.first(where: { $0.id == selectedModelKey })?.label ?? modelLabel
        recognitionModelMenuItem.title = "Recognition Model: \(selectedModelLabel)"
        recognitionModelMenuItem.submenu = menu(
            items: modelItems,
            selected: selectedModelKey,
            action: #selector(selectModel(_:))
        )

        hotkeyMenuItem.title = "Hotkey: \(settings.hotkey.label)"
        hotkeyMenuItem.submenu = hotkeyMenu(selected: settings.hotkey)

        submitHotkeyMenuItem.title = settings.submitHotkey.map { "Submit Hotkey: \($0.label)" } ?? "Submit Hotkey: Off"
        submitHotkeyMenuItem.submenu = submitHotkeyMenu(selected: settings.submitHotkey)

        silenceAutoStopMenuItem.state = settings.silenceAutoStopEnabled ? .on : .off
        outputModeMenuItem.title = "Output: \(settings.outputMode.label)"
        outputModeMenuItem.submenu = menu(
            items: OutputMode.allCases.map { (id: $0.rawValue, label: $0.label) },
            selected: settings.outputMode.rawValue,
            representedObject: { rawValue in OutputMode(rawValue: rawValue) ?? .pasteRestoreClipboard },
            action: #selector(selectOutputMode(_:))
        )
        let devices = AudioDeviceManager.inputDevices()
        let storedUID = settings.microphoneDeviceUID?.isEmpty == true ? nil : settings.microphoneDeviceUID
        if let storedUID,
           let selectedDevice = devices.first(where: { $0.uid == storedUID }) {
            microphoneMenuItem.title = "Microphone: \(selectedDevice.name)"
        } else if storedUID != nil {
            microphoneMenuItem.title = "Microphone: Unavailable"
        } else {
            microphoneMenuItem.title = "Microphone: System Default"
        }
        microphoneMenuItem.submenu = microphoneMenu(selectedUID: storedUID, devices: devices)
        applySettingsEnabledState()
    }

    private func applySettingsEnabledState() {
        let enabled = registry != nil && settings != nil && !isBusy(currentState)
        languageMenuItem.isEnabled = enabled
        recognitionModelMenuItem.isEnabled = enabled
        hotkeyMenuItem.isEnabled = enabled
        submitHotkeyMenuItem.isEnabled = enabled
        silenceAutoStopMenuItem.isEnabled = enabled
        outputModeMenuItem.isEnabled = enabled
        microphoneMenuItem.isEnabled = settings != nil
        launchAtLoginMenuItem.isEnabled = true
        launchAtLoginMenuItem.state = launchAtLoginEnabled ? .on : .off
    }

    private func modelMenuKey(engineID: String, modelID: String) -> String {
        "\(engineID)\u{1f}\(modelID)"
    }

    private func recognitionModelMenuItems(from registry: ModelRegistry) -> [(id: String, label: String)] {
        let entries: [(engine: ModelRegistry.Engine, model: ModelRegistry.Engine.Model)] = registry.engines.flatMap { engine in
            engine.models.map { model in (engine: engine, model: model) }
        }
        return entries.map { entry in
            let label = "\(entry.model.label) (\(entry.engine.label))"
            return (id: modelMenuKey(engineID: entry.engine.id, modelID: entry.model.id), label: label)
        }
    }

    private func hotkeyMenu(selected: HotkeyShortcut) -> NSMenu {
        let submenu = NSMenu()
        submenu.autoenablesItems = false
        for shortcut in HotkeyShortcut.presets {
            let item = NSMenuItem(title: shortcut.label, action: #selector(selectHotkey(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = shortcut.storageValue
            item.state = shortcut == selected ? .on : .off
            submenu.addItem(item)
        }
        if !HotkeyShortcut.presets.contains(selected) {
            submenu.addItem(.separator())
            let customItem = NSMenuItem(title: "Custom: \(selected.label)", action: nil, keyEquivalent: "")
            customItem.state = .on
            customItem.isEnabled = false
            submenu.addItem(customItem)
        }
        submenu.addItem(.separator())
        let recordItem = NSMenuItem(title: "Record Custom Shortcut...", action: #selector(recordCustomHotkey), keyEquivalent: "")
        recordItem.target = self
        submenu.addItem(recordItem)
        return submenu
    }

    private func submitHotkeyMenu(selected: HotkeyShortcut?) -> NSMenu {
        let submenu = NSMenu()
        submenu.autoenablesItems = false

        let offItem = NSMenuItem(title: "Off", action: #selector(selectSubmitHotkey(_:)), keyEquivalent: "")
        offItem.target = self
        offItem.representedObject = ""
        offItem.state = selected == nil ? .on : .off
        submenu.addItem(offItem)

        submenu.addItem(.separator())
        for shortcut in HotkeyShortcut.submitPresets {
            let item = NSMenuItem(title: shortcut.label, action: #selector(selectSubmitHotkey(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = shortcut.storageValue
            item.state = shortcut == selected ? .on : .off
            submenu.addItem(item)
        }

        if let selected, !HotkeyShortcut.submitPresets.contains(selected) {
            submenu.addItem(.separator())
            let customItem = NSMenuItem(title: "Custom: \(selected.label)", action: nil, keyEquivalent: "")
            customItem.state = .on
            customItem.isEnabled = false
            submenu.addItem(customItem)
        }

        submenu.addItem(.separator())
        let recordItem = NSMenuItem(title: "Record Custom Shortcut...", action: #selector(recordCustomSubmitHotkey), keyEquivalent: "")
        recordItem.target = self
        submenu.addItem(recordItem)
        return submenu
    }

    private func menu(
        items: [(id: String, label: String)],
        selected: String,
        representedObject: (String) -> Any = { $0 },
        action: Selector
    ) -> NSMenu {
        let submenu = NSMenu()
        submenu.autoenablesItems = false
        for item in items {
            let menuItem = NSMenuItem(title: item.label, action: action, keyEquivalent: "")
            menuItem.target = self
            menuItem.representedObject = representedObject(item.id)
            menuItem.state = item.id == selected ? .on : .off
            submenu.addItem(menuItem)
        }
        return submenu
    }

    private func microphoneMenu(
        selectedUID: String?,
        devices: [AudioInputDevice] = AudioDeviceManager.inputDevices()
    ) -> NSMenu {
        let submenu = NSMenu()
        submenu.autoenablesItems = false
        let defaultItem = NSMenuItem(title: "System Default", action: #selector(selectMicrophone(_:)), keyEquivalent: "")
        defaultItem.target = self
        defaultItem.representedObject = ""
        defaultItem.state = selectedUID == nil ? .on : .off
        submenu.addItem(defaultItem)
        if let selectedUID,
           !devices.contains(where: { $0.uid == selectedUID }) {
            let missingItem = NSMenuItem(title: "Selected device unavailable", action: nil, keyEquivalent: "")
            missingItem.state = .on
            missingItem.isEnabled = false
            submenu.addItem(missingItem)
        }
        if !devices.isEmpty {
            submenu.addItem(.separator())
        }
        for device in devices {
            let item = NSMenuItem(title: device.name, action: #selector(selectMicrophone(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = device.uid
            item.state = device.uid == selectedUID ? .on : .off
            submenu.addItem(item)
        }
        return submenu
    }

    private func menuText(for state: AppState) -> String {
        switch state {
        case .idle:
            return "Idle"
        case .inputWaiting:
            return "Input waiting"
        case .pasteUnavailable(let reason):
            return "Paste unavailable: \(reason)"
        case .recording(let elapsed, let voiceActive):
            return voiceActive
                ? "Recording \(StatusText.elapsed(elapsed)) - voice"
                : "Recording \(StatusText.elapsed(elapsed)) - listening"
        case .preloading(let message):
            return "Loading model: \(message)"
        case .transcribing:
            return "Transcribing"
        case .copied(let pasteDispatched, let reason):
            if pasteDispatched {
                let enterText: String
                if reason?.lowercased().contains("enter sent") == true {
                    enterText = "; Enter sent"
                } else if reason?.lowercased().contains("enter skipped") == true {
                    enterText = "; Enter skipped"
                } else if reason?.lowercased().contains("enter unavailable") == true {
                    enterText = "; Enter unavailable"
                } else {
                    enterText = ""
                }
                if reason?.lowercased().contains("kept") == true {
                    return "Paste sent\(enterText); clipboard kept"
                }
                if reason?.lowercased().contains("restore failed") == true {
                    return "Paste sent\(enterText); clipboard restore failed"
                }
                if reason?.lowercased().contains("restore pending") == true {
                    return "Paste sent\(enterText); clipboard restore pending"
                }
                if reason?.lowercased().contains("restored") == true {
                    return "Paste sent\(enterText); clipboard restored"
                }
                return "Paste sent\(enterText)"
            }
            if let reason, !reason.isEmpty {
                return "Copied; paste skipped: \(reason)"
            }
            return "Copied"
        case .copySkipped(let reason):
            return "Copy Skipped: \(reason)"
        case .copyFailed(let message):
            return "Copy Failed: \(StatusText.visibleErrorSummary(message))"
        case .modelUnavailable(let message):
            return "Model Not Available: \(StatusText.visibleErrorSummary(message))"
        case .backendRepairRequired(let message):
            return "Backend Repair Required: \(StatusText.visibleErrorSummary(message))"
        case .repairingBackend:
            return "Repairing Backend"
        case .microphoneError(let message):
            return "Microphone Error: \(StatusText.visibleErrorSummary(message))"
        case .hotkeyError(let message):
            return "Hotkey Error: \(StatusText.visibleErrorSummary(message))"
        case .appSignatureChanged:
            return "App Signature Changed"
        case .error(let message):
            return "Error: \(StatusText.visibleErrorSummary(message))"
        }
    }

    private func toggleTitle(for state: AppState) -> String {
        if case .recording = state {
            return "Stop Recording"
        }
        return "Start Recording"
    }

    private func canRetryPreload(_ state: AppState) -> Bool {
        switch state {
        case .modelUnavailable, .inputWaiting, .pasteUnavailable, .copied, .copySkipped, .copyFailed:
            return true
        case .idle, .recording, .preloading, .transcribing, .backendRepairRequired,
             .repairingBackend, .microphoneError, .hotkeyError, .appSignatureChanged, .error:
            return false
        }
    }

    private func canRepairBackend(_ state: AppState) -> Bool {
        if case .backendRepairRequired = state {
            return true
        }
        return false
    }

    private func canAcceptSignatureChange(_ state: AppState) -> Bool {
        guard case .appSignatureChanged = state else {
            return false
        }
        return canAcceptCurrentSignatureChange()
    }

    private func acceptSignatureTitle(for state: AppState) -> String {
        guard case .appSignatureChanged = state,
              !canAcceptCurrentSignatureChange() else {
            return "Accept Signature Change"
        }
        return "Accept Signature Change (Daily app only)"
    }

    private func canAcceptCurrentSignatureChange() -> Bool {
        AppRuntimeIdentity.currentBundlePath == AppRuntimeIdentity.dailyAppPath
    }

    private func updateMicrophoneRecovery(for state: AppState) {
        guard case .microphoneError(let message) = state else {
            microphoneRecoveryAction = nil
            retryMicMenuItem.title = "Retry Microphone"
            retryMicMenuItem.isEnabled = false
            retryMicMenuItem.isHidden = false
            return
        }
        if isMicrophonePermissionIssue(message) {
            microphoneRecoveryAction = nil
            retryMicMenuItem.title = "Retry Microphone"
            retryMicMenuItem.isEnabled = false
            retryMicMenuItem.isHidden = true
        } else {
            microphoneRecoveryAction = .retryMicrophone
            retryMicMenuItem.title = "Retry Microphone"
            retryMicMenuItem.isEnabled = true
            retryMicMenuItem.isHidden = false
        }
    }

    private func isMicrophonePermissionIssue(_ message: String) -> Bool {
        let lower = message.lowercased()
        return lower.contains("permission") || lower.contains("denied") || lower.contains("restricted")
    }

    private func updatePrimaryRecovery(for state: AppState) {
        let recovery = primaryRecoveryDescriptor(for: state)
        primaryRecoveryAction = recovery?.action
        primaryRecoveryMenuItem.title = recovery?.title ?? ""
        primaryRecoveryMenuItem.isHidden = recovery == nil
        primaryRecoveryMenuItem.isEnabled = recovery != nil
    }

    private func primaryRecoveryDescriptor(for state: AppState) -> (title: String, action: RecoveryAction)? {
        switch state {
        case .modelUnavailable:
            return ("Retry Model Load", .retryPreload)
        case .backendRepairRequired:
            return ("Repair Backend", .repairBackend)
        case .microphoneError(let message):
            if isMicrophonePermissionIssue(message) {
                return ("Open Microphone Settings", .openMicrophoneSettings)
            }
            return ("Retry Microphone", .retryMicrophone)
        case .pasteUnavailable:
            return ("Open Accessibility Settings", .openAccessibilitySettings)
        case .appSignatureChanged:
            guard canAcceptCurrentSignatureChange() else {
                return nil
            }
            return ("Accept Signature Change", .acceptSignatureChange)
        case .idle, .inputWaiting, .recording, .preloading, .transcribing, .copied,
             .copySkipped, .copyFailed, .repairingBackend, .hotkeyError, .error:
            return nil
        }
    }

    private func isBusy(_ state: AppState) -> Bool {
        switch state {
        case .recording, .preloading, .transcribing, .repairingBackend:
            return true
        case .idle, .inputWaiting, .pasteUnavailable, .copied, .copySkipped, .copyFailed, .modelUnavailable, .backendRepairRequired,
             .microphoneError, .hotkeyError, .appSignatureChanged, .error:
            return false
        }
    }

    @objc private func toggleRecording() { onToggleRecording?() }
    @objc private func primaryRecovery() {
        performRecovery(primaryRecoveryAction)
    }

    private func performRecovery(_ action: RecoveryAction?) {
        switch action {
        case .retryPreload:
            onRetryPreload?()
        case .repairBackend:
            onRepairBackend?()
        case .acceptSignatureChange:
            onAcceptSignatureChange?()
        case .retryMicrophone:
            onRetryMicrophone?()
        case .openMicrophoneSettings:
            openMicrophoneSettings()
        case .openAccessibilitySettings:
            openAccessibilitySettings()
        case nil:
            break
        }
    }
    @objc private func retryPreload() { onRetryPreload?() }
    @objc private func repairBackend() { onRepairBackend?() }
    @objc private func acceptSignatureChange() { onAcceptSignatureChange?() }
    @objc private func recoverMicrophone() { performRecovery(microphoneRecoveryAction) }
    @objc private func selectLanguage(_ sender: NSMenuItem) {
        guard let language = sender.representedObject as? String else { return }
        onSelectLanguage?(language)
    }
    @objc private func selectModel(_ sender: NSMenuItem) {
        guard let value = sender.representedObject as? String else { return }
        let parts = value.split(separator: "\u{1f}", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { return }
        onSelectModel?(parts[0], parts[1])
    }
    @objc private func selectHotkey(_ sender: NSMenuItem) {
        guard let rawValue = sender.representedObject as? String,
              let shortcut = HotkeyShortcut.parseComboString(rawValue) else { return }
        onSelectHotkey?(shortcut)
    }
    @objc private func recordCustomHotkey() { onRecordCustomHotkey?() }
    @objc private func selectSubmitHotkey(_ sender: NSMenuItem) {
        guard let rawValue = sender.representedObject as? String else { return }
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            onSelectSubmitHotkey?(nil)
            return
        }
        guard let shortcut = HotkeyShortcut.optionalFromStorageValue(trimmed) else { return }
        onSelectSubmitHotkey?(shortcut)
    }
    @objc private func recordCustomSubmitHotkey() { onRecordCustomSubmitHotkey?() }
    @objc private func toggleSilenceAutoStop() {
        let enabled = silenceAutoStopMenuItem.state != .on
        onToggleSilenceAutoStop?(enabled)
    }
    @objc private func selectOutputMode(_ sender: NSMenuItem) {
        guard let mode = sender.representedObject as? OutputMode else { return }
        onSelectOutputMode?(mode)
    }
    @objc private func toggleLaunchAtLogin() {
        let enabled = launchAtLoginMenuItem.state != .on
        onToggleLaunchAtLogin?(enabled)
    }
    @objc private func selectMicrophone(_ sender: NSMenuItem) {
        guard let uid = sender.representedObject as? String else { return }
        onSelectMicrophone?(uid.isEmpty ? nil : uid)
    }
    @objc private func openLogs() { onOpenLogs?() }
    @objc private func copyDiagnostics() { onCopyDiagnostics?() }
    @objc private func quit() { onQuit?() }

    @objc private func openMicrophoneSettings() {
        openSystemSettings(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
            label: "Microphone Settings"
        )
    }

    @objc private func openAccessibilitySettings() {
        let options = [
            kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true
        ] as CFDictionary
        AXIsProcessTrustedWithOptions(options)
        openSystemSettings(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            label: "Accessibility Settings"
        )
    }

    private func openSystemSettings(_ urlString: String, label: String) {
        guard let url = URL(string: urlString),
              NSWorkspace.shared.open(url) else {
            NSLog("zen-whisper: failed to open %@", label)
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Could Not Open \(label)"
            alert.informativeText = urlString
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }
    }
}

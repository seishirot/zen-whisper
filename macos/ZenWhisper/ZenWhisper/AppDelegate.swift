import AppKit
import AVFoundation
import Foundation

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let paths = AppPaths.live
    private var registry: ModelRegistry!
    private var settingsStore: SettingsStore!
    private var settings: SettingsSnapshot!
    private var statusController: StatusController!
    private var backend: BackendClient!
    private var recorder: AudioRecorder!
    private var appLogger: AppLogger?
    private let pasteController = PasteController()
    private let hotkeyManager = HotkeyManager()
    private let submitHotkeyManager = HotkeyManager(signature: HotkeyManager.submitSignature)
    private let loginItemManager = LoginItemManager()
    private var hotkeyRecorder: HotkeyRecorderWindowController?
    private var submitHotkeyRecorder: HotkeyRecorderWindowController?
    private var submitAfterPasteForCurrentRecording = false
    private var state: AppState = .idle
    private var timer: Timer?
    private var statusResetTimer: Timer?
    private var pasteTargetCacheTimer: Timer?
    private var pasteTargetAtRecordingStart: PasteTargetSnapshot?
    private var lastKnownPasteTarget: PasteTargetSnapshot?
    private var lastKnownPasteTargetDate: Date?
    private var startupDiagnostics: [String] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        do {
            try paths.prepare()
            appLogger = AppLogger(logsDirectory: paths.logs)
            registry = try ModelRegistry.loadDefault()
            settingsStore = SettingsStore(registry: registry)
            settings = settingsStore.load()
            backend = BackendClient(paths: paths)
            recorder = AudioRecorder(paths: paths)
            statusController = StatusController()
            wireMenu()
            statusController.updateSettings(registry: registry, settings: settings)
            refreshLaunchAtLoginState()
            try registerHotkey()
            verifySignatureAndStartBackend()
        } catch {
            let message = "startup failed: \(String(describing: error))"
            startupDiagnostics.append(message)
            logInfo(message)
            if statusController == nil {
                statusController = StatusController()
                wireMenu()
            }
            if registry != nil, settings != nil {
                statusController.updateSettings(registry: registry, settings: settings)
            }
            if let startupError = error as? AppStartupError,
               case .hotkeyRegistration(let label, let reason) = startupError {
                setState(.hotkeyError("Hotkey registration failed for \(label): \(reason)"))
            } else {
                setState(.error(String(describing: error)))
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        statusResetTimer?.invalidate()
        pasteTargetCacheTimer?.invalidate()
        recorder?.cancel()
        backend?.stop()
    }

    private func wireMenu() {
        statusController.onToggleRecording = { [weak self] in self?.toggleRecording() }
        statusController.onRetryPreload = { [weak self] in self?.preloadSelectedModel() }
        statusController.onRepairBackend = { [weak self] in self?.repairBackend() }
        statusController.onAcceptSignatureChange = { [weak self] in self?.acceptSignatureChange() }
        statusController.onRetryMicrophone = { [weak self] in self?.toggleRecording() }
        statusController.onSelectLanguage = { [weak self] language in self?.selectLanguage(language) }
        statusController.onSelectModel = { [weak self] engine, model in self?.selectModel(engine: engine, model: model) }
        statusController.onSelectHotkey = { [weak self] shortcut in self?.selectHotkey(shortcut) }
        statusController.onRecordCustomHotkey = { [weak self] in self?.recordCustomHotkey() }
        statusController.onSelectSubmitHotkey = { [weak self] shortcut in self?.selectSubmitHotkey(shortcut) }
        statusController.onRecordCustomSubmitHotkey = { [weak self] in self?.recordCustomSubmitHotkey() }
        statusController.onToggleSilenceAutoStop = { [weak self] enabled in self?.setSilenceAutoStop(enabled) }
        statusController.onSelectOutputMode = { [weak self] mode in self?.selectOutputMode(mode) }
        statusController.onSelectMicrophone = { [weak self] uid in self?.selectMicrophone(uid) }
        statusController.onToggleLaunchAtLogin = { [weak self] enabled in self?.setLaunchAtLogin(enabled) }
        statusController.onMenuWillOpen = { [weak self] in
            self?.rememberPasteTarget(stage: "menu open")
            self?.refreshLaunchAtLoginState()
        }
        statusController.onOpenLogs = { [weak self] in
            guard let self else { return }
            guard NSWorkspace.shared.open(self.paths.logs) else {
                self.logInfo("open logs failed: \(self.paths.logs.path)")
                self.showOpenFailureAlert(
                    title: "Could Not Open Logs",
                    message: self.paths.logs.path
                )
                return
            }
        }
        statusController.onCopyDiagnostics = { [weak self] in self?.copyDiagnostics() }
        statusController.onQuit = { NSApp.terminate(nil) }
    }

    private func registerHotkey() throws {
        do {
            try hotkeyManager.register(shortcut: settings.hotkey) { [weak self] in
                self?.toggleRecording(submitAfterPaste: false)
            }
        } catch {
            throw AppStartupError.hotkeyRegistration(settings.hotkey.label, String(describing: error))
        }
        do {
            try registerSubmitHotkeyIfNeeded()
        } catch {
            let label = settings.submitHotkey.map { "Submit \($0.label)" } ?? "Submit Hotkey"
            throw AppStartupError.hotkeyRegistration(label, String(describing: error))
        }
    }

    private func registerSubmitHotkeyIfNeeded() throws {
        guard let submitHotkey = settings.submitHotkey else {
            submitHotkeyManager.unregister()
            return
        }
        try submitHotkeyManager.register(shortcut: submitHotkey) { [weak self] in
            self?.toggleRecording(submitAfterPaste: true)
        }
    }

    private func recoverFromHotkeyErrorIfReady() {
        guard case .hotkeyError = state else {
            return
        }
        do {
            if !hotkeyManager.hasActiveRegistration {
                try hotkeyManager.register(shortcut: settings.hotkey) { [weak self] in
                    self?.toggleRecording(submitAfterPaste: false)
                }
            }
            if settings.submitHotkey != nil, !submitHotkeyManager.hasActiveRegistration {
                try registerSubmitHotkeyIfNeeded()
            }
            verifySignatureAndStartBackend()
        } catch {
            setState(.hotkeyError("Hotkey registration failed: \(error)"))
        }
    }

    private func verifySignatureAndStartBackend() {
        let status = SignatureValidator(
            appSupport: paths.appSupport
        ).validateCurrentApp()
        switch status {
        case .valid, .notDailyBundle:
            startBackend()
        case .missingBaseline, .changed:
            setState(.appSignatureChanged)
        }
    }

    private func startBackend() {
        let validator = BackendInstallValidator(paths: paths, bundleURL: Bundle.main.bundleURL)
        setState(.preloading(message: "backend"))
        let backend = self.backend!
        DispatchQueue.global(qos: .userInitiated).async {
            switch validator.validateInstallMetadata() {
            case .valid:
                break
            case .invalid(let reason):
                DispatchQueue.main.async {
                    self.logInfo("backend install metadata invalid: \(reason)")
                    self.setState(.backendRepairRequired(reason))
                }
                return
            }
            do {
                try backend.start()
                let health = try backend.waitForHealth()
                switch validator.validateHealth(health) {
                case .valid:
                    break
                case .invalid(let reason):
                    throw BackendValidationError.invalid(reason)
                }
                DispatchQueue.main.async {
                    self.preloadSelectedModel()
                }
            } catch {
                backend.stop()
                DispatchQueue.main.async {
                    self.logInfo("backend start failed: \(error)")
                    self.setState(.backendRepairRequired(String(describing: error)))
                }
            }
        }
    }

    private func preloadSelectedModel() {
        let engine = settings.engine
        let model = registry.validModel(settings.lastModelByEngine[engine], for: engine)
        let language = settings.language
        let backend = self.backend!
        setState(.preloading(message: model))
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                try backend.preload(engine: engine, model: model, language: language)
                DispatchQueue.main.async {
                    self.setState(self.readyState())
                }
            } catch {
                DispatchQueue.main.async {
                    self.setBackendOperationError(error)
                }
            }
        }
    }

    private func toggleRecording(submitAfterPaste: Bool = false) {
        if recorder.isRecording {
            let shouldSubmitAfterPaste = submitAfterPasteForCurrentRecording || submitAfterPaste
            submitAfterPasteForCurrentRecording = shouldSubmitAfterPaste
            stopRecordingAndTranscribe(submitAfterPaste: shouldSubmitAfterPaste)
            return
        }
        guard state.canStartRecording else {
            return
        }
        submitAfterPasteForCurrentRecording = submitAfterPaste
        startRecording()
    }

    private func startRecording() {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            beginRecording()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async {
                    granted ? self.beginRecording() : self.setState(.microphoneError("Permission denied"))
                }
            }
        case .denied, .restricted:
            setState(.microphoneError("Permission denied"))
        @unknown default:
            setState(.microphoneError("Unknown microphone permission state"))
        }
    }

    private func beginRecording() {
        do {
            pasteTargetAtRecordingStart = capturePasteTarget(stage: "recording start", allowCached: true)
            try recorder.start(deviceUID: settings.microphoneDeviceUID)
            setState(.recording(elapsed: 0, voiceActive: false))
            timer?.invalidate()
            let recordingTimer = Timer(timeInterval: 0.2, repeats: true) { [weak self] timer in
                Task { @MainActor in
                    self?.refreshRecordingTimer(timer)
                }
            }
            timer = recordingTimer
            RunLoop.main.add(recordingTimer, forMode: .common)
        } catch {
            setState(.microphoneError(String(describing: error)))
        }
    }

    @objc private func refreshRecordingTimer(_ timer: Timer) {
        guard recorder.isRecording else {
            timer.invalidate()
            return
        }
        let level = recorder.levelSnapshot()
        if level.reachedMaxDuration || (settings.silenceAutoStopEnabled && level.shouldAutoStop) {
            stopRecordingAndTranscribe(submitAfterPaste: submitAfterPasteForCurrentRecording)
            return
        }
        setState(.recording(elapsed: level.elapsed, voiceActive: level.voiceActive))
    }

    private func stopRecordingAndTranscribe(submitAfterPaste: Bool = false) {
        timer?.invalidate()
        submitAfterPasteForCurrentRecording = false
        let stopTarget = capturePasteTarget(stage: "recording stop", allowCached: false)
        do {
            let recording = try recorder.stop()
            if recording.isEmptyAudio {
                logInfo("recording skipped as empty audio rms=\(recording.rms) peak=\(recording.peak)")
                try? FileManager.default.removeItem(at: recording.url)
                setCopySkippedTransient("empty audio")
                return
            }
            let audioURL = recording.url
            setState(.transcribing)
            let startTarget = pasteTargetAtRecordingStart
            let engine = settings.engine
            let model = registry.validModel(settings.lastModelByEngine[engine], for: engine)
            let language = settings.language
            let backend = self.backend!
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let text = try backend.transcribe(
                        audioURL: audioURL,
                        engine: engine,
                        model: model,
                        language: language
                    )
                    try? FileManager.default.removeItem(at: audioURL)
                    DispatchQueue.main.async {
                        self.handleTranscript(
                            text,
                            startTarget: startTarget,
                            stopTarget: stopTarget,
                            submitAfterPaste: submitAfterPaste
                        )
                    }
                } catch {
                    try? FileManager.default.removeItem(at: audioURL)
                    DispatchQueue.main.async {
                        self.setBackendOperationError(error)
                    }
                }
            }
        } catch {
            setState(.microphoneError(String(describing: error)))
        }
    }

    private func handleTranscript(
        _ text: String,
        startTarget: PasteTargetSnapshot?,
        stopTarget: PasteTargetSnapshot?,
        submitAfterPaste: Bool = false
    ) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            logInfo("transcript empty; leaving pasteboard unchanged")
            setState(readyState())
            return
        }
        if [startTarget, stopTarget].compactMap({ $0 }).contains(where: { pasteController.isUnsafeForClipboard($0) }) {
            logInfo(
                "paste decision skip-copy before AX check: target is unsafe start=\(startTarget?.redactedDescription ?? "nil") stop=\(stopTarget?.redactedDescription ?? "nil")"
            )
            setCopySkippedTransient("target is unsafe")
            return
        }
        guard pasteController.isAccessibilityTrusted() else {
            copyTranscriptWithoutPaste(trimmed, reason: "Accessibility not allowed")
            return
        }
        guard settings.outputMode.shouldAttemptPaste else {
            if let current = capturePasteTarget(stage: "copy-only output check", allowCached: false),
               pasteController.isUnsafeForClipboard(current) {
                logInfo("copy-only output skipped: target is unsafe current=\(current.redactedDescription)")
                setCopySkippedTransient("target is unsafe")
                return
            }
            logInfo("output mode copy-only; paste skipped")
            copyTranscriptWithoutPaste(trimmed, reason: "output mode copy only")
            return
        }
        let current = capturePasteTarget(stage: "transcription complete", allowCached: false)
        let decision = pasteController.decide(
            start: startTarget,
            stop: stopTarget,
            current: current
        )
        switch decision {
        case .paste:
            guard pasteController.canCreatePasteEvents() else {
                logInfo("paste event unavailable before pasteboard write")
                copyTranscriptWithoutPaste(trimmed, reason: "paste event unavailable")
                return
            }
            guard let restoreToken = pasteController.copyForAutoPaste(trimmed) else {
                setCopyFailedTransient("pasteboard write failed")
                return
            }
            if let pid = current?.pid, pasteController.paste(to: pid) {
                let pasteReason: String
                if settings.outputMode.restoresClipboardAfterPaste {
                    pasteReason = "clipboard restore pending"
                    pasteController.scheduleRestore(restoreToken, after: 1.0) { [weak self] restored in
                        guard let self else {
                            return
                        }
                        if restored {
                            self.logInfo("clipboard restored after paste")
                        } else {
                            self.logInfo("clipboard restore failed after paste")
                            if case .copied = self.state {
                                self.setCopiedTransient(pasteDispatched: true, reason: "clipboard restore failed")
                            }
                        }
                    }
                } else {
                    pasteReason = "clipboard kept"
                }
                if let current {
                    logInfo(
                        "paste event posted to target: \(current.redactedDescription) submitAfterPaste=\(submitAfterPaste)"
                    )
                }
                setCopiedTransient(pasteDispatched: true, reason: pasteReason)
                if submitAfterPaste, let current {
                    scheduleSubmitReturn(to: current, pasteReason: pasteReason)
                }
            } else {
                logInfo("paste event unavailable for target: \(current?.redactedDescription ?? "nil")")
                if settings.outputMode.restoresClipboardAfterPaste {
                    guard pasteController.restore(restoreToken) else {
                        setCopiedTransient(pasteDispatched: false, reason: "paste event unavailable; clipboard restore failed")
                        return
                    }
                }
                setCopiedTransient(pasteDispatched: false, reason: "paste event unavailable")
            }
        case .copyOnly(let reason):
            logInfo(
                "paste decision copy-only: \(reason) start=\(startTarget?.redactedDescription ?? "nil") stop=\(stopTarget?.redactedDescription ?? "nil") current=\(current?.redactedDescription ?? "nil")"
            )
            copyTranscriptWithoutPaste(trimmed, reason: reason)
        case .skipCopy(let reason):
            logInfo(
                "paste decision skip-copy: \(reason) start=\(startTarget?.redactedDescription ?? "nil") stop=\(stopTarget?.redactedDescription ?? "nil") current=\(current?.redactedDescription ?? "nil")"
            )
            setCopySkippedTransient(reason)
        }
    }

    private func scheduleSubmitReturn(to approvedTarget: PasteTargetSnapshot, pasteReason: String) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self else {
                return
            }
            let current = self.capturePasteTarget(stage: "submit return", allowCached: false)
            guard self.pasteController.decide(
                start: approvedTarget,
                stop: approvedTarget,
                current: current
            ) == .paste else {
                self.logInfo(
                    "submit return skipped: target changed approved=\(approvedTarget.redactedDescription) current=\(current?.redactedDescription ?? "nil")"
                )
                self.setCopiedTransient(pasteDispatched: true, reason: "\(pasteReason); enter skipped")
                return
            }
            if self.pasteController.pressReturn(to: approvedTarget.pid) {
                self.logInfo("submit return posted to target: \(approvedTarget.redactedDescription)")
                self.setCopiedTransient(pasteDispatched: true, reason: "\(pasteReason); enter sent")
            } else {
                self.logInfo("submit return unavailable for target: \(approvedTarget.redactedDescription)")
                self.setCopiedTransient(pasteDispatched: true, reason: "\(pasteReason); enter unavailable")
            }
        }
    }

    private func copyTranscriptWithoutPaste(_ text: String, reason: String) {
        guard pasteController.copy(text) else {
            setCopyFailedTransient("pasteboard write failed")
            return
        }
        setCopiedTransient(pasteDispatched: false, reason: reason)
    }

    private func setCopiedTransient(pasteDispatched: Bool, reason: String?) {
        if pasteDispatched {
            logInfo("transcript copied; paste sent: \(reason ?? "unknown")")
        } else {
            logInfo("transcript copied; paste skipped: \(reason ?? "unknown")")
        }
        setState(.copied(pasteDispatched: pasteDispatched, reason: reason))
        statusResetTimer?.invalidate()
        let resetTimer = Timer(timeInterval: 1.2, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, case .copied = self.state else {
                    return
                }
                self.setState(self.readyState())
            }
        }
        statusResetTimer = resetTimer
        RunLoop.main.add(resetTimer, forMode: .common)
    }

    private func setCopySkippedTransient(_ reason: String) {
        logInfo("transcript copy skipped: \(reason)")
        setState(.copySkipped(reason))
        statusResetTimer?.invalidate()
        let resetTimer = Timer(timeInterval: 1.2, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, case .copySkipped = self.state else {
                    return
                }
                self.setState(self.readyState())
            }
        }
        statusResetTimer = resetTimer
        RunLoop.main.add(resetTimer, forMode: .common)
    }

    private func setCopyFailedTransient(_ reason: String) {
        logInfo("transcript copy failed: \(reason)")
        setState(.copyFailed(reason))
        statusResetTimer?.invalidate()
        let resetTimer = Timer(timeInterval: 1.6, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, case .copyFailed = self.state else {
                    return
                }
                self.setState(self.readyState())
            }
        }
        statusResetTimer = resetTimer
        RunLoop.main.add(resetTimer, forMode: .common)
    }

    private func rememberPasteTarget(stage: String) {
        _ = capturePasteTarget(stage: stage, allowCached: false)
    }

    private func capturePasteTarget(stage: String, allowCached: Bool) -> PasteTargetSnapshot? {
        let probe = pasteController.snapshotFocusedTargetProbe()
        if let snapshot = probe.snapshot {
            cachePasteTargetIfEligible(snapshot, stage: stage, log: true)
            return snapshot
        }
        if allowCached, let snapshot = recentCachedPasteTarget() {
            logInfo("paste target reused at \(stage): \(snapshot.redactedDescription)")
            return snapshot
        }
        logInfo("paste target missing at \(stage): \(probe.detail)")
        return nil
    }

    private func refreshPasteTargetCache(stage: String) {
        guard let snapshot = pasteController.snapshotFocusedTargetProbe().snapshot else {
            return
        }
        cachePasteTargetIfEligible(snapshot, stage: stage, log: false)
    }

    private func cachePasteTargetIfEligible(
        _ snapshot: PasteTargetSnapshot,
        stage: String,
        log: Bool
    ) {
        guard pasteController.isEligible(snapshot) else {
            if log {
                logInfo("paste target captured but ineligible at \(stage): \(snapshot.redactedDescription)")
            }
            return
        }
        let changed = lastKnownPasteTarget != snapshot
        lastKnownPasteTarget = snapshot
        lastKnownPasteTargetDate = Date()
        if log || changed {
            logInfo("paste target cached at \(stage): \(snapshot.redactedDescription)")
        }
    }

    private func recentCachedPasteTarget(maxAge: TimeInterval = 20) -> PasteTargetSnapshot? {
        guard let snapshot = lastKnownPasteTarget,
              let date = lastKnownPasteTargetDate,
              Date().timeIntervalSince(date) <= maxAge else {
            return nil
        }
        return snapshot
    }

    private func selectLanguage(_ language: String) {
        settings.language = registry.validLanguage(language, for: settings.engine)
        saveSettingsAndPreloadSelectedModel()
    }

    private func selectModel(engine: String, model: String) {
        let previousEngine = settings.engine
        let previousModel = registry.validModel(settings.lastModelByEngine[previousEngine], for: previousEngine)
        let engine = registry.validEngine(engine)
        let model = registry.validModel(model, for: engine)
        let selectionChanged = engine != previousEngine || model != previousModel
        settings.engine = engine
        settings.language = registry.validLanguage(settings.language, for: engine)
        settings.lastModelByEngine[engine] = model
        if selectionChanged {
            saveSettingsAndRestartBackendForModelChange()
        } else {
            saveSettingsOnly()
        }
    }

    private func selectHotkey(_ shortcut: HotkeyShortcut) {
        guard shortcut != settings.hotkey else {
            return
        }
        if let submitHotkey = settings.submitHotkey, shortcut == submitHotkey {
            showHotkeyRegistrationAlert(
                title: "Could Not Register Hotkey",
                message: "\(shortcut.label) is already used by Submit Hotkey."
            )
            return
        }
        let previous = settings.hotkey
        do {
            try hotkeyManager.register(shortcut: shortcut) { [weak self] in
                self?.toggleRecording(submitAfterPaste: false)
            }
            settings.hotkey = shortcut
            saveSettingsOnly()
            recoverFromHotkeyErrorIfReady()
        } catch {
            settings.hotkey = previous
            statusController.updateSettings(registry: registry, settings: settings)
            if !hotkeyManager.hasActiveRegistration {
                do {
                    try hotkeyManager.register(shortcut: previous) { [weak self] in
                        self?.toggleRecording(submitAfterPaste: false)
                    }
                } catch {
                    setState(.hotkeyError("Could not restore \(previous.label): \(error)"))
                    return
                }
            }
            showHotkeyRegistrationAlert(
                title: "Could Not Register Hotkey",
                message: "\(shortcut.label) is unavailable. Keeping \(previous.label)."
            )
        }
    }

    private func recordCustomHotkey() {
        closeHotkeyRecorders()
        suspendHotkeysForRecorder()
        let recorder = HotkeyRecorderWindowController(
            title: "Record Hotkey",
            instruction: "Press the shortcut to start or stop recording. Use Ctrl, Option, or Cmd. Shift+Space is also allowed.",
            currentShortcut: settings.hotkey
        ) { [weak self] shortcut in
            guard let self else {
                return
            }
            self.hotkeyRecorder = nil
            guard let shortcut else {
                self.resumeHotkeysAfterRecorder()
                return
            }
            if shortcut == self.settings.hotkey {
                self.resumeHotkeysAfterRecorder()
                return
            }
            self.selectHotkey(shortcut)
            self.resumeHotkeysAfterRecorder()
        }
        hotkeyRecorder = recorder
        recorder.showRecorder()
    }

    private func selectSubmitHotkey(_ shortcut: HotkeyShortcut?) {
        guard shortcut != settings.submitHotkey else {
            return
        }
        if let shortcut, shortcut == settings.hotkey {
            showHotkeyRegistrationAlert(
                title: "Could Not Register Submit Hotkey",
                message: "\(shortcut.label) is already used by Hotkey."
            )
            return
        }
        let previous = settings.submitHotkey
        guard let shortcut else {
            submitHotkeyManager.unregister()
            settings.submitHotkey = nil
            saveSettingsOnly()
            recoverFromHotkeyErrorIfReady()
            return
        }
        do {
            try submitHotkeyManager.register(shortcut: shortcut) { [weak self] in
                self?.toggleRecording(submitAfterPaste: true)
            }
            settings.submitHotkey = shortcut
            saveSettingsOnly()
            recoverFromHotkeyErrorIfReady()
        } catch {
            settings.submitHotkey = previous
            statusController.updateSettings(registry: registry, settings: settings)
            if !submitHotkeyManager.hasActiveRegistration, let previous {
                do {
                    try submitHotkeyManager.register(shortcut: previous) { [weak self] in
                        self?.toggleRecording(submitAfterPaste: true)
                    }
                } catch {
                    setState(.hotkeyError("Could not restore submit \(previous.label): \(error)"))
                    return
                }
            }
            showHotkeyRegistrationAlert(
                title: "Could Not Register Submit Hotkey",
                message: "\(shortcut.label) is unavailable. Keeping \(previous?.label ?? "Off")."
            )
        }
    }

    private func recordCustomSubmitHotkey() {
        closeHotkeyRecorders()
        suspendHotkeysForRecorder()
        let current = settings.submitHotkey ?? HotkeyShortcut.controlOptionCommandReturn
        let recorder = HotkeyRecorderWindowController(
            title: "Record Submit Hotkey",
            instruction: "Press the shortcut to record, paste, and send Return after paste. Use Ctrl, Option, or Cmd.",
            currentShortcut: current
        ) { [weak self] shortcut in
            guard let self else {
                return
            }
            self.submitHotkeyRecorder = nil
            guard let shortcut else {
                self.resumeHotkeysAfterRecorder()
                return
            }
            if let submitHotkey = self.settings.submitHotkey, shortcut == submitHotkey {
                self.resumeHotkeysAfterRecorder()
                return
            }
            self.selectSubmitHotkey(shortcut)
            self.resumeHotkeysAfterRecorder()
        }
        submitHotkeyRecorder = recorder
        recorder.showRecorder()
    }

    private func closeHotkeyRecorders() {
        hotkeyRecorder?.close()
        submitHotkeyRecorder?.close()
        hotkeyRecorder = nil
        submitHotkeyRecorder = nil
    }

    private func suspendHotkeysForRecorder() {
        hotkeyManager.suspend()
        submitHotkeyManager.suspend()
    }

    private func resumeHotkeysAfterRecorder() {
        resumePrimaryHotkeyAfterRecorder()
        resumeSubmitHotkeyAfterRecorder()
    }

    private func resumePrimaryHotkeyAfterRecorder() {
        do {
            try hotkeyManager.resume()
        } catch {
            setState(.hotkeyError("Could not restore \(settings.hotkey.label): \(error)"))
        }
    }

    private func resumeSubmitHotkeyAfterRecorder() {
        guard settings.submitHotkey != nil else {
            return
        }
        do {
            try submitHotkeyManager.resume()
        } catch {
            setState(.hotkeyError("Could not restore submit \(settings.submitHotkey?.label ?? "hotkey"): \(error)"))
        }
    }

    private func showHotkeyRegistrationAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    private func showOpenFailureAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    private func setSilenceAutoStop(_ enabled: Bool) {
        settings.silenceAutoStopEnabled = enabled
        saveSettingsOnly()
    }

    private func selectOutputMode(_ mode: OutputMode) {
        guard settings != nil else {
            return
        }
        settings.outputMode = mode
        saveSettingsOnly()
    }

    private func selectMicrophone(_ uid: String?) {
        guard settings != nil else {
            return
        }
        settings.microphoneDeviceUID = uid?.isEmpty == true ? nil : uid
        saveSettingsOnly()
    }

    private func refreshLaunchAtLoginState() {
        statusController?.updateLaunchAtLogin(enabled: loginItemManager.isEnabled())
    }

    private func setLaunchAtLogin(_ enabled: Bool) {
        do {
            try loginItemManager.setEnabled(enabled)
            statusController.updateLaunchAtLogin(enabled: enabled)
            logInfo("launch at login \(enabled ? "enabled" : "disabled")")
        } catch {
            refreshLaunchAtLoginState()
            logInfo("launch at login change failed: \(error.localizedDescription)")
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Could Not Update Launch at Login"
            alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }
    }

    private func saveSettingsOnly() {
        settings.engine = registry.validEngine(settings.engine)
        settings.language = registry.validLanguage(settings.language, for: settings.engine)
        settings.lastModelByEngine = registry.coerceModels(settings.lastModelByEngine)
        settingsStore.save(settings)
        statusController.updateSettings(registry: registry, settings: settings)
    }

    private func saveSettingsAndPreloadSelectedModel() {
        saveSettingsOnly()
        switch state {
        case .inputWaiting, .pasteUnavailable, .modelUnavailable, .copied, .copySkipped, .copyFailed:
            preloadSelectedModel()
        case .idle, .recording, .preloading, .transcribing, .backendRepairRequired,
             .repairingBackend, .microphoneError, .hotkeyError, .appSignatureChanged, .error:
            break
        }
    }

    private func saveSettingsAndRestartBackendForModelChange() {
        saveSettingsOnly()
        switch state {
        case .inputWaiting, .pasteUnavailable, .modelUnavailable, .copied, .copySkipped, .copyFailed, .microphoneError:
            restartBackendForModelChange()
        case .idle, .recording, .preloading, .transcribing, .backendRepairRequired,
             .repairingBackend, .hotkeyError, .appSignatureChanged, .error:
            break
        }
    }

    private func restartBackendForModelChange() {
        logInfo("restarting backend after recognition model change")
        setState(.preloading(message: "backend"))
        let backend = self.backend!
        DispatchQueue.global(qos: .userInitiated).async {
            guard backend.stop() else {
                DispatchQueue.main.async {
                    self.logInfo("model change restart failed because backend did not stop")
                    self.setState(.backendRepairRequired("Backend did not stop. See logs."))
                }
                return
            }
            DispatchQueue.main.async {
                self.startBackend()
            }
        }
    }

    private func repairBackend() {
        guard case .backendRepairRequired = state else {
            return
        }
        logInfo("backend repair requested")
        guard backend?.stop() != false else {
            logInfo("backend repair aborted because the existing backend did not stop")
            setState(.backendRepairRequired("Backend did not stop. See logs."))
            return
        }
        setState(.repairingBackend)
        let runner = BackendRepairRunner(paths: paths, bundleURL: Bundle.main.bundleURL)
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                try runner.repair()
                DispatchQueue.main.async {
                    self.logInfo("backend repair completed")
                    self.startBackend()
                }
            } catch {
                DispatchQueue.main.async {
                    self.logInfo("backend repair failed: \(error)")
                    self.setState(.backendRepairRequired(Self.visibleRepairFailureMessage(error)))
                }
            }
        }
    }

    private func copyDiagnostics() {
        let appLog = paths.logs.appendingPathComponent("app.log")
        let repairLog = paths.logs.appendingPathComponent("backend-repair.log")
        let diagnostics = [
            "zen-whisper diagnostics",
            "startup issues:",
            startupDiagnostics.joined(separator: "\n"),
            "app.log:",
            Self.processOutput(from: appLog),
            "backend-repair.log:",
            Self.processOutput(from: repairLog),
            "backend-startup.log:",
            Self.processOutput(from: paths.backendStartupLog)
        ].joined(separator: "\n")
        guard pasteController.copy(diagnostics) else {
            setCopyFailedTransient("diagnostics pasteboard write failed")
            return
        }
        logInfo("diagnostics copied to pasteboard")
        setCopiedTransient(pasteDispatched: false, reason: "diagnostics")
    }

    private func acceptSignatureChange() {
        guard case .appSignatureChanged = state else {
            return
        }
        guard AppRuntimeIdentity.currentBundlePath == AppRuntimeIdentity.dailyAppPath else {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Cannot Accept Signature Here"
            alert.informativeText = """
            Signature acceptance is only available for the Daily app at \(AppRuntimeIdentity.dailyAppPath). \
            Current app path: \(AppRuntimeIdentity.currentBundlePath)
            """
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }

        let confirmation = NSAlert()
        confirmation.alertStyle = .warning
        confirmation.messageText = "Accept App Signature Change?"
        confirmation.informativeText = """
        This updates zen-whisper's local signing baseline for \(AppRuntimeIdentity.dailyAppPath). \
        If macOS permissions stop working afterward, reset and regrant Microphone and Accessibility permissions.
        """
        confirmation.addButton(withTitle: "Accept and Continue")
        confirmation.addButton(withTitle: "Cancel")
        guard confirmation.runModal() == .alertFirstButtonReturn else {
            return
        }

        do {
            try SignatureBaselineStore.acceptCurrentDailyApp(appSupport: paths.appSupport)
            let permissions = NSAlert()
            permissions.alertStyle = .informational
            permissions.messageText = "Regrant macOS Permissions"
            permissions.informativeText = """
            Open Microphone Settings and Accessibility Settings from the zen-whisper menu if recording or paste does not work. \
            This is expected after accepting a changed app signature.
            """
            permissions.addButton(withTitle: "Continue")
            permissions.runModal()
            startBackend()
        } catch {
            logInfo("signature acceptance failed: \(error)")
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Could Not Accept Signature Change"
            alert.informativeText = "See Open Logs for details."
            alert.addButton(withTitle: "OK")
            alert.runModal()
            setState(.appSignatureChanged)
        }
    }

    private func setState(_ newState: AppState) {
        if case .copied = newState {
        } else if case .copySkipped = newState {
        } else if case .copyFailed = newState {
        } else {
            statusResetTimer?.invalidate()
            statusResetTimer = nil
        }
        state = newState
        statusController.update(state: newState)
        updatePasteTargetCacheTimer(for: newState)
    }

    private func logInfo(_ message: String) {
        appLogger?.info(message)
    }

    private func readyState() -> AppState {
        pasteController.isAccessibilityTrusted()
            ? .inputWaiting
            : .pasteUnavailable("Accessibility not allowed")
    }

    private func setBackendOperationError(_ error: Error) {
        if case BackendProtocolError.backendError(let code, let message, let recoverable) = error {
            logInfo("backend operation error code=\(code) recoverable=\(recoverable): \(message)")
            if recoverable {
                setState(.modelUnavailable(Self.visibleBackendOperationMessage(code: code, recoverable: recoverable)))
            } else {
                setState(.backendRepairRequired(Self.visibleBackendOperationMessage(code: code, recoverable: recoverable)))
            }
            return
        }
        logInfo("backend operation failed: \(error)")
        setState(.backendRepairRequired(String(describing: error)))
    }

    nonisolated private static func visibleBackendOperationMessage(code: String, recoverable: Bool) -> String {
        switch code.uppercased() {
        case "AUDIO_NOT_FOUND":
            return "Audio file missing. See logs."
        case "MODEL_NOT_AVAILABLE", "MODEL_LOAD_FAILED":
            return "Model unavailable. See logs."
        default:
            return recoverable ? "Model unavailable. See logs." : "Backend unavailable. See logs."
        }
    }

    nonisolated private static func visibleRepairFailureMessage(_ error: Error) -> String {
        if case BackendRepairRunnerError.timedOut = error {
            return "Repair timed out. See logs."
        }
        return "Repair failed. See logs."
    }

    nonisolated private static func processOutput(from url: URL) -> String {
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            return "[unavailable: \(url.path): \(error.localizedDescription)]"
        }
        guard var text = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) else {
            return "[unreadable UTF-8: \(url.path)]"
        }
        guard !text.isEmpty else {
            return "[empty]"
        }
        if text.count > 4_000 {
            text = "[truncated to last 4000 characters]\n" + String(text.suffix(4_000))
        }
        return text
    }

    private func updatePasteTargetCacheTimer(for state: AppState) {
        guard shouldPollPasteTarget(in: state),
              pasteController.isAccessibilityTrusted() else {
            pasteTargetCacheTimer?.invalidate()
            pasteTargetCacheTimer = nil
            return
        }
        guard pasteTargetCacheTimer == nil else {
            return
        }
        refreshPasteTargetCache(stage: "idle target poll")
        let cacheTimer = Timer(timeInterval: 0.75, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshPasteTargetCache(stage: "idle target poll")
            }
        }
        pasteTargetCacheTimer = cacheTimer
        RunLoop.main.add(cacheTimer, forMode: .common)
    }

    private func shouldPollPasteTarget(in state: AppState) -> Bool {
        switch state {
        case .inputWaiting, .pasteUnavailable, .copied, .copySkipped, .copyFailed:
            return true
        case .idle, .recording, .preloading, .transcribing, .modelUnavailable,
             .backendRepairRequired, .repairingBackend, .microphoneError,
             .hotkeyError, .appSignatureChanged, .error:
            return false
        }
    }
}

enum BackendRepairError: Error {
    case failed(Int32, String)
}

extension BackendRepairError: CustomStringConvertible {
    var description: String {
        switch self {
        case .failed(let status, let output):
            if output.isEmpty {
                return "failed with exit status \(status)"
            }
            return "failed with exit status \(status): \(output)"
        }
    }
}

enum BackendValidationError: Error {
    case invalid(String)
}

enum AppStartupError: Error, CustomStringConvertible {
    case hotkeyRegistration(String, String)

    var description: String {
        switch self {
        case .hotkeyRegistration(let label, let reason):
            return "Hotkey registration failed for \(label): \(reason)"
        }
    }
}

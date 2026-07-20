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
    private var pasteApplicationAtRecordingStart: PasteApplicationTarget?
    private var lastKnownPasteTarget: PasteTargetSnapshot?
    private var lastKnownPasteTargetDate: Date?
    private var startupDiagnostics: [String] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        do {
            let preparationWarnings = try paths.prepare()
            appLogger = AppLogger(logsDirectory: paths.logs)
            startupDiagnostics.append(contentsOf: preparationWarnings)
            for warning in preparationWarnings {
                logInfo(warning)
            }
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
        if let warning = recorder?.cancel() {
            startupDiagnostics.append(warning)
            logInfo(warning)
        }
        if let backend {
            let stopResult = backend.stopDetailed()
            if !stopResult.stopped {
                logInfo("backend did not stop during app termination")
            } else if !stopResult.cleanExit {
                logInfo("backend termination finished after \(stopResult.summary)")
            }
        }
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
        statusController.onToggleUnverifiedPasteFallback = { [weak self] enabled in self?.setUnverifiedPasteFallback(enabled) }
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
        case .invalidBaseline(let reason):
            logInfo("signature baseline invalid: \(reason)")
            setState(.error("Signing baseline invalid. Reinstall app."))
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
                let stopResult = backend.stopDetailed()
                DispatchQueue.main.async {
                    if stopResult.stopped, !stopResult.cleanExit {
                        self.logInfo("backend start cleanup finished after \(stopResult.summary)")
                    }
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
            pasteApplicationAtRecordingStart = capturePasteApplicationTarget(stage: "recording start")
            pasteTargetAtRecordingStart = capturePasteTarget(stage: "recording start", allowCached: true)
            try recorder.start(deviceUID: settings.microphoneDeviceUID)
            setState(.recording(elapsed: 0, voiceActive: false))
            timer?.invalidate()
            let recordingTimer = Timer(
                timeInterval: 0.2,
                target: self,
                selector: #selector(refreshRecordingTimer(_:)),
                userInfo: nil,
                repeats: true
            )
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
        let stopApplication = capturePasteApplicationTarget(stage: "recording stop")
        let stopTarget = capturePasteTarget(stage: "recording stop", allowCached: true)
        do {
            let recording = try recorder.stop()
            if recording.isEmptyAudio {
                logInfo("recording skipped as empty audio rms=\(recording.rms) peak=\(recording.peak)")
                removeRecordingFile(recording.url, context: "empty recording cleanup")
                setCopySkippedTransient("empty audio")
                return
            }
            let audioURL = recording.url
            setState(.transcribing)
            let startTarget = pasteTargetAtRecordingStart
            let startApplication = pasteApplicationAtRecordingStart
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
                    DispatchQueue.main.async {
                        self.removeRecordingFile(audioURL, context: "post-transcription recording cleanup")
                        self.handleTranscript(
                            text,
                            startTarget: startTarget,
                            stopTarget: stopTarget,
                            startApplication: startApplication,
                            stopApplication: stopApplication,
                            submitAfterPaste: submitAfterPaste
                        )
                    }
                } catch {
                    DispatchQueue.main.async {
                        self.removeRecordingFile(audioURL, context: "failed transcription recording cleanup")
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
        startApplication: PasteApplicationTarget?,
        stopApplication: PasteApplicationTarget?,
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
        let currentApplication = capturePasteApplicationTarget(stage: "transcription complete")
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
            let pasteboardWrite = pasteController.prepareAutoPaste(trimmed)
            guard case .success(let restoreToken) = pasteboardWrite else {
                if case .writeFailed(let restoreSucceeded) = pasteboardWrite, !restoreSucceeded {
                    setCopyFailedTransient("pasteboard write failed; clipboard restore failed")
                } else {
                    setCopyFailedTransient("pasteboard write failed")
                }
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
                            self.updateClipboardRestoreStatus(restored: true)
                        } else {
                            self.logInfo("clipboard restore failed after paste")
                            self.updateClipboardRestoreStatus(restored: false)
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
                    setCopySkippedTransient(Self.copySkippedReasonAfterRestoredPasteFailure("paste event unavailable"))
                    return
                }
                setCopiedTransient(pasteDispatched: false, reason: "paste event unavailable")
            }
        case .copyOnly(let reason):
            let reasonText = reason.message
            if settings.allowUnverifiedPasteFallback,
               let fallback = Self.fallbackPasteApplicationTarget(
                reason: reason,
                start: startApplication,
                stop: stopApplication,
                current: currentApplication
            ) {
                logInfo(
                    "paste decision fallback-to-frontmost-app: \(reasonText) appTarget=\(fallback.redactedDescription) appStart=\(startApplication?.redactedDescription ?? "nil") appStop=\(stopApplication?.redactedDescription ?? "nil") appCurrent=\(currentApplication?.redactedDescription ?? "nil") axStart=\(startTarget?.redactedDescription ?? "nil") axStop=\(stopTarget?.redactedDescription ?? "nil") axCurrent=\(current?.redactedDescription ?? "nil")"
                )
                pasteTranscriptToApplicationFallback(
                    trimmed,
                    target: fallback,
                    submitAfterPaste: submitAfterPaste
                )
                return
            }
            logInfo(
                "paste decision copy-only: \(reasonText) start=\(startTarget?.redactedDescription ?? "nil") stop=\(stopTarget?.redactedDescription ?? "nil") current=\(current?.redactedDescription ?? "nil")"
            )
            copyTranscriptWithoutPaste(trimmed, reason: reasonText)
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
                self.setCopiedTransient(pasteDispatched: true, reason: "\(pasteReason); enter attempted")
            } else {
                self.logInfo("submit return unavailable for target: \(approvedTarget.redactedDescription)")
                self.setCopiedTransient(pasteDispatched: true, reason: "\(pasteReason); enter unavailable")
            }
        }
    }

    private func scheduleFallbackSubmitReturn(to approvedTarget: PasteApplicationTarget, pasteReason: String) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            guard let self else {
                return
            }
            guard let frontmost = self.capturePasteApplicationTarget(stage: "fallback submit return"),
                  frontmost == approvedTarget else {
                self.logInfo("fallback submit return skipped: target changed approved=\(approvedTarget.redactedDescription)")
                self.setCopiedTransient(pasteDispatched: true, reason: "\(pasteReason); enter skipped")
                return
            }
            if self.pasteController.pressReturnToFrontmostApplication(approvedTarget: approvedTarget) {
                self.logInfo("unverified fallback submit return posted target=\(approvedTarget.redactedDescription)")
                self.setCopiedTransient(pasteDispatched: true, reason: "\(pasteReason); enter attempted")
            } else {
                self.logInfo("unverified fallback submit return unavailable target=\(approvedTarget.redactedDescription)")
                self.setCopiedTransient(pasteDispatched: true, reason: "\(pasteReason); enter unavailable")
            }
        }
    }

    private func updateClipboardRestoreStatus(restored: Bool) {
        guard case .copied(let pasteDispatched, let reason) = state, pasteDispatched else {
            return
        }
        let current = reason ?? ""
        let replacement = restored ? "clipboard restored" : "clipboard restore failed"
        let nextReason: String
        if current.localizedCaseInsensitiveContains("clipboard restore pending") {
            nextReason = current.replacingOccurrences(
                of: "clipboard restore pending",
                with: replacement,
                options: [.caseInsensitive]
            )
        } else if current.isEmpty {
            nextReason = replacement
        } else {
            nextReason = "\(replacement); \(current)"
        }
        setCopiedTransient(pasteDispatched: true, reason: nextReason)
    }

    private func copyTranscriptWithoutPaste(_ text: String, reason: String) {
        guard copyToPasteboardOrFail(text) else {
            return
        }
        setCopiedTransient(pasteDispatched: false, reason: reason)
    }

    private func copyToPasteboardOrFail(_ text: String) -> Bool {
        switch pasteController.prepareAutoPaste(text) {
        case .success:
            return true
        case .writeFailed(let restoreSucceeded):
            if restoreSucceeded {
                setCopyFailedTransient("pasteboard write failed")
            } else {
                setCopyFailedTransient("pasteboard write failed; clipboard restore failed")
            }
            return false
        }
    }

    private func pasteTranscriptToApplicationFallback(
        _ text: String,
        target: PasteApplicationTarget,
        submitAfterPaste: Bool
    ) {
        guard let verifiedTarget = capturePasteApplicationTarget(stage: "fallback paste verification"),
              verifiedTarget == target else {
            logInfo("unverified fallback paste skipped: frontmost app changed target=\(target.redactedDescription)")
            copyTranscriptWithoutPaste(text, reason: "unverified fallback target changed")
            return
        }
        guard pasteController.canCreatePasteEvents() else {
            logInfo("unverified fallback paste event unavailable before pasteboard write target=\(target.redactedDescription)")
            copyTranscriptWithoutPaste(text, reason: "paste event unavailable")
            return
        }
        let pasteboardWrite = pasteController.prepareAutoPaste(text)
        guard case .success(let restoreToken) = pasteboardWrite else {
            if case .writeFailed(let restoreSucceeded) = pasteboardWrite, !restoreSucceeded {
                setCopyFailedTransient("pasteboard write failed; clipboard restore failed")
            } else {
                setCopyFailedTransient("pasteboard write failed")
            }
            return
        }
        guard let dispatchTarget = capturePasteApplicationTarget(stage: "fallback paste dispatch"),
              dispatchTarget == target else {
            logInfo("unverified fallback paste skipped after pasteboard write: frontmost app changed target=\(target.redactedDescription)")
            if settings.outputMode.restoresClipboardAfterPaste {
                guard pasteController.restore(restoreToken) else {
                    setCopiedTransient(pasteDispatched: false, reason: "unverified fallback target changed; clipboard restore failed")
                    return
                }
                setCopySkippedTransient(
                    Self.copySkippedReasonAfterRestoredPasteFailure("unverified fallback target changed")
                )
                return
            }
            setCopiedTransient(pasteDispatched: false, reason: "unverified fallback target changed")
            return
        }
        if pasteController.pasteToFrontmostApplication(approvedTarget: target) {
            let pasteReason: String
            if settings.outputMode.restoresClipboardAfterPaste {
                pasteReason = "unverified fallback; clipboard restore pending"
                pasteController.scheduleRestore(restoreToken, after: 1.0) { [weak self] restored in
                    guard let self else {
                        return
                    }
                    if restored {
                        self.logInfo("clipboard restored after fallback paste")
                        self.updateClipboardRestoreStatus(restored: true)
                    } else {
                        self.logInfo("clipboard restore failed after fallback paste")
                        self.updateClipboardRestoreStatus(restored: false)
                    }
                }
            } else {
                pasteReason = "unverified fallback; clipboard kept"
            }
            logInfo("unverified frontmost fallback paste event posted target=\(target.redactedDescription) submitAfterPaste=\(submitAfterPaste)")
            setCopiedTransient(pasteDispatched: true, reason: pasteReason)
            if submitAfterPaste {
                scheduleFallbackSubmitReturn(to: target, pasteReason: pasteReason)
            }
        } else {
            logInfo("unverified frontmost fallback paste event unavailable target=\(target.redactedDescription)")
            if settings.outputMode.restoresClipboardAfterPaste {
                guard pasteController.restore(restoreToken) else {
                    setCopiedTransient(pasteDispatched: false, reason: "paste event unavailable; clipboard restore failed")
                    return
                }
                setCopySkippedTransient(Self.copySkippedReasonAfterRestoredPasteFailure("paste event unavailable"))
                return
            }
            setCopiedTransient(pasteDispatched: false, reason: "paste event unavailable")
        }
    }

    private func setCopiedTransient(pasteDispatched: Bool, reason: String?) {
        if pasteDispatched {
            logInfo("transcript copied; paste attempted: \(reason ?? "unknown")")
        } else {
            logInfo("transcript copied; paste skipped: \(reason ?? "unknown")")
        }
        setState(.copied(pasteDispatched: pasteDispatched, reason: reason))
        statusResetTimer?.invalidate()
        let resetTimer = Timer(
            timeInterval: 1.2,
            target: self,
            selector: #selector(resetCopiedState(_:)),
            userInfo: nil,
            repeats: false
        )
        statusResetTimer = resetTimer
        RunLoop.main.add(resetTimer, forMode: .common)
    }

    @objc private func resetCopiedState(_ timer: Timer) {
        guard case .copied = state else {
            return
        }
        setState(readyState())
    }

    private func removeRecordingFile(_ url: URL, context: String) {
        if let warning = paths.removeRecording(url, context: context) {
            startupDiagnostics.append(warning)
            logInfo(warning)
        }
    }

    private func setCopySkippedTransient(_ reason: String) {
        logInfo("transcript copy skipped: \(reason)")
        setState(.copySkipped(reason))
        statusResetTimer?.invalidate()
        let resetTimer = Timer(
            timeInterval: 1.2,
            target: self,
            selector: #selector(resetCopySkippedState(_:)),
            userInfo: nil,
            repeats: false
        )
        statusResetTimer = resetTimer
        RunLoop.main.add(resetTimer, forMode: .common)
    }

    @objc private func resetCopySkippedState(_ timer: Timer) {
        guard case .copySkipped = state else {
            return
        }
        setState(readyState())
    }

    private func setCopyFailedTransient(_ reason: String) {
        logInfo("transcript copy failed: \(reason)")
        setState(.copyFailed(reason))
        statusResetTimer?.invalidate()
        let resetTimer = Timer(
            timeInterval: 1.6,
            target: self,
            selector: #selector(resetCopyFailedState(_:)),
            userInfo: nil,
            repeats: false
        )
        statusResetTimer = resetTimer
        RunLoop.main.add(resetTimer, forMode: .common)
    }

    @objc private func resetCopyFailedState(_ timer: Timer) {
        guard case .copyFailed = state else {
            return
        }
        setState(readyState())
    }

    private func rememberPasteTarget(stage: String) {
        _ = capturePasteTarget(stage: stage, allowCached: false)
    }

    private func capturePasteApplicationTarget(stage: String) -> PasteApplicationTarget? {
        guard let app = NSWorkspace.shared.frontmostApplication else {
            logInfo("paste app target missing at \(stage): frontmost=nil")
            return nil
        }
        guard app.processIdentifier != NSRunningApplication.current.processIdentifier else {
            logInfo("paste app target missing at \(stage): frontmost=current")
            return nil
        }
        let target = PasteApplicationTarget(
            pid: app.processIdentifier,
            bundleIdentifier: app.bundleIdentifier ?? "<nil>"
        )
        logInfo("paste app target captured at \(stage): \(target.redactedDescription)")
        return target
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
              Self.shouldUseCachedPasteTarget(
                cachedDate: lastKnownPasteTargetDate,
                now: Date(),
                maxAge: maxAge,
                cachedPID: snapshot.pid,
                frontmostPID: NSWorkspace.shared.frontmostApplication?.processIdentifier,
                currentPID: NSRunningApplication.current.processIdentifier
              ) else {
            return nil
        }
        return snapshot
    }

    nonisolated static func shouldReuseCachedPasteTarget(
        cachedPID: pid_t,
        frontmostPID: pid_t?,
        currentPID: pid_t
    ) -> Bool {
        guard let frontmostPID else {
            return false
        }
        return frontmostPID == cachedPID || frontmostPID == currentPID
    }

    nonisolated static func shouldUseCachedPasteTarget(
        cachedDate: Date?,
        now: Date,
        maxAge: TimeInterval,
        cachedPID: pid_t,
        frontmostPID: pid_t?,
        currentPID: pid_t
    ) -> Bool {
        guard let cachedDate, now.timeIntervalSince(cachedDate) <= maxAge else {
            return false
        }
        return shouldReuseCachedPasteTarget(
            cachedPID: cachedPID,
            frontmostPID: frontmostPID,
            currentPID: currentPID
        )
    }

    nonisolated static func shouldContinueAfterBackendStop(_ result: BackendStopResult) -> Bool {
        result.stopped
    }

    nonisolated static func copySkippedReasonAfterRestoredPasteFailure(_ reason: String) -> String {
        "\(reason); clipboard restored"
    }

    nonisolated static func fallbackPasteApplicationTarget(
        reason: PasteCopyOnlyReason,
        start: PasteApplicationTarget?,
        stop: PasteApplicationTarget?,
        current: PasteApplicationTarget?
    ) -> PasteApplicationTarget? {
        guard reason.isMissingAXTarget,
              let start,
              let stop,
              let current,
              start == stop,
              stop == current else {
            return nil
        }
        return current
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
        let current = settings.submitHotkey ?? HotkeyShortcut.shiftCommandSpace
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

    private func setUnverifiedPasteFallback(_ enabled: Bool) {
        guard settings != nil else {
            return
        }
        settings.allowUnverifiedPasteFallback = enabled
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
        switch loginItemManager.status() {
        case .enabled:
            statusController?.updateLaunchAtLogin(enabled: true)
        case .disabled:
            statusController?.updateLaunchAtLogin(enabled: false)
        case .invalid(let reason):
            statusController?.updateLaunchAtLogin(enabled: false)
            logInfo("launch at login status unavailable: \(reason)")
        }
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
            let stopResult = backend.stopDetailed()
            guard Self.shouldContinueAfterBackendStop(stopResult) else {
                DispatchQueue.main.async {
                    self.logInfo("model change restart failed because backend did not stop")
                    self.setState(.backendRepairRequired("Backend did not stop. See logs."))
                }
                return
            }
            DispatchQueue.main.async {
                if !stopResult.cleanExit {
                    self.logInfo("model change restart continuing after \(stopResult.summary)")
                }
                self.startBackend()
            }
        }
    }

    private func repairBackend() {
        guard case .backendRepairRequired = state else {
            return
        }
        logInfo("backend repair requested")
        if let backend {
            let stopResult = backend.stopDetailed()
            guard Self.shouldContinueAfterBackendStop(stopResult) else {
                logInfo("backend repair aborted because the existing backend did not stop")
                setState(.backendRepairRequired("Backend did not stop. See logs."))
                return
            }
            if !stopResult.cleanExit {
                logInfo("backend repair continuing after \(stopResult.summary)")
            }
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
        guard copyToPasteboardOrFail(diagnostics) else {
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
        if let appLogger {
            appLogger.info(message)
        } else {
            NSLog("zen-whisper: %@", message)
        }
    }

    private func readyState() -> AppState {
        pasteController.isAccessibilityTrusted()
            ? .inputWaiting
            : .pasteUnavailable("Accessibility not allowed")
    }

    private func setBackendOperationError(_ error: Error) {
        if case BackendProtocolError.backendError(let code, let message, let recoverable) = error {
            logInfo("backend operation error code=\(code) recoverable=\(recoverable): \(message)")
            setState(Self.stateForBackendOperationError(code: code, recoverable: recoverable))
            return
        }
        logInfo("backend operation failed: \(error)")
        setState(.backendRepairRequired(String(describing: error)))
    }

    nonisolated static func stateForBackendOperationError(code: String, recoverable: Bool) -> AppState {
        let message = visibleBackendOperationMessage(code: code, recoverable: recoverable)
        if code.uppercased() == "BACKEND_SHUTTING_DOWN" {
            return .backendRepairRequired(message)
        }
        return recoverable ? .modelUnavailable(message) : .backendRepairRequired(message)
    }

    nonisolated private static func visibleBackendOperationMessage(code: String, recoverable: Bool) -> String {
        switch code.uppercased() {
        case "AUDIO_NOT_FOUND":
            return "Audio file missing. See logs."
        case "AUDIO_UNREADABLE":
            return "Audio file unreadable. See logs."
        case "BACKEND_IO_ERROR":
            return "Backend I/O error. See logs."
        case "BACKEND_SHUTTING_DOWN":
            return "Backend is shutting down. Restart zen-whisper."
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
        let cacheTimer = Timer(
            timeInterval: 0.75,
            target: self,
            selector: #selector(pollPasteTargetCache(_:)),
            userInfo: nil,
            repeats: true
        )
        pasteTargetCacheTimer = cacheTimer
        RunLoop.main.add(cacheTimer, forMode: .common)
    }

    @objc private func pollPasteTargetCache(_ timer: Timer) {
        refreshPasteTargetCache(stage: "idle target poll")
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

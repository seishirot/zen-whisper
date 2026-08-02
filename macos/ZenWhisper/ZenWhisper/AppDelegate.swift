import AppKit
import AVFoundation
import Foundation

enum RecoveryTranscriptCopyResult: Equatable {
    case empty
    case copied
    case copyFailed
}

struct UnconfirmedTranscriptRecoveryStore {
    private var transcripts: [String] = []

    var count: Int {
        transcripts.count
    }

    mutating func append(_ transcript: String) {
        transcripts.append(transcript)
    }

    mutating func copyNext(
        using copy: (String) -> Bool
    ) -> RecoveryTranscriptCopyResult {
        guard let transcript = transcripts.first else {
            return .empty
        }
        guard copy(transcript) else {
            return .copyFailed
        }
        transcripts.removeFirst()
        return .copied
    }
}

enum UnconfirmedTranscriptRecoveryPolicy {
    static func shouldRetain(for result: PasteAttemptResult) -> Bool {
        switch result {
        case .manualPasteFallback(let reason, _):
            return reason != .copyOnlyMode
        case .blocked(_, transcript: .recoveryMenu):
            return true
        case .failed(reason: .pasteboardWriteFailed):
            return true
        case .pastedVerified, .blocked, .failed:
            return false
        }
    }
}

enum PasteAttemptPresentation {
    static func state(for result: PasteAttemptResult) -> AppState? {
        switch result {
        case .pastedVerified(let clipboard, let submit):
            let reason = [
                clipboardStatusReason(clipboard),
                submitStatusReason(submit)
            ]
            .compactMap { $0 }
            .joined(separator: "; ")
            return .copied(pasteDispatched: true, reason: reason)
        case .manualPasteFallback(let reason, let availability):
            return .copied(
                pasteDispatched: false,
                reason: manualPasteReason(
                    reason,
                    availability: availability
                )
            )
        case .blocked(let reason, let transcript):
            return .copySkipped(
                blockedPasteReason(
                    reason,
                    transcript: transcript
                )
            )
        case .failed(let reason):
            switch reason {
            case .pasteboardWriteFailed(let disposition):
                return .copyFailed(
                    pasteboardWriteFailureReason(disposition)
                )
            case .superseded:
                return nil
            default:
                return .copyFailed(
                    manualPasteReason(
                        reason,
                        availability: .clipboard
                    )
                )
            }
        }
    }

    private static func clipboardStatusReason(
        _ disposition: PasteClipboardDisposition
    ) -> String {
        switch disposition {
        case .kept:
            return "clipboard kept"
        }
    }

    private static func submitStatusReason(
        _ result: PasteSubmitResult
    ) -> String? {
        switch result {
        case .notRequested:
            return nil
        case .sent:
            return "enter sent"
        case .skippedTargetChanged:
            return "enter skipped"
        case .eventUnavailable:
            return "enter unavailable"
        }
    }

    private static func manualPasteReason(
        _ reason: PasteFailureReason,
        availability: PasteManualPasteAvailability
    ) -> String {
        if availability == .recoveryMenu {
            return "paste not confirmed; newer clipboard preserved; transcript available from menu"
        }
        switch reason {
        case .copyOnlyMode:
            return "output mode copy only"
        case .accessibilityUnavailable:
            return "Accessibility not allowed"
        case .eventPermissionUnavailable:
            return "paste event unavailable"
        case .noEditableTarget:
            return "no editable target"
        case .verificationUnavailable, .verificationTimedOut:
            return "paste not confirmed; clipboard kept"
        case .pasteboardWriteFailed(let disposition):
            return pasteboardWriteFailureReason(disposition)
        case .superseded:
            return "paste not confirmed; attempt superseded; clipboard kept"
        }
    }

    static func pasteboardWriteFailureReason(
        _ disposition: PasteboardWriteFailureDisposition
    ) -> String {
        switch disposition {
        case .originalUntouched:
            return "pasteboard write failed; clipboard unchanged; transcript available from menu"
        case .originalUnavailable:
            return "pasteboard write failed after clipboard clear; original clipboard unavailable; transcript available from menu"
        case .externalChangePreserved:
            return "pasteboard write failed; external clipboard preserved; transcript available from menu"
        }
    }

    private static func blockedPasteReason(
        _ reason: PasteBlockReason,
        transcript: PasteBlockedTranscriptDisposition
    ) -> String {
        let recoverySuffix = transcript == .recoveryMenu
            ? "; transcript available from menu"
            : ""
        switch reason {
        case .unsafeTarget:
            return "target is unsafe\(recoverySuffix)"
        case .targetSafetyIndeterminate:
            return "target safety could not be verified\(recoverySuffix)"
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let paths = AppPaths.live
    private var registry: ModelRegistry!
    private var settingsStore: SettingsStore!
    private var settings: SettingsSnapshot!
    private var enhancementCatalogStore: EnhancementCatalogStore!
    private var enhancementCatalog = EnhancementCatalogSnapshot()
    private var statusController: StatusController!
    private var backend: BackendClient!
    private var recorder: AudioRecorder!
    private var appLogger: AppLogger?
    private let pasteController = PasteController()
    private var pasteAttemptCoordinator: PasteAttemptCoordinator!
    private let hotkeyManager = HotkeyManager()
    private let submitHotkeyManager = HotkeyManager(signature: HotkeyManager.submitSignature)
    private let loginItemManager = LoginItemManager()
    private var launchAtLoginState = LoginItemStatusState()
    private var launchAtLoginStatus: LoginItemStatus {
        launchAtLoginState.status
    }
    private var settingsWindowController: SettingsWindowController?
    private var hotkeyRecorder: HotkeyRecorderWindowController?
    private var submitHotkeyRecorder: HotkeyRecorderWindowController?
    private var submitAfterPasteForCurrentRecording = false
    private var state: AppState = .idle
    private var stateBeforeHotkeyError: AppState?
    private var timer: Timer?
    private var statusResetTimer: Timer?
    private var pendingEnhancementWarningMessage: String?
    private var pasteTargetCacheTimer: Timer?
    private var pasteTargetAtRecordingStart: PasteTargetSnapshot?
    private var lastKnownPasteTarget: PasteTargetSnapshot?
    private var lastKnownPasteTargetDate: Date?
    private var pasteAttemptsInProgress = 0
    private var unconfirmedTranscriptRecovery = UnconfirmedTranscriptRecoveryStore()
    private var enhancementCatalogForCurrentRecording: EnhancementCatalogSnapshot?
    private var startupDiagnostics: [String] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        do {
            let preparationWarnings = try paths.prepare()
            appLogger = AppLogger(logsDirectory: paths.logs)
            pasteAttemptCoordinator = PasteAttemptCoordinator(
                controller: pasteController,
                logger: { [weak self] message in
                    self?.logInfo(message)
                }
            )
            startupDiagnostics.append(contentsOf: preparationWarnings)
            for warning in preparationWarnings {
                logInfo(warning)
            }
            registry = try ModelRegistry.loadDefault()
            settingsStore = SettingsStore(registry: registry)
            settings = settingsStore.load()
            enhancementCatalogStore = EnhancementCatalogStore(paths: paths)
            refreshEnhancementCatalog()
            backend = BackendClient(paths: paths)
            recorder = AudioRecorder(paths: paths)
            statusController = StatusController()
            wireMenu()
            statusController.updateSettings(registry: registry, settings: settings)
            if settingsStore.didMigrateClipboardRestoreMode {
                logInfo(
                    "clipboard mode initialized to non-destructive keep mode"
                )
                DispatchQueue.main.async { [weak self] in
                    self?.showClipboardRestoreMigrationAlert()
                }
            }
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
                setHotkeyError(
                    "Hotkey registration failed for \(label): \(reason)"
                )
            } else {
                setState(.error(String(describing: error)))
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        statusResetTimer?.invalidate()
        pasteTargetCacheTimer?.invalidate()
        pasteAttemptCoordinator?.cancel()
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
        statusController.onCopyUnconfirmedTranscript = { [weak self] in
            self?.copyUnconfirmedTranscript()
        }
        statusController.onRetryPreload = { [weak self] in self?.preloadSelectedModel() }
        statusController.onRepairBackend = { [weak self] in self?.repairBackend() }
        statusController.onAcceptSignatureChange = { [weak self] in self?.acceptSignatureChange() }
        statusController.onRetryMicrophone = { [weak self] in self?.toggleRecording() }
        statusController.onOpenSettings = { [weak self] in self?.showSettings() }
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
#if DEBUG
        statusController.onTestPastePipeline = { [weak self] in
            self?.testPastePipeline()
        }
#endif
        statusController.onQuit = { NSApp.terminate(nil) }
    }

    private func showClipboardRestoreMigrationAlert() {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = "Clipboard Behavior Updated"
        alert.informativeText = "To prevent ZenWhisper from overwriting a newer clipboard value, automatic clipboard restore is unavailable. Paste mode now keeps transcripts on the clipboard after a verified paste."
        alert.addButton(withTitle: "OK")
        alert.runModal()
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
            let transaction = HotkeyPairRegistrationTransaction(
                primaryManager: hotkeyManager,
                submitManager: submitHotkeyManager
            )
            try transaction.reconcile(
                with: settings,
                primaryAction: { [weak self] in
                    self?.toggleRecording(submitAfterPaste: false)
                },
                submitAction: { [weak self] in
                    self?.toggleRecording(submitAfterPaste: true)
                }
            )
            let previousState = stateBeforeHotkeyError
            stateBeforeHotkeyError = nil
            if backend.isRunning {
                if let previousState,
                   previousState.shouldRestoreAfterHotkeyRecovery {
                    setState(previousState)
                } else {
                    setState(readyState())
                }
            } else if let previousState,
                      previousState.shouldRestoreAfterHotkeyRecoveryWithoutBackend {
                setState(previousState)
            } else {
                verifySignatureAndStartBackend()
            }
        } catch {
            setHotkeyError("Hotkey registration failed: \(error)")
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
            pendingEnhancementWarningMessage = nil
            refreshEnhancementCatalog()
            enhancementCatalogForCurrentRecording = enhancementCatalog
            synchronizeSettingsWindow()
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
            enhancementCatalogForCurrentRecording = nil
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
        let transcriptionCatalog = enhancementCatalogForCurrentRecording ?? enhancementCatalog
        enhancementCatalogForCurrentRecording = nil
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
            let engine = settings.engine
            let model = registry.validModel(settings.lastModelByEngine[engine], for: engine)
            let language = settings.language
            let enhancementRequest = EnhancementRequestResolver.resolve(
                selection: settings.enhancement,
                catalog: transcriptionCatalog
            )
            if let warningCode = enhancementRequest.warningCode {
                logInfo("enhancement request warning code=\(warningCode)")
            }
            let shouldSubmitAfterPaste = Self.shouldSubmitAfterPaste(
                requested: submitAfterPaste,
                cliWasSelected: enhancementRequest.cliWasSelected
            )
            let profileHadRecognitionHints = enhancementRequest.profile.map {
                !$0.context.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || !$0.terms.isEmpty
            } ?? false
            let backend = self.backend!
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let result = try backend.transcribe(
                        audioURL: audioURL,
                        engine: engine,
                        model: model,
                        language: language,
                        profile: enhancementRequest.profile,
                        postprocessor: enhancementRequest.postprocessor,
                        onProgress: { progress in
                            DispatchQueue.main.async {
                                guard let nextState = Self.state(
                                    for: progress,
                                    currentState: self.state
                                ) else {
                                    return
                                }
                                self.setState(nextState)
                            }
                        }
                    )
                    DispatchQueue.main.async {
                        self.removeRecordingFile(audioURL, context: "post-transcription recording cleanup")
                        self.logEnhancementResult(
                            result.enhancement,
                            postprocessor: enhancementRequest.postprocessor
                        )
                        self.handleTranscript(
                            result.text,
                            recordingAnchor: startTarget,
                            submitAfterPaste: shouldSubmitAfterPaste
                        )
                        let warningCode = Self.preferredEnhancementWarningCode(
                            requestWarningCode: enhancementRequest.warningCode,
                            backendWarningCode: result.enhancement.warningCode,
                            engine: engine,
                            profileHadRecognitionHints: profileHadRecognitionHints,
                            hintsApplied: result.hintsApplied
                        )
                        if let warningCode {
                            self.enqueueEnhancementWarning(code: warningCode)
                        }
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
        recordingAnchor: PasteTargetSnapshot?,
        submitAfterPaste: Bool = false
    ) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            logInfo("transcript empty; leaving pasteboard unchanged")
            setState(readyState())
            return
        }
        let request = PasteRequest(
            text: trimmed,
            outputMode: settings.outputMode,
            submitAfterPaste: submitAfterPaste,
            recordingAnchor: recordingAnchor
        )
        performPasteRequest(request)
    }

    private func performPasteRequest(_ request: PasteRequest) {
        guard let pasteAttemptCoordinator else {
            setCopyFailedTransient("paste coordinator unavailable")
            return
        }
        pasteAttemptsInProgress += 1
        if state.canStartRecording {
            setState(.transcribing)
        }
        Task { @MainActor [weak self] in
            guard let self else {
                return
            }
            let report = await pasteAttemptCoordinator.perform(request)
            self.pasteAttemptsInProgress = max(
                0,
                self.pasteAttemptsInProgress - 1
            )
            if report.result == .failed(reason: .superseded) {
                self.logInfo(
                    "paste attempt result ignored because a newer attempt superseded it"
                )
                return
            }
            self.applyPasteAttemptReport(
                report,
                transcript: request.text
            )
        }
    }

#if DEBUG
    private func testPastePipeline() {
        guard state.canStartRecording else {
            logInfo("debug paste pipeline test skipped while app is busy")
            return
        }
        let marker = UUID().uuidString.prefix(8)
        performPasteRequest(
            PasteRequest(
                text: "ZenWhisper paste test \(marker)",
                outputMode: .pasteRestoreClipboard,
                submitAfterPaste: false,
                recordingAnchor: recentCachedPasteTarget()
            )
        )
    }
#endif

    private func copyToPasteboardOrFail(_ text: String) -> Bool {
        switch pasteController.copyPlainText(text) {
        case .copied:
            return true
        case .writeFailed(let disposition):
            setCopyFailedTransient(
                PasteAttemptPresentation.pasteboardWriteFailureReason(
                    disposition
                )
            )
            return false
        }
    }

    private func copyUnconfirmedTranscript() {
        let result = unconfirmedTranscriptRecovery.copyNext { [weak self] text in
            self?.copyToPasteboardOrFail(text) ?? false
        }
        statusController.setUnconfirmedTranscriptCount(
            unconfirmedTranscriptRecovery.count
        )
        guard result == .copied else {
            return
        }
        setCopiedTransient(
            pasteDispatched: false,
            reason: "unconfirmed transcript copied"
        )
    }

    private func applyPasteAttemptReport(
        _ report: PasteAttemptReport,
        transcript: String
    ) {
        if UnconfirmedTranscriptRecoveryPolicy.shouldRetain(for: report.result) {
            retainUnconfirmedTranscript(transcript)
        }
        guard let state = PasteAttemptPresentation.state(
            for: report.result
        ) else {
            return
        }
        switch state {
        case .copied(let pasteDispatched, let reason):
            setCopiedTransient(
                pasteDispatched: pasteDispatched,
                reason: reason
            )
        case .copySkipped(let reason):
            setCopySkippedTransient(reason)
        case .copyFailed(let reason):
            setCopyFailedTransient(reason)
        default:
            assertionFailure(
                "PasteAttemptPresentation returned an unsupported state"
            )
        }
    }

    private func retainUnconfirmedTranscript(_ transcript: String) {
        unconfirmedTranscriptRecovery.append(transcript)
        statusController.setUnconfirmedTranscriptCount(
            unconfirmedTranscriptRecovery.count
        )
    }

    private func setCopiedTransient(pasteDispatched: Bool, reason: String?) {
        if pasteDispatched {
            logInfo("transcript copied; paste verified: \(reason ?? "unknown")")
        } else {
            logInfo("paste not verified: \(reason ?? "unknown")")
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
        presentPendingEnhancementWarningOrReady()
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
        presentPendingEnhancementWarningOrReady()
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
        presentPendingEnhancementWarningOrReady()
    }

    private func enqueueEnhancementWarning(code: String) {
        let message = Self.visibleEnhancementWarningMessage(code: code)
        logInfo("showing enhancement fallback warning")
        if pasteAttemptsInProgress > 0 {
            pendingEnhancementWarningMessage = message
            return
        }
        switch state {
        case .copied, .copySkipped, .copyFailed:
            pendingEnhancementWarningMessage = message
        default:
            pendingEnhancementWarningMessage = nil
            setEnhancementWarningTransient(message)
        }
    }

    private func presentPendingEnhancementWarningOrReady() {
        guard let message = pendingEnhancementWarningMessage else {
            setState(readyState())
            return
        }
        pendingEnhancementWarningMessage = nil
        setEnhancementWarningTransient(message)
    }

    private func setEnhancementWarningTransient(_ message: String) {
        setState(.enhancementWarning(message))
        statusResetTimer?.invalidate()
        let resetTimer = Timer(
            timeInterval: 2.4,
            target: self,
            selector: #selector(resetEnhancementWarningState(_:)),
            userInfo: nil,
            repeats: false
        )
        statusResetTimer = resetTimer
        RunLoop.main.add(resetTimer, forMode: .common)
    }

    @objc private func resetEnhancementWarningState(_ timer: Timer) {
        guard case .enhancementWarning = state else {
            return
        }
        setState(readyState())
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

    private func showSettings() {
        guard registry != nil, settings != nil else {
            showOpenFailureAlert(
                title: "Settings Are Unavailable",
                message: "zen-whisper has not finished loading its settings."
            )
            return
        }
        rememberPasteTarget(stage: "settings open")
        refreshLaunchAtLoginState()
        refreshEnhancementCatalog()

        let controller: SettingsWindowController
        if let settingsWindowController {
            controller = settingsWindowController
        } else {
            let newController = SettingsWindowController(
                registry: registry,
                settings: settings,
                launchAtLoginStatus: launchAtLoginStatus,
                enhancementCatalog: enhancementCatalog
            )
            newController.onSave = { [weak self] request in
                guard let self else {
                    return .failure(SettingsApplicationError.appUnavailable)
                }
                return self.applySettingsWindowRequest(request)
            }
            newController.onRecordPrimaryHotkey = { [weak self] current, completion in
                self?.recordSettingsPrimaryHotkey(
                    current: current,
                    completion: completion
                )
            }
            newController.onRecordSubmitHotkey = { [weak self] current, completion in
                self?.recordSettingsSubmitHotkey(
                    current: current,
                    completion: completion
                )
            }
            newController.onCancelHotkeyRecording = { [weak self] in
                self?.closeHotkeyRecorders()
            }
            newController.onRequestAudioInputDevices = {
                AudioDeviceManager.inputDevices()
            }
            newController.onSaveProfile = { [weak self] profile, fingerprint in
                guard let self, let store = self.enhancementCatalogStore else {
                    return .failure(SettingsApplicationError.appUnavailable)
                }
                guard !self.state.blocksSettingsChanges else {
                    return .failure(SettingsApplicationError.busy)
                }
                do {
                    _ = try store.saveProfile(
                        profile,
                        expectedFingerprint: fingerprint
                    )
                    self.refreshEnhancementCatalog()
                    return .success(self.enhancementCatalog)
                } catch {
                    return .failure(error)
                }
            }
            newController.onSavePostprocessor = {
                [weak self] preset, fingerprint, explicitlyReclassified in
                guard let self, let store = self.enhancementCatalogStore else {
                    return .failure(SettingsApplicationError.appUnavailable)
                }
                guard !self.state.blocksSettingsChanges else {
                    return .failure(SettingsApplicationError.busy)
                }
                do {
                    _ = try store.savePostprocessor(
                        preset,
                        expectedFingerprint: fingerprint,
                        destinationWasExplicitlyReclassified:
                            explicitlyReclassified
                    )
                    self.refreshEnhancementCatalog()
                    return .success(self.enhancementCatalog)
                } catch {
                    return .failure(error)
                }
            }
            settingsWindowController = newController
            controller = newController
        }

        synchronizeSettingsWindow()
        controller.showSettings()
    }

    private func synchronizeSettingsWindow() {
        guard let settingsWindowController,
              registry != nil,
              settings != nil else {
            return
        }
        settingsWindowController.synchronize(
            authoritativeSettings: settings,
            launchAtLoginStatus: launchAtLoginStatus,
            audioInputDevices: AudioDeviceManager.inputDevices(),
            isBusy: state.blocksSettingsChanges,
            enhancementCatalog: enhancementCatalog
        )
    }

    private func applySettingsWindowRequest(
        _ request: SettingsSaveRequest
    ) -> SettingsSaveOutcome {
        var runtimeSettingsApplied = false
        do {
            if request.runtimeSettingsChanged {
                runtimeSettingsApplied = try applyRuntimeSettings(request.settings)
            }
            if request.launchAtLoginChanged {
                try applyLaunchAtLogin(request.launchAtLoginEnabled)
            }
            let authoritative = AuthoritativeSettings(
                settings: settings,
                launchAtLoginStatus: launchAtLoginStatus
            )
            synchronizeSettingsWindow()
            return .success(authoritative)
        } catch {
            if request.launchAtLoginChanged {
                reconcileLaunchAtLoginFailure(
                    error,
                    requestedEnabled: request.launchAtLoginEnabled
                )
            }
            synchronizeSettingsWindow()
            logInfo("settings window save failed: \(error.localizedDescription)")
            if runtimeSettingsApplied {
                return .partial(
                    AuthoritativeSettings(
                        settings: settings,
                        launchAtLoginStatus: launchAtLoginStatus
                    ),
                    error
                )
            }
            return .failure(error)
        }
    }

    private func recordSettingsPrimaryHotkey(
        current: HotkeyShortcut,
        completion: @escaping (HotkeyShortcut?) -> Void
    ) {
        guard allowRuntimeSettingsChange("settings hotkey recorder") else {
            completion(nil)
            return
        }
        closeHotkeyRecorders()
        suspendHotkeysForRecorder()
        let recorder = HotkeyRecorderWindowController(
            title: "Record Hotkey",
            instruction: "Press the shortcut to start or stop recording. Use Ctrl, Option, or Cmd. Shift+Space is also allowed.",
            currentShortcut: current
        ) { [weak self] shortcut in
            guard let self else {
                return
            }
            self.hotkeyRecorder = nil
            self.resumeHotkeysAfterRecorder()
            completion(shortcut)
        }
        hotkeyRecorder = recorder
        recorder.showRecorder()
    }

    private func recordSettingsSubmitHotkey(
        current: HotkeyShortcut?,
        completion: @escaping (HotkeyShortcut?) -> Void
    ) {
        guard allowRuntimeSettingsChange("settings submit hotkey recorder") else {
            completion(nil)
            return
        }
        closeHotkeyRecorders()
        suspendHotkeysForRecorder()
        let recorder = HotkeyRecorderWindowController(
            title: "Record Submit Hotkey",
            instruction: "Press the shortcut to record, paste, and send Return after paste. Use Ctrl, Option, or Cmd.",
            currentShortcut: current ?? .shiftCommandSpace
        ) { [weak self] shortcut in
            guard let self else {
                return
            }
            self.submitHotkeyRecorder = nil
            self.resumeHotkeysAfterRecorder()
            completion(shortcut)
        }
        submitHotkeyRecorder = recorder
        recorder.showRecorder()
    }

    private func selectLanguage(_ language: String) {
        guard allowRuntimeSettingsChange("language") else {
            return
        }
        settings.language = registry.validLanguage(language, for: settings.engine)
        saveSettingsAndPreloadSelectedModel()
    }

    private func selectModel(engine: String, model: String) {
        guard allowRuntimeSettingsChange("recognition model") else {
            return
        }
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
        guard allowRuntimeSettingsChange("hotkey") else {
            return
        }
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
                    setHotkeyError("Could not restore \(previous.label): \(error)")
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
        guard allowRuntimeSettingsChange("hotkey recorder") else {
            return
        }
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
        guard allowRuntimeSettingsChange("submit hotkey") else {
            return
        }
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
                    setHotkeyError(
                        "Could not restore submit \(previous.label): \(error)"
                    )
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
        guard allowRuntimeSettingsChange("submit hotkey recorder") else {
            return
        }
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
            setHotkeyError(
                "Could not restore \(settings.hotkey.label): \(error)"
            )
        }
    }

    private func resumeSubmitHotkeyAfterRecorder() {
        guard settings.submitHotkey != nil else {
            return
        }
        do {
            try submitHotkeyManager.resume()
        } catch {
            setHotkeyError(
                "Could not restore submit "
                    + "\(settings.submitHotkey?.label ?? "hotkey"): \(error)"
            )
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
        guard allowRuntimeSettingsChange("silence auto-stop") else {
            return
        }
        settings.silenceAutoStopEnabled = enabled
        saveSettingsOnly()
    }

    private func selectOutputMode(_ mode: OutputMode) {
        guard settings != nil, allowRuntimeSettingsChange("output mode") else {
            return
        }
        settings.outputMode = mode
        saveSettingsOnly()
    }

    private func selectMicrophone(_ uid: String?) {
        guard settings != nil, allowRuntimeSettingsChange("microphone") else {
            return
        }
        settings.microphoneDeviceUID = uid?.isEmpty == true ? nil : uid
        saveSettingsOnly()
    }

    private func allowRuntimeSettingsChange(_ label: String) -> Bool {
        guard !state.blocksSettingsChanges else {
            logInfo("ignored \(label) settings change while app is busy")
            return false
        }
        return true
    }

    private func refreshLaunchAtLoginState() {
        let status = launchAtLoginState.refresh(
            observed: loginItemManager.status()
        )
        updateLaunchAtLoginState(status)
    }

    private func reconcileLaunchAtLoginFailure(
        _ error: Error,
        requestedEnabled: Bool
    ) {
        let status = launchAtLoginState.didFail(
            error,
            requestedEnabled: requestedEnabled,
            observed: loginItemManager.status()
        )
        updateLaunchAtLoginState(status)
    }

    private func updateLaunchAtLoginState(_ status: LoginItemStatus) {
        switch launchAtLoginStatus {
        case .enabled, .disabled:
            break
        case .invalid(let reason):
            logInfo("launch at login status unavailable: \(reason)")
        }
        statusController?.updateLaunchAtLogin(status: launchAtLoginStatus)
        synchronizeSettingsWindow()
    }

    private func setLaunchAtLogin(_ enabled: Bool) {
        do {
            try applyLaunchAtLogin(enabled)
            logInfo("launch at login \(enabled ? "enabled" : "disabled")")
        } catch {
            reconcileLaunchAtLoginFailure(
                error,
                requestedEnabled: enabled
            )
            logInfo("launch at login change failed: \(error.localizedDescription)")
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Could Not Update Launch at Login"
            alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }
    }

    private func applyLaunchAtLogin(_ enabled: Bool) throws {
        let requestedStatus: LoginItemStatus = enabled ? .enabled : .disabled
        guard requestedStatus != launchAtLoginStatus else {
            return
        }
        try loginItemManager.setEnabled(enabled)
        _ = launchAtLoginState.didApply(enabled: enabled)
        statusController.updateLaunchAtLogin(status: requestedStatus)
        synchronizeSettingsWindow()
    }

    @discardableResult
    private func applyRuntimeSettings(_ proposedSettings: SettingsSnapshot) throws -> Bool {
        var editor = SettingsEditorState(
            settings: settings,
            launchAtLoginStatus: launchAtLoginStatus,
            registry: registry
        )
        editor.draftSettings = proposedSettings
        editor.normalizeDraft()
        if let issue = editor.validationIssue {
            throw SettingsApplicationError.validation(issue.message)
        }
        guard !state.blocksSettingsChanges || !editor.runtimeSettingsChanged else {
            throw SettingsApplicationError.busy
        }
        guard editor.runtimeSettingsChanged else {
            return false
        }

        let canonicalSettings = editor.normalizedDraftSettings
        let previousSettings = settings!
        if canonicalSettings.hotkey != previousSettings.hotkey
            || canonicalSettings.submitHotkey != previousSettings.submitHotkey {
            try replaceHotkeyRegistrations(
                with: canonicalSettings,
                restoring: previousSettings
            )
        }

        settings = canonicalSettings
        saveSettingsOnly()
        recoverFromHotkeyErrorIfReady()

        switch editor.applyPlan {
        case .restart:
            switch state {
            case .inputWaiting, .pasteUnavailable, .modelUnavailable, .copied,
                 .copySkipped, .copyFailed, .enhancementWarning, .microphoneError:
                restartBackendForModelChange()
            case .idle, .recording, .preloading, .transcribing, .postprocessing,
                 .backendRepairRequired, .repairingBackend, .hotkeyError,
                 .appSignatureChanged, .error:
                break
            }
        case .preload:
            switch state {
            case .inputWaiting, .pasteUnavailable, .modelUnavailable, .copied,
                 .copySkipped, .copyFailed, .enhancementWarning:
                preloadSelectedModel()
            case .idle, .recording, .preloading, .transcribing, .postprocessing,
                 .backendRepairRequired, .repairingBackend, .microphoneError,
                 .hotkeyError, .appSignatureChanged, .error:
                break
            }
        case .none, .saveOnly:
            break
        }
        return true
    }

    private func replaceHotkeyRegistrations(
        with proposedSettings: SettingsSnapshot,
        restoring previousSettings: SettingsSnapshot
    ) throws {
        closeHotkeyRecorders()
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: hotkeyManager,
            submitManager: submitHotkeyManager
        )
        do {
            try transaction.replace(
                with: proposedSettings,
                restoring: previousSettings,
                primaryAction: { [weak self] in
                    self?.toggleRecording(submitAfterPaste: false)
                },
                submitAction: { [weak self] in
                    self?.toggleRecording(submitAfterPaste: true)
                }
            )
        } catch HotkeyPairReplacementError.registrationFailed(let reason) {
            throw SettingsApplicationError.hotkeyRegistration(reason)
        } catch HotkeyPairReplacementError.rollbackFailed(let registration, let rollback) {
            setHotkeyError(
                "Could not restore the previous hotkeys: \(rollback)"
            )
            throw SettingsApplicationError.hotkeyRollbackFailed(
                registration: registration,
                rollback: rollback
            )
        }
    }

    private func saveSettingsOnly() {
        settings.engine = registry.validEngine(settings.engine)
        settings.language = registry.validLanguage(settings.language, for: settings.engine)
        settings.lastModelByEngine = registry.coerceModels(settings.lastModelByEngine)
        settingsStore.save(settings)
        statusController.updateSettings(registry: registry, settings: settings)
        synchronizeSettingsWindow()
    }

    private func saveSettingsAndPreloadSelectedModel() {
        saveSettingsOnly()
        switch state {
        case .inputWaiting, .pasteUnavailable, .modelUnavailable, .copied, .copySkipped,
             .copyFailed, .enhancementWarning:
            preloadSelectedModel()
        case .idle, .recording, .preloading, .transcribing, .postprocessing, .backendRepairRequired,
             .repairingBackend, .microphoneError, .hotkeyError, .appSignatureChanged, .error:
            break
        }
    }

    private func saveSettingsAndRestartBackendForModelChange() {
        saveSettingsOnly()
        switch state {
        case .inputWaiting, .pasteUnavailable, .modelUnavailable, .copied, .copySkipped,
             .copyFailed, .enhancementWarning, .microphoneError:
            restartBackendForModelChange()
        case .idle, .recording, .preloading, .transcribing, .postprocessing, .backendRepairRequired,
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

    private func setHotkeyError(_ message: String) {
        if case .hotkeyError = state {
            // Preserve the state captured by the first hotkey failure.
        } else {
            stateBeforeHotkeyError = state
        }
        setState(.hotkeyError(message))
    }

    private func setState(_ newState: AppState) {
        if case .copied = newState {
        } else if case .copySkipped = newState {
        } else if case .copyFailed = newState {
        } else if case .enhancementWarning = newState {
        } else {
            statusResetTimer?.invalidate()
            statusResetTimer = nil
            pendingEnhancementWarningMessage = nil
        }
        state = newState
        statusController.update(state: newState)
        settingsWindowController?.updateBusyState(newState.blocksSettingsChanges)
        updatePasteTargetCacheTimer(for: newState)
    }

    private func logInfo(_ message: String) {
        if let appLogger {
            appLogger.info(message)
        } else {
            NSLog("zen-whisper: %@", message)
        }
    }

    private func refreshEnhancementCatalog() {
        guard let enhancementCatalogStore else {
            return
        }
        enhancementCatalog = enhancementCatalogStore.load()
        invalidateStalePostprocessorApprovalIfNeeded()
        guard !enhancementCatalog.issues.isEmpty else {
            return
        }
        let reasonCodes = Set(
            enhancementCatalog.issues.map { $0.reason.rawValue }
        ).sorted().joined(separator: ",")
        logInfo(
            "enhancement catalog loaded with issues count="
                + "\(enhancementCatalog.issues.count) reasons=\(reasonCodes)"
        )
    }

    private func invalidateStalePostprocessorApprovalIfNeeded() {
        guard settings != nil,
              settings.enhancement.approvedPostprocessorRevision != nil else {
            return
        }
        guard !Self.postprocessorApprovalIsCurrent(
            selection: settings.enhancement,
            catalog: enhancementCatalog
        ) else {
            return
        }
        settings.enhancement.approvedPostprocessorRevision = nil
        settingsStore.save(settings)
        logInfo("invalidated stale postprocessor approval")
    }

    private func logEnhancementResult(
        _ result: BackendEnhancementResult,
        postprocessor: BackendPostprocessorRequest
    ) {
        logInfo(Self.enhancementLogMessage(
            result,
            postprocessor: postprocessor
        ))
    }

    nonisolated static func enhancementLogMessage(
        _ result: BackendEnhancementResult,
        postprocessor: BackendPostprocessorRequest
    ) -> String {
        let allowedWarningCodes: Set<String> = [
            "PREFLIGHT_FAILED",
            "PREFLIGHT_TIMEOUT",
            "EXECUTABLE_NOT_FOUND",
            "CLI_LAUNCH_FAILED",
            "CLI_CANCELLED",
            "CLI_TIMEOUT",
            "CLI_COMMUNICATION_FAILED",
            "CLI_FAILED",
            "CLI_OUTPUT_EMPTY",
            "CLI_OUTPUT_TOO_LARGE"
        ]
        let warningCode: String
        if let candidate = result.warningCode,
           allowedWarningCodes.contains(candidate) {
            warningCode = candidate
        } else {
            warningCode = result.warningCode == nil ? "none" : "UNKNOWN"
        }
        let elapsedSeconds = result.elapsedSeconds.map {
            String(
                format: "%.3f",
                locale: Locale(identifier: "en_US_POSIX"),
                $0
            )
        } ?? "unknown"
        return "enhancement result postprocessor=\(postprocessor.logIdentifier) "
            + "outcome=\(result.outcome.rawValue) "
            + "cli_selected=\(result.cliSelected) "
            + "succeeded=\(result.succeeded) "
            + "applied=\(result.applied) "
            + "warning_code=\(warningCode) "
            + "elapsed_sec=\(elapsedSeconds)"
    }

    nonisolated static func state(
        for progress: BackendProgressStage,
        currentState: AppState
    ) -> AppState? {
        guard progress == .postprocessing,
              currentState == .transcribing else {
            return nil
        }
        return .postprocessing
    }

    nonisolated static func shouldSubmitAfterPaste(
        requested: Bool,
        cliWasSelected: Bool
    ) -> Bool {
        requested && !cliWasSelected
    }

    nonisolated static func postprocessorApprovalIsCurrent(
        selection: EnhancementSelection,
        catalog: EnhancementCatalogSnapshot
    ) -> Bool {
        guard case .preset(let presetID) = selection.postprocessing,
              !catalog.blocksAllPostprocessors,
              !catalog.blockedPostprocessorIDs.contains(presetID),
              let preset = catalog.postprocessors[presetID],
              preset.destination != .local else {
            return false
        }
        return selection.approvedPostprocessorRevision == preset.reviewRevision
    }

    nonisolated static func preferredEnhancementWarningCode(
        requestWarningCode: String?,
        backendWarningCode: String?,
        engine: String,
        profileHadRecognitionHints: Bool,
        hintsApplied: Bool
    ) -> String? {
        if let backendWarningCode {
            return backendWarningCode
        }
        if let requestWarningCode {
            return requestWarningCode
        }
        if engine == "mlx-whisper", profileHadRecognitionHints, !hintsApplied {
            return "HINTS_NOT_APPLIED"
        }
        return nil
    }

    nonisolated static func visibleEnhancementWarningMessage(code: String) -> String {
        switch code.uppercased() {
        case "PROFILE_UNAVAILABLE":
            return "Selected profile was unavailable; transcription continued without its hints."
        case "POSTPROCESSOR_UNAVAILABLE":
            return "Selected post-processor was unavailable; dictionary fallback was used."
        case "POSTPROCESSOR_CONSENT_REQUIRED":
            return "Post-processing is inactive until reviewed in Settings; dictionary fallback was used."
        case "POSTPROCESSOR_CATALOG_INVALID", "POSTPROCESSOR_BLOCKED":
            return "Post-processor configuration was invalid; dictionary fallback was used."
        case "ENHANCEMENT_CONFIGURATION_TOO_LARGE":
            return "Enhancement configuration was too large; safe fallback was used."
        case "PREFLIGHT_FAILED", "PREFLIGHT_TIMEOUT":
            return "Post-processor readiness check failed; dictionary fallback was used."
        case "EXECUTABLE_NOT_FOUND", "CLI_LAUNCH_FAILED":
            return "Post-processor could not start; dictionary fallback was used."
        case "CLI_TIMEOUT":
            return "Post-processing timed out; dictionary fallback was used."
        case "CLI_CANCELLED":
            return "Post-processing was cancelled; dictionary fallback was used."
        case "CLI_COMMUNICATION_FAILED", "CLI_FAILED":
            return "Post-processing failed; dictionary fallback was used."
        case "CLI_OUTPUT_EMPTY":
            return "Post-processor returned no text; dictionary fallback was used."
        case "CLI_OUTPUT_TOO_LARGE":
            return "Post-processor output was too large; dictionary fallback was used."
        case "HINTS_NOT_APPLIED":
            return "Recognition hints were not applied; transcription continued safely."
        default:
            return "Enhancement could not be applied; safe fallback was used. See logs."
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
        case .inputWaiting, .pasteUnavailable, .copied, .copySkipped, .copyFailed,
             .enhancementWarning:
            return true
        case .idle, .recording, .preloading, .transcribing, .postprocessing, .modelUnavailable,
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

enum SettingsApplicationError: LocalizedError {
    case appUnavailable
    case busy
    case validation(String)
    case hotkeyRegistration(String)
    case hotkeyRollbackFailed(registration: String, rollback: String)

    var errorDescription: String? {
        switch self {
        case .appUnavailable:
            return "zen-whisper is no longer available to save these settings."
        case .busy:
            return "Settings cannot be changed while recording, loading a model, transcribing, or repairing the backend."
        case .validation(let message):
            return message
        case .hotkeyRegistration(let reason):
            return "The requested hotkeys could not be registered. The previous hotkeys are still active. \(reason)"
        case .hotkeyRollbackFailed(let registration, let rollback):
            return "The requested hotkeys could not be registered, and the previous hotkeys could not be restored. Registration: \(registration). Restore: \(rollback)."
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

import AppKit
import Carbon
import CoreGraphics
import CryptoKit
import Darwin
import XCTest
@testable import ZenWhisper

final class CoreTests: XCTestCase {
    func testElapsedText() {
        XCTAssertEqual(StatusText.elapsed(0), "00:00")
        XCTAssertEqual(StatusText.elapsed(12), "00:12")
        XCTAssertEqual(StatusText.elapsed(75), "01:15")
        XCTAssertEqual(StatusText.copyOnlyReason("Accessibility not allowed"), "AX")
        XCTAssertEqual(StatusText.copyOnlyReason("target changed"), "Changed")
        XCTAssertEqual(StatusText.copyOnlyReason("target is unsafe"), "Unsafe")
    }

    func testStatusIconKindMapping() {
        XCTAssertEqual(StatusIconFactory.kind(for: .idle), .idle)
        XCTAssertEqual(StatusIconFactory.kind(for: .inputWaiting), .inputWaiting)
        XCTAssertEqual(
            StatusIconFactory.kind(for: .recording(elapsed: 3, voiceActive: false)),
            .recordingSilent
        )
        XCTAssertEqual(
            StatusIconFactory.kind(for: .recording(elapsed: 3, voiceActive: true)),
            .recordingSpeech
        )
        XCTAssertEqual(StatusIconFactory.kind(for: .preloading(message: "x")), .loading)
        XCTAssertEqual(StatusIconFactory.kind(for: .transcribing), .processing)
        XCTAssertEqual(StatusIconFactory.kind(for: .postprocessing), .postprocessing)
        XCTAssertTrue(
            StatusIconFactory.processingColor(for: .postprocessing)?
                .isEqual(NSColor.systemPurple) == true
        )
        XCTAssertEqual(StatusIconFactory.kind(for: .copySkipped("x")), .warning)
        XCTAssertEqual(StatusIconFactory.kind(for: .copyFailed("x")), .warning)
        XCTAssertEqual(StatusIconFactory.kind(for: .enhancementWarning("x")), .warning)
        XCTAssertEqual(StatusIconFactory.kind(for: .hotkeyError("x")), .warning)
        XCTAssertEqual(StatusIconFactory.kind(for: .backendRepairRequired("x")), .warning)
    }

    func testHotkeyShortcutParsesLegacyAndCustomValues() {
        XCTAssertEqual(HotkeyShortcut.fromStorageValue("shift+space"), .shiftSpace)
        XCTAssertEqual(
            HotkeyShortcut.fromStorageValue("ctrl+option+cmd+space"),
            .controlOptionCommandSpace
        )

        let custom = HotkeyShortcut(keyCode: 0, modifiers: UInt32(shiftKey | cmdKey), keyLabel: "A")
        XCTAssertEqual(HotkeyShortcut.fromStorageValue(custom.storageValue), custom)
        XCTAssertEqual(HotkeyShortcut.parseComboString("cmd+shift+a")?.label, "Shift+Cmd+A")
        XCTAssertEqual(HotkeyShortcut.parseComboString("option+return")?.label, "Option+Return")
        XCTAssertEqual(HotkeyShortcut.shiftCommandSpace.label, "Shift+Cmd+Space")
        XCTAssertEqual(HotkeyShortcut.shiftCommandSpace.storageValue, "shift+cmd+space")
        XCTAssertEqual(HotkeyShortcut.controlOptionCommandReturn.label, "Ctrl+Option+Cmd+Return")
        XCTAssertEqual(HotkeyShortcut.controlOptionCommandReturn.storageValue, "ctrl+option+cmd+return")
        XCTAssertEqual(HotkeyShortcut.submitPresets, [.shiftCommandSpace, .controlOptionCommandReturn])
        XCTAssertNil(HotkeyShortcut.optionalFromStorageValue(""))
        XCTAssertNil(HotkeyShortcut.optionalFromStorageValue("off"))
        XCTAssertEqual(
            HotkeyShortcut.optionalFromStorageValue("ctrl+option+cmd+return"),
            .controlOptionCommandReturn
        )
        XCTAssertEqual(
            HotkeyShortcut.optionalFromStorageValue("cmd+shift+space"),
            .shiftCommandSpace
        )
        XCTAssertNil(HotkeyShortcut.parseComboString("space"))
        XCTAssertNil(HotkeyShortcut.parseComboString("shift+a"))
        XCTAssertEqual(
            HotkeyShortcut.fromStorageValue("keycode:0:\(UInt32(shiftKey)):A"),
            .shiftSpace
        )
        XCTAssertEqual(
            HotkeyShortcut(keyCode: 0, modifiers: UInt32(cmdKey), keyLabel: "A"),
            HotkeyShortcut(keyCode: 0, modifiers: UInt32(cmdKey), keyLabel: "Key 0")
        )
    }

    func testPasteEnterStatusText() {
        XCTAssertEqual(
            AppState.copied(pasteDispatched: true, reason: "clipboard restored; enter sent").title,
            "Pasted + Enter"
        )
        XCTAssertEqual(
            AppState.copied(pasteDispatched: true, reason: "clipboard kept").title,
            "Pasted"
        )
        XCTAssertEqual(
            AppState.copied(
                pasteDispatched: false,
                reason: "paste not confirmed; clipboard kept"
            ).title,
            "Copied · Paste not confirmed"
        )
        XCTAssertEqual(
            AppState.copied(
                pasteDispatched: false,
                reason: "paste not confirmed; newer clipboard preserved; transcript available from menu"
            ).title,
            "Paste not confirmed · Recoverable"
        )
        XCTAssertEqual(
            AppState.copied(
                pasteDispatched: false,
                reason: "unconfirmed transcript copied"
            ).title,
            "Transcript copied"
        )
        XCTAssertEqual(
            AppState.copied(
                pasteDispatched: false,
                reason: "no editable target"
            ).title,
            "Copied · No editable target"
        )
        XCTAssertEqual(AppState.copySkipped("target is unsafe").title, "Blocked · Secure field")
        XCTAssertEqual(
            AppState.copySkipped(
                "target safety could not be verified"
            ).title,
            "Blocked · Target safety unknown"
        )
        XCTAssertEqual(
            AppState.copySkipped(
                "target is unsafe; transcript available from menu"
            ).title,
            "Blocked · Secure field · Recoverable"
        )
        XCTAssertEqual(
            AppState.copyFailed(
                "pasteboard write failed; transcript available from menu"
            ).title,
            "Copy failed · Recoverable"
        )
        XCTAssertEqual(AppState.enhancementWarning("safe fallback").title, "Fallback")
        XCTAssertEqual(AppState.postprocessing.title, "Post-processing")
        XCTAssertEqual(StatusText.copyOnlyReason("paste event unavailable"), "No paste")
    }

    func testPasteAttemptResultsDriveRecoverableStatusPresentation() throws {
        let writeFailures: [
            (
                PasteboardWriteFailureDisposition,
                String,
                String
            )
        ] = [
            (
                .originalUntouched,
                "pasteboard write failed; clipboard unchanged; transcript available from menu",
                "Copy Failed · Recoverable: pasteboard write failed; clipboard unchanged; transcript available from menu"
            ),
            (
                .originalUnavailable,
                "pasteboard write failed after clipboard clear; original clipboard unavailable; transcript available from menu",
                "Copy Failed · Recoverable: pasteboard write failed after clipboard clear; original clipboard unavailable; transcript available from menu"
            ),
            (
                .externalChangePreserved,
                "pasteboard write failed; external clipboard preserved; transcript available from menu",
                "Copy Failed · Recoverable: pasteboard write failed; external clipboard preserved; transcript available from menu"
            ),
        ]
        for (disposition, expectedReason, expectedMenuText) in writeFailures {
            let state = try XCTUnwrap(
                PasteAttemptPresentation.state(
                    for: .failed(
                        reason: .pasteboardWriteFailed(
                            disposition: disposition
                        )
                    )
                )
            )
            XCTAssertEqual(state, .copyFailed(expectedReason))
            XCTAssertEqual(state.title, "Copy failed · Recoverable")
            XCTAssertEqual(
                StatusController.menuText(for: state),
                expectedMenuText
            )
        }

        let blockedCases: [
            (
                PasteAttemptResult,
                AppState,
                String,
                String
            )
        ] = [
            (
                .blocked(
                    reason: .unsafeTarget,
                    transcript: .intentionallyDiscarded
                ),
                .copySkipped("target is unsafe"),
                "Blocked · Secure field",
                "Blocked: secure or sensitive field"
            ),
            (
                .blocked(
                    reason: .unsafeTarget,
                    transcript: .recoveryMenu
                ),
                .copySkipped(
                    "target is unsafe; transcript available from menu"
                ),
                "Blocked · Secure field · Recoverable",
                "Blocked: secure or sensitive field; transcript available from menu"
            ),
            (
                .blocked(
                    reason: .targetSafetyIndeterminate,
                    transcript: .intentionallyDiscarded
                ),
                .copySkipped("target safety could not be verified"),
                "Blocked · Target safety unknown",
                "Blocked: target safety could not be verified"
            ),
            (
                .blocked(
                    reason: .targetSafetyIndeterminate,
                    transcript: .recoveryMenu
                ),
                .copySkipped(
                    "target safety could not be verified; transcript available from menu"
                ),
                "Blocked · Safety unknown · Recoverable",
                "Blocked: target safety could not be verified; transcript available from menu"
            )
        ]
        for (result, expectedState, expectedTitle, expectedMenuText) in blockedCases {
            let state = try XCTUnwrap(
                PasteAttemptPresentation.state(for: result)
            )
            XCTAssertEqual(state, expectedState)
            XCTAssertEqual(state.title, expectedTitle)
            XCTAssertEqual(
                StatusController.menuText(for: state),
                expectedMenuText
            )
        }
    }

    func testUnconfirmedTranscriptRecoveryIsFIFOAndRetainsFailedCopy() {
        var store = UnconfirmedTranscriptRecoveryStore()
        store.append("first")
        store.append("second")
        var copied: [String] = []

        XCTAssertEqual(
            store.copyNext(using: { _ in false }),
            .copyFailed
        )
        XCTAssertEqual(store.count, 2)
        XCTAssertEqual(
            store.copyNext(using: {
                copied.append($0)
                return true
            }),
            .copied
        )
        XCTAssertEqual(copied, ["first"])
        XCTAssertEqual(store.count, 1)
        XCTAssertEqual(
            store.copyNext(using: {
                copied.append($0)
                return true
            }),
            .copied
        )
        XCTAssertEqual(copied, ["first", "second"])
        XCTAssertEqual(store.count, 0)
        XCTAssertEqual(
            store.copyNext(using: { _ in true }),
            .empty
        )
    }

    func testUnconfirmedTranscriptRecoveryPolicyRetainsOnlyUnconfirmedOutput() {
        XCTAssertTrue(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: .manualPasteFallback(
                    reason: .verificationTimedOut,
                    availability: .clipboard
                )
            )
        )
        XCTAssertTrue(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: .manualPasteFallback(
                    reason: .noEditableTarget,
                    availability: .recoveryMenu
                )
            )
        )
        XCTAssertTrue(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: .failed(
                    reason: .pasteboardWriteFailed(
                        disposition: .originalUnavailable
                    )
                )
            )
        )
        XCTAssertFalse(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: .manualPasteFallback(
                    reason: .copyOnlyMode,
                    availability: .clipboard
                )
            )
        )
        XCTAssertFalse(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: .pastedVerified(
                    clipboard: .kept,
                    submit: .notRequested
                )
            )
        )
        XCTAssertFalse(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: .blocked(
                    reason: .unsafeTarget,
                    transcript: .intentionallyDiscarded
                )
            )
        )
        XCTAssertTrue(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: .blocked(
                    reason: .unsafeTarget,
                    transcript: .recoveryMenu
                )
            )
        )
        XCTAssertFalse(
            UnconfirmedTranscriptRecoveryPolicy.shouldRetain(
                for: .failed(reason: .superseded)
            )
        )
    }

    func testBackendStopReportsExitedProcessCleanlinessSeparately() throws {
        let paths = AppPaths(
            appSupport: URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
                .appendingPathComponent("zw-stop-support-\(UUID().uuidString)", isDirectory: true),
            logs: URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
                .appendingPathComponent("zw-stop-logs-\(UUID().uuidString)", isDirectory: true)
        )
        let success = try exitedProcess(status: 0)
        let failure = try exitedProcess(status: 1)

        XCTAssertEqual(
            BackendClient(paths: paths, process: success).stopDetailed(),
            BackendStopResult(stopped: true, cleanExit: true, terminationStatus: 0)
        )
        XCTAssertEqual(
            BackendClient(paths: paths, process: failure).stopDetailed(),
            BackendStopResult(stopped: true, cleanExit: false, terminationStatus: 1)
        )
        XCTAssertTrue(BackendClient(paths: paths, process: try exitedProcess(status: 1)).stop())
        XCTAssertTrue(AppDelegate.shouldContinueAfterBackendStop(
            BackendStopResult(stopped: true, cleanExit: false, terminationStatus: 1)
        ))
        XCTAssertFalse(AppDelegate.shouldContinueAfterBackendStop(
            BackendStopResult(stopped: false, cleanExit: false, terminationStatus: nil)
        ))
    }

    func testSocketPathUsesShortPrivateRuntimeDirectory() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("zen-whisper-paths-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: root)
        }
        let runtimeRoot = URL(fileURLWithPath: "/tmp/zwrt-\(UUID().uuidString.prefix(8))", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: runtimeRoot)
        }
        let livePaths = AppPaths(
            appSupport: root.appendingPathComponent("live-support", isDirectory: true),
            logs: root.appendingPathComponent("live-logs", isDirectory: true)
        )
        XCTAssertTrue(livePaths.socketPath.path.hasPrefix("/tmp/zen-whisper-\(getuid())/"))
        XCTAssertEqual(livePaths.socketPath.lastPathComponent, "b.sock")
        XCTAssertLessThanOrEqual(
            livePaths.socketPath.path.utf8CString.count,
            MemoryLayout.size(ofValue: sockaddr_un().sun_path)
        )

        let paths = AppPaths(
            appSupport: root.appendingPathComponent("support", isDirectory: true),
            logs: root.appendingPathComponent("logs", isDirectory: true),
            runtimeDirectoryOverride: runtimeRoot
        )

        XCTAssertEqual(paths.socketPath.lastPathComponent, "b.sock")

        try paths.prepare()
        var metadata = stat()
        XCTAssertEqual(lstat(paths.runtimeDirectory.path, &metadata), 0)
        XCTAssertEqual(metadata.st_uid, getuid())
        XCTAssertEqual(metadata.st_mode & S_IFMT, S_IFDIR)
        XCTAssertEqual(metadata.st_mode & 0o777, 0o700)
    }

    func testRuntimeDirectoryRejectsFileAndSymlinkAndRepairsPermissions() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("zen-whisper-runtime-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: root)
        }
        let runtimeRoot = URL(fileURLWithPath: "/tmp/zwrt-\(UUID().uuidString.prefix(8))", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: runtimeRoot)
        }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)

        let fileRuntime = runtimeRoot.appendingPathComponent("runtime-file")
        try FileManager.default.createDirectory(at: runtimeRoot, withIntermediateDirectories: true)
        try "not a directory".write(to: fileRuntime, atomically: true, encoding: .utf8)
        let filePaths = AppPaths(
            appSupport: root.appendingPathComponent("support-file", isDirectory: true),
            logs: root.appendingPathComponent("logs-file", isDirectory: true),
            runtimeDirectoryOverride: fileRuntime
        )
        XCTAssertThrowsError(try filePaths.prepare()) { error in
            guard case AppPathsError.runtimePathNotDirectory(let path) = error else {
                XCTFail("Expected runtimePathNotDirectory, got \(error)")
                return
            }
            XCTAssertEqual(path, fileRuntime.path)
        }

        let target = runtimeRoot.appendingPathComponent("target", isDirectory: true)
        try FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
        let symlinkRuntime = runtimeRoot.appendingPathComponent("runtime-link")
        try FileManager.default.createSymbolicLink(at: symlinkRuntime, withDestinationURL: target)
        let symlinkPaths = AppPaths(
            appSupport: root.appendingPathComponent("support-link", isDirectory: true),
            logs: root.appendingPathComponent("logs-link", isDirectory: true),
            runtimeDirectoryOverride: symlinkRuntime
        )
        XCTAssertThrowsError(try symlinkPaths.prepare()) { error in
            guard case AppPathsError.runtimePathNotDirectory(let path) = error else {
                XCTFail("Expected runtimePathNotDirectory, got \(error)")
                return
            }
            XCTAssertEqual(path, symlinkRuntime.path)
        }

        let looseRuntime = runtimeRoot.appendingPathComponent("runtime-loose", isDirectory: true)
        try FileManager.default.createDirectory(at: looseRuntime, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: looseRuntime.path)
        let loosePaths = AppPaths(
            appSupport: root.appendingPathComponent("support-loose", isDirectory: true),
            logs: root.appendingPathComponent("logs-loose", isDirectory: true),
            runtimeDirectoryOverride: looseRuntime
        )
        try loosePaths.prepare()
        var metadata = stat()
        XCTAssertEqual(lstat(looseRuntime.path, &metadata), 0)
        XCTAssertEqual(metadata.st_mode & 0o777, 0o700)
    }

    func testSettingsStoreDropsDuplicateSubmitHotkey() throws {
        let registry = try ModelRegistry.loadDefault()
        let suiteName = "zen-whisper-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        defaults.set("shift+space", forKey: "hotkey")
        defaults.set("keycode:\(UInt32(kVK_Space)):\(UInt32(shiftKey)):Custom Space", forKey: "submitHotkey")

        let settings = SettingsStore(defaults: defaults, registry: registry).load()

        XCTAssertEqual(settings.hotkey, .shiftSpace)
        XCTAssertNil(settings.submitHotkey)
        XCTAssertNil(defaults.string(forKey: "submitHotkey"))
    }

    func testSettingsStoreRemovesLegacyUnverifiedPasteFallback() throws {
        let registry = try ModelRegistry.loadDefault()
        let suiteName = "zen-whisper-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        defaults.set(true, forKey: "allowUnverifiedPasteFallback")
        let store = SettingsStore(defaults: defaults, registry: registry)
        let settings = store.load()

        XCTAssertNil(defaults.object(forKey: "allowUnverifiedPasteFallback"))
        XCTAssertEqual(settings.outputMode, .pasteKeepClipboard)
        store.save(settings)
        XCTAssertNil(defaults.object(forKey: "allowUnverifiedPasteFallback"))
    }

    func testSettingsStoreMigratesLegacyClipboardRestoreModeToSafeKeepMode() throws {
        let registry = try ModelRegistry.loadDefault()
        let suiteName = "zen-whisper-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        defaults.set(
            OutputMode.pasteRestoreClipboard.rawValue,
            forKey: "outputMode"
        )

        let store = SettingsStore(
            defaults: defaults,
            registry: registry
        )
        let settings = store.load()

        XCTAssertEqual(settings.outputMode, .pasteKeepClipboard)
        XCTAssertTrue(store.didMigrateClipboardRestoreMode)
        XCTAssertEqual(
            defaults.string(forKey: "outputMode"),
            OutputMode.pasteKeepClipboard.rawValue
        )
        XCTAssertFalse(OutputMode.allCases.contains(.pasteRestoreClipboard))
    }

    func testSettingsStoreNeverPersistsLegacyClipboardRestoreMode() throws {
        let registry = try ModelRegistry.loadDefault()
        let suiteName = "zen-whisper-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let store = SettingsStore(defaults: defaults, registry: registry)
        var settings = store.load()
        settings.outputMode = .pasteRestoreClipboard

        store.save(settings)

        XCTAssertEqual(
            defaults.string(forKey: "outputMode"),
            OutputMode.pasteKeepClipboard.rawValue
        )
    }

    func testRMSAnalyzer() {
        XCTAssertTrue(RMSAnalyzer.isEmptyAudio(samples: [0, 0, 0]))
        XCTAssertFalse(RMSAnalyzer.isEmptyAudio(samples: [0.02, 0, 0]))
    }

    func testRegistryCoercion() throws {
        let registry = try ModelRegistry.loadDefault()
        XCTAssertEqual(registry.validLanguage("ja"), "ja")
        XCTAssertEqual(registry.validLanguage("auto", for: "mlx-whisper"), "auto")
        XCTAssertEqual(registry.validLanguage("ja", for: "mlx-whisper"), "ja")
        XCTAssertEqual(registry.supportedLanguages(for: "mlx-whisper").map { $0.id }, ["auto", "en", "ja"])
        XCTAssertEqual(registry.backendLanguage("ja", engineID: "mlx-whisper"), "ja")
        XCTAssertEqual(registry.backendLanguage("auto", engineID: "mlx-whisper"), "auto")
        XCTAssertEqual(registry.validLanguage("auto", for: "mlx-qwen3-asr"), "auto")
        XCTAssertEqual(registry.backendLanguage("auto", engineID: "mlx-qwen3-asr"), "auto")
        XCTAssertEqual(registry.backendLanguage("ja", engineID: "mlx-qwen3-asr"), "Japanese")
        XCTAssertEqual(registry.backendLanguage("en", engineID: "mlx-qwen3-asr"), "English")
        XCTAssertEqual(registry.validEngine("missing"), registry.defaultEngine)
        XCTAssertEqual(
            registry.validModel("missing", for: "mlx-whisper"),
            "mlx-community/whisper-large-v3-turbo"
        )
        XCTAssertEqual(
            registry.validModel("missing", for: "mlx-qwen3-asr"),
            "mlx-community/Qwen3-ASR-0.6B-8bit"
        )
    }

    func testRegistryValidationRejectsUnsupportedVersionDuplicatesAndUnknownLanguageEngines() throws {
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(version: 2))) { error in
            XCTAssertEqual(error as? RegistryError, .unsupportedVersion(2))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(duplicateEngine: true))) { error in
            XCTAssertEqual(error as? RegistryError, .duplicateID("engine", "mlx-whisper"))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(duplicateModel: true))) { error in
            XCTAssertEqual(error as? RegistryError, .duplicateID("mlx-whisper model", "model-a"))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(unknownLanguageEngine: true))) { error in
            XCTAssertEqual(error as? RegistryError, .invalidLanguageEngine("ja", "missing"))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(blankEngineLabel: true))) { error in
            XCTAssertEqual(error as? RegistryError, .invalidLabel("engine", "mlx-whisper"))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(blankModelLabel: true))) { error in
            XCTAssertEqual(error as? RegistryError, .invalidLabel("mlx-whisper model", "model-a"))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(blankLanguageLabel: true))) { error in
            XCTAssertEqual(error as? RegistryError, .invalidLabel("language", "ja"))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(blankLanguageID: true))) { error in
            XCTAssertEqual(error as? RegistryError, .invalidLanguageID(""))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(defaultLanguageMissingDefaultEngine: true))) { error in
            XCTAssertEqual(error as? RegistryError, .unsupportedDefaultLanguage("ja", "mlx-whisper"))
        }
        XCTAssertThrowsError(try ModelRegistry.load(from: registryJSON(engineWithoutLanguage: true))) { error in
            XCTAssertEqual(error as? RegistryError, .engineWithoutLanguage("mlx-qwen3-asr"))
        }
    }

    func testBackendErrorPreservesRecoverableFlag() throws {
        XCTAssertThrowsError(try decodeBackendResponse(Data("not json".utf8))) { error in
            XCTAssertEqual(error as? BackendProtocolError, .invalidJSON)
        }

        let recoverable = """
        {"type":"error","request_id":"r1","code":"MODEL_NOT_AVAILABLE","message":"offline","recoverable":true}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try decodeBackendResponse(recoverable)) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .backendError(code: "MODEL_NOT_AVAILABLE", message: "offline", recoverable: true)
            )
        }

        let fatal = """
        {"type":"error","request_id":"r2","code":"BACKEND_ERROR","message":"crashed","recoverable":false}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try decodeBackendResponse(fatal)) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .backendError(code: "BACKEND_ERROR", message: "crashed", recoverable: false)
            )
        }

        let malformed = """
        {"type":"error","request_id":"r3","message":"crashed","recoverable":false}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try decodeBackendResponse(malformed)) { error in
            XCTAssertEqual(error as? BackendProtocolError, .invalidBackendError("missing code"))
        }

        let missingRecoverable = """
        {"type":"error","request_id":"r4","code":"BACKEND_ERROR","message":"crashed"}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try decodeBackendResponse(missingRecoverable)) { error in
            XCTAssertEqual(error as? BackendProtocolError, .invalidBackendError("missing recoverable"))
        }
    }

    func testBackendOperationErrorStateMapping() {
        XCTAssertEqual(
            AppDelegate.stateForBackendOperationError(code: "MODEL_NOT_AVAILABLE", recoverable: true),
            .modelUnavailable("Model unavailable. See logs.")
        )
        XCTAssertEqual(
            AppDelegate.stateForBackendOperationError(code: "BACKEND_SHUTTING_DOWN", recoverable: true),
            .backendRepairRequired("Backend is shutting down. Restart zen-whisper.")
        )
        XCTAssertEqual(
            AppDelegate.stateForBackendOperationError(code: "BACKEND_ERROR", recoverable: false),
            .backendRepairRequired("Backend unavailable. See logs.")
        )
    }

    func testCachedPasteTargetReuseRequiresCurrentOrCachedFrontmostApp() {
        let now = Date()
        XCTAssertTrue(
            AppDelegate.shouldReuseCachedPasteTarget(
                cachedPID: 100,
                frontmostPID: 100,
                currentPID: 200
            )
        )
        XCTAssertTrue(
            AppDelegate.shouldReuseCachedPasteTarget(
                cachedPID: 100,
                frontmostPID: 200,
                currentPID: 200
            )
        )
        XCTAssertFalse(
            AppDelegate.shouldReuseCachedPasteTarget(
                cachedPID: 100,
                frontmostPID: 300,
                currentPID: 200
            )
        )
        XCTAssertFalse(
            AppDelegate.shouldReuseCachedPasteTarget(
                cachedPID: 100,
                frontmostPID: nil,
                currentPID: 200
            )
        )
        XCTAssertTrue(
            AppDelegate.shouldUseCachedPasteTarget(
                cachedDate: now.addingTimeInterval(-19),
                now: now,
                maxAge: 20,
                cachedPID: 100,
                frontmostPID: 100,
                currentPID: 200
            )
        )
        XCTAssertTrue(
            AppDelegate.shouldUseCachedPasteTarget(
                cachedDate: now.addingTimeInterval(-20),
                now: now,
                maxAge: 20,
                cachedPID: 100,
                frontmostPID: 100,
                currentPID: 200
            )
        )
        XCTAssertFalse(
            AppDelegate.shouldUseCachedPasteTarget(
                cachedDate: now.addingTimeInterval(-21),
                now: now,
                maxAge: 20,
                cachedPID: 100,
                frontmostPID: 100,
                currentPID: 200
            )
        )
        XCTAssertFalse(
            AppDelegate.shouldUseCachedPasteTarget(
                cachedDate: nil,
                now: now,
                maxAge: 20,
                cachedPID: 100,
                frontmostPID: 100,
                currentPID: 200
            )
        )
        XCTAssertFalse(
            AppDelegate.shouldUseCachedPasteTarget(
                cachedDate: now,
                now: now,
                maxAge: 20,
                cachedPID: 100,
                frontmostPID: 300,
                currentPID: 200
            )
        )
    }

    func testBackendResponseValidationChecksErrorRequestIDAndSchema() throws {
        let request: [String: Any] = ["type": "preload", "request_id": "expected"]
        XCTAssertThrowsError(try validateBackendResponse(
            [
                "type": "error",
                "request_id": "other",
                "code": "MODEL_NOT_AVAILABLE",
                "message": "offline",
                "recoverable": true
            ],
            request: request,
            expectedType: "ready"
        )) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .requestIDMismatch(expected: "expected", actual: "other")
            )
        }
        XCTAssertThrowsError(try validateBackendResponse(
            [
                "type": "error",
                "request_id": "expected",
                "code": "",
                "message": "offline",
                "recoverable": true
            ],
            request: request,
            expectedType: "ready"
        )) { error in
            XCTAssertEqual(error as? BackendProtocolError, .invalidBackendError("missing code"))
        }
        XCTAssertThrowsError(try validateBackendResponse(
            [
                "type": "error",
                "request_id": "expected",
                "code": "MODEL_NOT_AVAILABLE",
                "message": "offline",
                "recoverable": true
            ],
            request: request,
            expectedType: "ready"
        )) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .backendError(code: "MODEL_NOT_AVAILABLE", message: "offline", recoverable: true)
            )
        }
        XCTAssertThrowsError(try validateBackendResponse(
            [
                "type": "error",
                "request_id": "expected",
                "code": "BACKEND_SHUTTING_DOWN",
                "message": "stopping",
                "recoverable": true
            ],
            request: request,
            expectedType: "ready"
        )) { error in
            XCTAssertEqual(error as? BackendProtocolError, .invalidBackendError("contradictory recoverable"))
        }
    }

    func testBackendRepairRunnerExecutesBundledInstallerWithRepairEnvironment() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("zen-whisper-repair-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: root)
        }
        let app = root.appendingPathComponent("zen-whisper.app", isDirectory: true)
        let backendResources = app
            .appendingPathComponent("Contents/Resources/backend", isDirectory: true)
        try FileManager.default.createDirectory(at: backendResources, withIntermediateDirectories: true)
        let installer = backendResources.appendingPathComponent("install_backend_from_app.sh")
        let script = """
        #!/bin/sh
        set -eu
        printf 'arg=%s\\n' "$1" > "$ZEN_WHISPER_APP_SUPPORT/repair-env.txt"
        printf 'bundled=%s\\n' "${ZEN_WHISPER_ALLOW_BUNDLED_TOOLCHAIN:-}" >> "$ZEN_WHISPER_APP_SUPPORT/repair-env.txt"
        printf 'recorded=%s\\n' "${ZEN_WHISPER_ALLOW_RECORDED_TOOLCHAIN:-}" >> "$ZEN_WHISPER_APP_SUPPORT/repair-env.txt"
        printf 'path=%s\\n' "$PATH" >> "$ZEN_WHISPER_APP_SUPPORT/repair-env.txt"
        echo "installer stdout"
        """
        try script.write(to: installer, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: installer.path)

        let paths = AppPaths(
            appSupport: root.appendingPathComponent("support", isDirectory: true),
            logs: root.appendingPathComponent("logs", isDirectory: true)
        )
        try BackendRepairRunner(paths: paths, bundleURL: app, timeoutSeconds: 5).repair()

        let env = try String(
            contentsOf: paths.appSupport.appendingPathComponent("repair-env.txt"),
            encoding: .utf8
        )
        XCTAssertTrue(env.contains("arg=\(app.path)\n"))
        XCTAssertTrue(env.contains("bundled=1\n"))
        XCTAssertTrue(env.contains("recorded=1\n"))
        XCTAssertTrue(env.contains("/opt/homebrew/bin"))
        let repairLog = try String(
            contentsOf: paths.logs.appendingPathComponent("backend-repair.log"),
            encoding: .utf8
        )
        XCTAssertTrue(repairLog.contains("installer stdout"))
    }

    func testBackendRepairRunnerReportsInstallerFailure() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("zen-whisper-repair-fail-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: root)
        }
        let app = root.appendingPathComponent("zen-whisper.app", isDirectory: true)
        let backendResources = app
            .appendingPathComponent("Contents/Resources/backend", isDirectory: true)
        try FileManager.default.createDirectory(at: backendResources, withIntermediateDirectories: true)
        let installer = backendResources.appendingPathComponent("install_backend_from_app.sh")
        try "#!/bin/sh\nexit 7\n".write(to: installer, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: installer.path)
        let paths = AppPaths(
            appSupport: root.appendingPathComponent("support", isDirectory: true),
            logs: root.appendingPathComponent("logs", isDirectory: true)
        )

        XCTAssertThrowsError(try BackendRepairRunner(paths: paths, bundleURL: app, timeoutSeconds: 5).repair()) { error in
            XCTAssertEqual(error as? BackendRepairRunnerError, .failed(7))
        }
    }

    func testManifestJSONEscapingMatchesPythonEnsureAscii() {
        XCTAssertEqual(jsonEscaped("高😀\n\u{7F}"), "\\u9ad8\\ud83d\\ude00\\n\\u007f")
    }

    func testBackendInstallValidatorAcceptsAndInvalidatesPerUserManifests() throws {
        let fixture = try makePerUserBackendInstallFixture()
        XCTAssertEqual(fixture.validator.validateInstallMetadata(), .valid)

        try "\n ".append(
            to: fixture.paths.appSupport.appendingPathComponent("backend/venv_manifest.json")
        )
        if case .invalid(let reason) = fixture.validator.validateInstallMetadata() {
            XCTAssertTrue(reason.contains("backend venv manifest"))
        } else {
            XCTFail("Expected tampered backend venv manifest to invalidate install")
        }

        let runtimeFixture = try makePerUserBackendInstallFixture()
        try "\n ".append(
            to: runtimeFixture.paths.appSupport
                .appendingPathComponent("backend/python_runtime_manifest.json")
        )
        if case .invalid(let reason) = runtimeFixture.validator.validateInstallMetadata() {
            XCTAssertTrue(reason.contains("Python runtime manifest"))
        } else {
            XCTFail("Expected tampered Python runtime manifest to invalidate install")
        }
    }

    func testBackendInstallValidatorRejectsStartupHookEvenWhenManifestMatches() throws {
        let fixture = try makePerUserBackendInstallFixture()
        let sitePackages = fixture.paths.appSupport
            .appendingPathComponent("backend/.venv/lib/python3.12/site-packages", isDirectory: true)
        try "raise SystemExit('blocked')\n"
            .write(to: sitePackages.appendingPathComponent("sitecustomize.py"), atomically: true, encoding: .utf8)
        try rewriteVenvManifestAndInstallHash(fixture.paths)

        if case .invalid(let reason) = fixture.validator.validateInstallMetadata() {
            XCTAssertTrue(reason.contains("startup hook"))
        } else {
            XCTFail("Expected startup hook to invalidate install")
        }
    }

    func testBackendInstallValidatorRejectsExternalSymlinkEvenWhenManifestMatches() throws {
        let fixture = try makePerUserBackendInstallFixture()
        let sitePackages = fixture.paths.appSupport
            .appendingPathComponent("backend/.venv/lib/python3.12/site-packages", isDirectory: true)
        try FileManager.default.createSymbolicLink(
            at: sitePackages.appendingPathComponent("external-link"),
            withDestinationURL: URL(fileURLWithPath: "/tmp")
        )
        try rewriteVenvManifestAndInstallHash(fixture.paths)

        if case .invalid(let reason) = fixture.validator.validateInstallMetadata() {
            XCTAssertTrue(reason.contains("external backend venv symlink"))
        } else {
            XCTFail("Expected external symlink to invalidate install")
        }
    }

    func testBackendInstallValidatorRejectsExternalPythonRuntimeSymlinkEvenWhenManifestMatches() throws {
        let fixture = try makePerUserBackendInstallFixture()
        let installURL = fixture.paths.appSupport.appendingPathComponent("backend/install.json")
        let data = try Data(contentsOf: installURL)
        let install = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        let runtimeRoot = URL(fileURLWithPath: try XCTUnwrap(install["python_runtime_prefix"] as? String))
        try FileManager.default.createSymbolicLink(
            at: runtimeRoot.appendingPathComponent("external-runtime-link"),
            withDestinationURL: URL(fileURLWithPath: "/tmp")
        )
        try rewriteRuntimeManifestAndInstallHash(fixture.paths)

        if case .invalid(let reason) = fixture.validator.validateInstallMetadata() {
            XCTAssertTrue(reason.contains("external Python runtime symlink"))
        } else {
            XCTFail("Expected external Python runtime symlink to invalidate install")
        }
    }

    func testBackendInstallValidatorRejectsRuntimeManifestHashMismatch() throws {
        let fixture = try makePerUserBackendInstallFixture()
        try "\n ".append(
            to: fixture.paths.appSupport
                .appendingPathComponent("backend/python_runtime_manifest.json")
        )

        if case .invalid(let reason) = fixture.validator.validateInstallMetadata() {
            XCTAssertTrue(reason.contains("Python runtime manifest"))
        } else {
            XCTFail("Expected runtime manifest mismatch to invalidate install")
        }
    }

    func testBackendInstallValidatorRejectsPythonSourceHashMismatch() throws {
        let fixture = try makePerUserBackendInstallFixture()
        try updateInstallRecord(fixture.paths) { install in
            install["python_source_sha256"] = "bad"
        }

        if case .invalid(let reason) = fixture.validator.validateInstallMetadata() {
            XCTAssertTrue(reason.contains("Python executable hash"))
        } else {
            XCTFail("Expected Python source hash mismatch to invalidate install")
        }
    }

    func testLoginItemManagerReadsMatchingLaunchAgent() throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: home)
        }
        let manager = LoginItemManager(homeDirectory: home)
        XCTAssertFalse(manager.isEnabled())

        let launchAgents = home
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("LaunchAgents", isDirectory: true)
        try FileManager.default.createDirectory(at: launchAgents, withIntermediateDirectories: true)
        let plist: [String: Any] = [
            "Label": LoginItemManager.label,
            "ProgramArguments": ["/usr/bin/open", LoginItemManager.appPath],
            "RunAtLoad": true
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        try data.write(to: manager.plistURL)
        XCTAssertTrue(manager.isEnabled())

        let wrongPlist: [String: Any] = [
            "Label": LoginItemManager.label,
            "ProgramArguments": ["/usr/bin/open", "/Applications/Other.app"],
            "RunAtLoad": true
        ]
        let wrongData = try PropertyListSerialization.data(fromPropertyList: wrongPlist, format: .xml, options: 0)
        try wrongData.write(to: manager.plistURL, options: .atomic)
        XCTAssertFalse(manager.isEnabled())
        XCTAssertEqual(manager.status(), .disabled)

        try "not a plist".write(to: manager.plistURL, atomically: true, encoding: .utf8)
        if case .invalid(let reason) = manager.status() {
            XCTAssertTrue(reason.contains("LaunchAgent plist is unreadable"))
        } else {
            XCTFail("Expected malformed LaunchAgent plist to be invalid")
        }
    }

    func testLoginItemManagerWritesAndRemovesLaunchAgent() throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: home)
        }
        var launchctlCalls: [[String]] = []
        let manager = LoginItemManager(
            homeDirectory: home,
            appExists: { $0 == LoginItemManager.appPath },
            launchctlRunner: { arguments in launchctlCalls.append(arguments) }
        )

        try manager.setEnabled(true)

        XCTAssertTrue(manager.isEnabled())
        let data = try Data(contentsOf: manager.plistURL)
        let plist = try XCTUnwrap(
            PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any]
        )
        XCTAssertEqual(plist["Label"] as? String, LoginItemManager.label)
        XCTAssertEqual(plist["ProgramArguments"] as? [String], ["/usr/bin/open", LoginItemManager.appPath])
        XCTAssertEqual(plist["RunAtLoad"] as? Bool, true)
        XCTAssertEqual(launchctlCalls.map { $0.first }, ["bootout", "bootstrap"])

        try manager.setEnabled(false)

        XCTAssertFalse(manager.isEnabled())
        XCTAssertFalse(FileManager.default.fileExists(atPath: manager.plistURL.path))
        XCTAssertEqual(launchctlCalls.map { $0.first }, ["bootout", "bootstrap", "bootout"])
    }

    func testLoginItemManagerCleansUpPlistWhenBootstrapFails() throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: home)
        }
        let manager = LoginItemManager(
            homeDirectory: home,
            appExists: { _ in true },
            launchctlRunner: { arguments in
                if arguments.first == "bootstrap" {
                    throw LoginItemError.launchctlFailed("boom")
                }
            }
        )

        XCTAssertThrowsError(try manager.setEnabled(true))
        XCTAssertFalse(manager.isEnabled())
        XCTAssertFalse(FileManager.default.fileExists(atPath: manager.plistURL.path))
    }

    func testLoginItemManagerTreatsOnlyBenignBootoutFailuresAsNonfatal() throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: home)
        }
        var launchctlCalls: [[String]] = []
        let manager = LoginItemManager(
            homeDirectory: home,
            appExists: { _ in true },
            launchctlRunner: { arguments in
                launchctlCalls.append(arguments)
                if arguments.first == "bootout" {
                    throw LoginItemError.launchctlFailed("Could not find specified service")
                }
            }
        )

        try manager.setEnabled(true)
        XCTAssertTrue(manager.isEnabled())
        XCTAssertEqual(launchctlCalls.map { $0.first }, ["bootout", "bootstrap"])

        let failing = LoginItemManager(
            homeDirectory: home,
            appExists: { _ in true },
            launchctlRunner: { arguments in
                if arguments.first == "bootout" {
                    throw LoginItemError.launchctlFailed("permission denied")
                }
            }
        )
        XCTAssertThrowsError(try failing.setEnabled(false)) { error in
            guard case LoginItemError.launchctlFailed(let message) = error else {
                XCTFail("Expected launchctl failure, got \(error)")
                return
            }
            XCTAssertEqual(message, "permission denied")
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: manager.plistURL.path))
    }

    func testLoginItemManagerSurfacesBootstrapCleanupFailure() throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: home)
        }
        let manager = LoginItemManager(
            homeDirectory: home,
            appExists: { _ in true },
            launchctlRunner: { arguments in
                if arguments.first == "bootstrap" {
                    throw LoginItemError.launchctlFailed("boom")
                }
            },
            removeItem: { _ in
                throw CocoaError(.fileWriteNoPermission)
            }
        )

        XCTAssertThrowsError(try manager.setEnabled(true)) { error in
            guard case LoginItemError.bootstrapCleanupFailed(let bootstrap, let cleanup, let plistPath) = error else {
                XCTFail("Expected bootstrap cleanup failure, got \(error)")
                return
            }
            XCTAssertTrue(bootstrap.contains("boom"))
            XCTAssertTrue(cleanup.contains("CocoaError"))
            XCTAssertEqual(plistPath, manager.plistURL.path)
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: manager.plistURL.path))
    }

    func testLoginItemStatusStateKeepsBootstrapCleanupFailureRetryable() {
        var state = LoginItemStatusState(status: .disabled)
        let error = LoginItemError.bootstrapCleanupFailed(
            bootstrap: "launchctl failed",
            cleanup: "permission denied",
            plistPath: "/tmp/com.seishirot.zenwhisper.plist"
        )

        let failedStatus = state.didFail(
            error,
            requestedEnabled: true,
            observed: .enabled
        )
        guard case .invalid(let reason) = failedStatus else {
            return XCTFail("Expected cleanup failure to remain unresolved")
        }
        XCTAssertTrue(reason.contains("cleanup also failed"))

        XCTAssertEqual(
            state.refresh(observed: .enabled),
            failedStatus,
            "A leftover matching plist must not clear the failed change"
        )
        XCTAssertEqual(state.didApply(enabled: true), .enabled)
        XCTAssertEqual(state.refresh(observed: .disabled), .disabled)
    }

    func testLoginItemStatusStateUsesObservedStatusForRecoverableFailure() {
        var state = LoginItemStatusState(status: .enabled)

        XCTAssertEqual(
            state.didFail(
                LoginItemError.launchctlFailed("permission denied"),
                requestedEnabled: true,
                observed: .disabled
            ),
            .disabled
        )
        XCTAssertEqual(state.refresh(observed: .enabled), .enabled)
    }

    func testLoginItemStatusStateLatchesAnyAmbiguousRequestedStatus() {
        var state = LoginItemStatusState(status: .disabled)

        let failedStatus = state.didFail(
            LoginItemError.permissionFailed(EACCES),
            requestedEnabled: true,
            observed: .enabled
        )
        guard case .invalid(let reason) = failedStatus else {
            return XCTFail("Expected requested-looking failed state to be invalid")
        }
        XCTAssertTrue(reason.contains("ambiguous"))
        XCTAssertEqual(state.refresh(observed: .enabled), failedStatus)
    }

    func testOnlyPersistentErrorsSurviveHotkeyRecoveryWithoutBackend() {
        XCTAssertTrue(
            AppState.modelUnavailable("missing")
                .shouldRestoreAfterHotkeyRecovery
        )
        XCTAssertTrue(
            AppState.microphoneError("denied")
                .shouldRestoreAfterHotkeyRecovery
        )
        XCTAssertFalse(AppState.inputWaiting.shouldRestoreAfterHotkeyRecovery)

        XCTAssertTrue(
            AppState.modelUnavailable("missing")
                .shouldRestoreAfterHotkeyRecoveryWithoutBackend
        )
        XCTAssertTrue(
            AppState.backendRepairRequired("broken")
                .shouldRestoreAfterHotkeyRecoveryWithoutBackend
        )
        XCTAssertTrue(
            AppState.appSignatureChanged
                .shouldRestoreAfterHotkeyRecoveryWithoutBackend
        )
        XCTAssertTrue(
            AppState.error("failed")
                .shouldRestoreAfterHotkeyRecoveryWithoutBackend
        )
        XCTAssertFalse(
            AppState.inputWaiting.shouldRestoreAfterHotkeyRecoveryWithoutBackend
        )
        XCTAssertFalse(
            AppState.microphoneError("denied")
                .shouldRestoreAfterHotkeyRecoveryWithoutBackend
        )
    }

    func testPasteTargetIdentityIgnoresMovingFrames() {
        let controller = PasteController()
        let target = pasteTarget()
        let moved = pasteTarget(
            windowFrame: CGRect(x: 500, y: 300, width: 900, height: 700),
            elementFrame: CGRect(x: 40, y: -220, width: 480, height: 80)
        )

        XCTAssertTrue(controller.sameLogicalTarget(target, moved))
        XCTAssertFalse(
            controller.sameLogicalTarget(target, pasteTarget(pid: target.pid + 1))
        )
        XCTAssertFalse(
            controller.sameLogicalTarget(
                target,
                pasteTarget(elementIdentifier: "other-editor")
            )
        )
    }

    func testPasteVerificationUsesUTF16ReplacementAndCaretMovement() {
        let before = PasteTextState(
            value: "hello world",
            selectedRange: NSRange(location: 6, length: 5)
        )
        let expectation = PasteVerificationExpectation(
            before: before,
            insertedText: "zen 🐕"
        )

        XCTAssertEqual(expectation.expectedValue, "hello zen 🐕")
        XCTAssertEqual(
            expectation.expectedCaretLocation,
            6 + ("zen 🐕" as NSString).length
        )
        XCTAssertTrue(
            expectation.isSatisfied(
                by: PasteTextState(
                    value: "hello zen 🐕",
                    selectedRange: NSRange(
                        location: 6 + ("zen 🐕" as NSString).length,
                        length: 0
                    )
                )
            )
        )
        XCTAssertFalse(expectation.isSatisfied(by: before))
    }

    func testPasteVerificationRequiresCaretAndInsertedFragment() {
        let caretAndFragment = PasteVerificationExpectation(
            before: PasteTextState(
                value: nil,
                selectedRange: NSRange(location: 4, length: 0)
            ),
            insertedText: "abc"
        )
        XCTAssertTrue(caretAndFragment.canVerify)
        XCTAssertFalse(
            caretAndFragment.isSatisfied(
                by: PasteTextState(
                    value: nil,
                    selectedRange: NSRange(location: 7, length: 0)
                )
            )
        )
        XCTAssertTrue(
            caretAndFragment.isSatisfied(
                by: PasteTextState(
                    value: nil,
                    selectedRange: NSRange(location: 7, length: 0),
                    textImmediatelyBeforeSelection: "abc"
                )
            )
        )

        let unavailable = PasteVerificationExpectation(
            before: PasteTextState(value: nil, selectedRange: nil),
            insertedText: "abc"
        )
        XCTAssertFalse(unavailable.canVerify)
    }

    func testPasteVerificationDoesNotAcceptUnchangedSameTextReplacement() {
        let before = PasteTextState(
            value: "same",
            selectedRange: NSRange(location: 0, length: 4)
        )
        let expectation = PasteVerificationExpectation(
            before: before,
            insertedText: "same"
        )

        XCTAssertFalse(expectation.isSatisfied(by: before))
        XCTAssertTrue(
            expectation.isSatisfied(
                by: PasteTextState(
                    value: "same",
                    selectedRange: NSRange(location: 4, length: 0),
                    textImmediatelyBeforeSelection: "same"
                )
            )
        )
    }

    func testPasteAttemptTimingAndOutcomeCodesAreStable() {
        let timing = PasteAttemptTiming()

        XCTAssertEqual(timing.targetRetryNanoseconds, 50_000_000)
        XCTAssertEqual(timing.targetResolutionTimeout, 0.25)
        XCTAssertEqual(timing.pasteboardSettleNanoseconds, 50_000_000)
        XCTAssertEqual(timing.keyUpDelayNanoseconds, 20_000_000)
        XCTAssertEqual(timing.verificationPollNanoseconds, 50_000_000)
        XCTAssertEqual(timing.verificationTimeout, 5)
        XCTAssertEqual(timing.submitDelayNanoseconds, 100_000_000)
        XCTAssertEqual(
            PasteFailureReason.verificationTimedOut.logCode,
            "verification_timed_out"
        )
        XCTAssertEqual(
            PasteClipboardDisposition.kept.logCode,
            "kept"
        )
        XCTAssertEqual(PasteSubmitResult.skippedTargetChanged.logCode, "skipped_target_changed")
    }

    func testPasteKeyCodeResolutionUsesTranslatedLayoutWithoutFixedFallback() {
        var observedModifierStates: [UInt32] = []
        let commandState = UInt32(cmdKey >> 8)
        XCTAssertEqual(
            KeyboardLayoutKeyCodeResolver.keyCode(
                for: "v",
                modifierState: commandState,
                translating: { keyCode, modifierState in
                    observedModifierStates.append(modifierState)
                    return keyCode == 42 && modifierState == commandState
                        ? 118
                        : nil
                }
            ),
            42
        )
        XCTAssertEqual(Set(observedModifierStates), [commandState])
        XCTAssertNil(
            KeyboardLayoutKeyCodeResolver.keyCode(
                for: "v",
                modifierState: commandState,
                translating: { _, _ in nil }
            )
        )
    }

    func testPasteControllerPostsKeyPairOnlyToApprovedPID() throws {
        let keyDown = try XCTUnwrap(
            CGEvent(
                keyboardEventSource: nil,
                virtualKey: 41,
                keyDown: true
            )
        )
        let keyUp = try XCTUnwrap(
            CGEvent(
                keyboardEventSource: nil,
                virtualKey: 41,
                keyDown: false
            )
        )
        var posts: [(CGEventType, pid_t)] = []
        let controller = PasteController(
            postEventToPID: { event, pid in
                posts.append((event.type, pid))
            }
        )
        let pair = PasteKeyEventPair(
            testVirtualKey: 41,
            keyDown: keyDown,
            keyUp: keyUp
        )

        controller.postKeyDown(pair, to: 4242)
        controller.postKeyUp(pair, to: 4242)

        XCTAssertEqual(posts.map(\.0), [.keyDown, .keyUp])
        XCTAssertEqual(posts.map(\.1), [4242, 4242])
    }

    func testTransientAXErrorsAreNotTreatedAsMissingAttributes() {
        XCTAssertTrue(AXError.attributeUnsupported.isBenignMissingAttribute)
        XCTAssertTrue(AXError.noValue.isBenignMissingAttribute)
        XCTAssertFalse(AXError.cannotComplete.isBenignMissingAttribute)
        XCTAssertFalse(AXError.invalidUIElement.isBenignMissingAttribute)

        XCTAssertEqual(
            resolvePasteTargetSafetyAttributes(
                role: .value("AXTextArea"),
                subrole: .missing,
                enabled: .missing,
                protectedContent: .missing
            ),
            .resolved(
                PasteTargetSafetyAttributes(
                    role: "AXTextArea",
                    subrole: "",
                    enabled: true,
                    isProtectedContent: false
                )
            )
        )
        XCTAssertEqual(
            resolvePasteTargetSafetyAttributes(
                role: .value("AXTextArea"),
                subrole: .missing,
                enabled: .missing,
                protectedContent: .failed(
                    "error=\(AXError.cannotComplete.rawValue)"
                )
            ),
            .retry("protected=error=\(AXError.cannotComplete.rawValue)")
        )
        XCTAssertEqual(
            resolvePasteTargetSafetyAttributes(
                role: .value("AXTextArea"),
                subrole: .failed("wrongType"),
                enabled: .value(true),
                protectedContent: .value(false)
            ),
            .retry("subrole=wrongType")
        )
    }

    func testPasteboardWriteNeverAutomaticallyRestoresPreviousItems() throws {
        let pasteboard = NSPasteboard(
            name: NSPasteboard.Name("zen-whisper-tests.\(UUID().uuidString)")
        )
        pasteboard.clearContents()
        let customType = NSPasteboard.PasteboardType("test.custom.binary")
        let first = NSPasteboardItem()
        XCTAssertTrue(first.setString("original", forType: .string))
        XCTAssertTrue(first.setData(Data([0, 1, 2, 255]), forType: customType))
        let second = NSPasteboardItem()
        XCTAssertTrue(second.setString("second", forType: .string))
        XCTAssertTrue(pasteboard.writeObjects([first, second]))
        let controller = PasteController(pasteboard: pasteboard)

        let writeResult = controller.prepareAutoPaste(
            "transcript",
            attemptID: UUID()
        )
        guard case .success = writeResult else {
            return XCTFail("expected prepared pasteboard transaction")
        }
        XCTAssertEqual(pasteboard.string(forType: .string), "transcript")
        let current = try XCTUnwrap(pasteboard.pasteboardItems)
        XCTAssertEqual(current.count, 1)
        XCTAssertNil(current[0].data(forType: customType))
    }

    func testFocusedTargetProbeFailsClosedWhenAXTimeoutCannotBeConfigured() {
        var configuredTimeout: Float?
        let controller = PasteController(
            configureAXMessagingTimeout: { _, timeout in
                configuredTimeout = timeout
                return .cannotComplete
            }
        )

        let probe = controller.snapshotFocusedTargetProbe()

        XCTAssertTrue(probe.isSafetyIndeterminate)
        XCTAssertTrue(probe.detail.contains("deadlineProtectionUnavailable"))
        XCTAssertEqual(configuredTimeout, 0.05)
    }

    func testFocusedTargetProbeFailsClosedAtOverallDeadline() {
        var times: [TimeInterval] = [0, 0.151]
        let controller = PasteController(
            configureAXMessagingTimeout: { _, _ in .success },
            monotonicNow: { times.removeFirst() }
        )

        let probe = controller.snapshotFocusedTargetProbe()

        XCTAssertTrue(probe.isSafetyIndeterminate)
        XCTAssertTrue(probe.detail.contains("probeDeadlineExceeded"))
    }

    func testPasteboardOwnershipRejectsExternalSameTextWrite() throws {
        let pasteboard = NSPasteboard(
            name: NSPasteboard.Name("zen-whisper-tests.\(UUID().uuidString)")
        )
        pasteboard.clearContents()
        XCTAssertTrue(pasteboard.setString("original", forType: .string))
        let controller = PasteController(pasteboard: pasteboard)

        let writeResult = controller.prepareAutoPaste(
            "transcript",
            attemptID: UUID()
        )
        guard case .success(let token) = writeResult else {
            return XCTFail("expected prepared pasteboard transaction")
        }
        XCTAssertTrue(controller.ownsPasteboard(token))
        pasteboard.clearContents()
        XCTAssertTrue(pasteboard.setString("transcript", forType: .string))

        XCTAssertFalse(controller.ownsPasteboard(token))
        XCTAssertEqual(pasteboard.string(forType: .string), "transcript")
    }

    func testPasteboardWriteFailureReportsUnavailableOriginalWhenClearIsOwned() {
        let ownedClearedPasteboard = NSPasteboard(
            name: NSPasteboard.Name("zen-whisper-tests.\(UUID().uuidString)")
        )
        ownedClearedPasteboard.clearContents()
        XCTAssertTrue(ownedClearedPasteboard.setString("original", forType: .string))
        let failingController = PasteController(
            pasteboard: ownedClearedPasteboard,
            writePasteboardItems: { _, _ in false }
        )

        guard case .writeFailed(let unavailableDisposition) =
            failingController.prepareAutoPaste("transcript") else {
            return XCTFail("expected a simulated pasteboard write failure")
        }
        XCTAssertEqual(unavailableDisposition, .originalUnavailable)
        XCTAssertNil(ownedClearedPasteboard.string(forType: .string))

        let changedPasteboard = NSPasteboard(
            name: NSPasteboard.Name("zen-whisper-tests.\(UUID().uuidString)")
        )
        changedPasteboard.clearContents()
        XCTAssertTrue(changedPasteboard.setString("original", forType: .string))
        let changedController = PasteController(
            pasteboard: changedPasteboard,
            writePasteboardItems: { pasteboard, _ in
                pasteboard.clearContents()
                XCTAssertTrue(
                    pasteboard.setString("external", forType: .string)
                )
                return false
            }
        )

        guard case .writeFailed(let changedDisposition) =
            changedController.prepareAutoPaste("transcript") else {
            return XCTFail("expected a simulated pasteboard write failure")
        }
        XCTAssertEqual(changedDisposition, .externalChangePreserved)
        XCTAssertEqual(
            changedPasteboard.string(forType: .string),
            "external"
        )
    }

    func testPlainTextWriteFailurePreservesExternalClipboardChange() {
        let pasteboard = NSPasteboard(
            name: NSPasteboard.Name(
                "zen-whisper-tests.\(UUID().uuidString)"
            )
        )
        pasteboard.clearContents()
        XCTAssertTrue(pasteboard.setString("original", forType: .string))
        let controller = PasteController(
            pasteboard: pasteboard,
            writePasteboardItems: { pasteboard, _ in
                pasteboard.clearContents()
                XCTAssertTrue(
                    pasteboard.setString("external", forType: .string)
                )
                return false
            }
        )

        guard case .writeFailed(let disposition) =
            controller.copyPlainText("transcript") else {
            return XCTFail("expected a simulated plain-text write failure")
        }
        XCTAssertEqual(disposition, .externalChangePreserved)
        XCTAssertEqual(
            pasteboard.string(forType: .string),
            "external"
        )
    }

    func testPasteboardClearCountCannotClaimConcurrentExternalWrite() {
        func makeController() -> (
            NSPasteboard,
            PasteController
        ) {
            let pasteboard = NSPasteboard(
                name: NSPasteboard.Name(
                    "zen-whisper-tests.\(UUID().uuidString)"
                )
            )
            pasteboard.clearContents()
            XCTAssertTrue(
                pasteboard.setString("original", forType: .string)
            )
            let controller = PasteController(
                pasteboard: pasteboard,
                clearPasteboard: { pasteboard in
                    let ownedChangeCount = pasteboard.clearContents()
                    pasteboard.clearContents()
                    XCTAssertTrue(
                        pasteboard.setString(
                            "external-after-clear",
                            forType: .string
                        )
                    )
                    return ownedChangeCount
                },
                writePasteboardItems: { _, _ in false }
            )
            return (pasteboard, controller)
        }

        let (autoPasteboard, autoController) = makeController()
        guard case .writeFailed(let autoDisposition) =
            autoController.prepareAutoPaste("transcript") else {
            return XCTFail("expected auto-paste write failure")
        }
        XCTAssertEqual(autoDisposition, .externalChangePreserved)
        XCTAssertEqual(
            autoPasteboard.string(forType: .string),
            "external-after-clear"
        )

        let (plainPasteboard, plainController) = makeController()
        guard case .writeFailed(let plainDisposition) =
            plainController.copyPlainText("transcript") else {
            return XCTFail("expected plain-text write failure")
        }
        XCTAssertEqual(plainDisposition, .externalChangePreserved)
        XCTAssertEqual(
            plainPasteboard.string(forType: .string),
            "external-after-clear"
        )
    }

    func testPasteboardWriteFailureDoesNotAttemptDestructiveRestore() {
        let pasteboard = NSPasteboard(
            name: NSPasteboard.Name(
                "zen-whisper-tests.\(UUID().uuidString)"
            )
        )
        pasteboard.clearContents()
        XCTAssertTrue(pasteboard.setString("original", forType: .string))
        let controller = PasteController(
            pasteboard: pasteboard,
            writePasteboardItems: { _, _ in false }
        )

        guard case .writeFailed(let disposition) =
            controller.prepareAutoPaste("transcript") else {
            return XCTFail("expected a simulated pasteboard write failure")
        }
        XCTAssertEqual(disposition, .originalUnavailable)
        XCTAssertNil(pasteboard.string(forType: .string))
    }

    func testPasteTreeSearchStopsWhenProbeDeadlineExpires() {
        var checks = 0
        let result: PasteTreeSearchResult<Int> = searchPasteTree(
            root: 0,
            maxDepth: 10,
            maxNodes: 300,
            nodeKey: { $0 },
            inspect: { node in
                PasteTreeNodeObservation(
                    focused: .value(false),
                    resolution: .noCandidate(detail: "node=\(node)")
                )
            },
            readChildren: { node in
                [PasteTreeChildRead(nodes: [node + 1], failureDetail: nil)]
            },
            shouldContinue: {
                checks += 1
                return checks < 4
            }
        )

        guard case .safetyIndeterminate(let detail) = result else {
            return XCTFail("expected deadline to stop traversal")
        }
        XCTAssertTrue(detail.contains("probeDeadlineExceeded"))
    }

    func testPasteEligibilityRejectsProtectedAndNonEditableTargets() {
        let controller = PasteController()

        XCTAssertTrue(controller.isEligible(pasteTarget()))
        XCTAssertFalse(controller.isEligible(pasteTarget(hasEditableValue: false)))
        XCTAssertFalse(controller.isEligible(pasteTarget(isProtectedContent: true)))
        XCTAssertFalse(controller.isEligible(pasteTarget(role: "AXSecureTextField")))
        XCTAssertFalse(
            controller.isEligible(
                pasteTarget(
                    role: "AXGroup",
                    hasEditableValue: false,
                    canSetSelectedText: true
                )
            )
        )
        XCTAssertFalse(controller.isEligible(pasteTarget(role: "AXWebArea")))
        XCTAssertTrue(
            controller.isEligible(
                pasteTarget(
                    role: "AXWebArea",
                    hasEditableValue: false,
                    canSetSelectedText: true
                )
            )
        )
        XCTAssertTrue(
            controller.isEligible(
                pasteTarget(
                    hasEditableValue: false,
                    hasReadableSelectedTextRange: true
                )
            )
        )
        XCTAssertFalse(controller.isUnsafeForClipboard(pasteTarget(searchableText: "pinboard")))
        XCTAssertTrue(controller.isUnsafeForClipboard(pasteTarget(searchableText: "pin")))
        XCTAssertFalse(controller.isEligible(pasteTarget(searchableText: "password")))
        XCTAssertFalse(controller.isEligible(pasteTarget(searchableText: "api key")))
        XCTAssertFalse(controller.isEligible(pasteTarget(searchableText: "認証コード")))
        XCTAssertTrue(controller.isEligible(pasteTarget(subrole: "AXSearchField")))
    }

    func testDispatchFocusValidationRejectsCurrentProtectedTarget() {
        let controller = PasteController()
        let approved = pasteTarget()

        XCTAssertEqual(
            controller.dispatchFocusValidation(
                approved: approved,
                current: approved,
                elementsEqual: true
            ),
            .matched
        )
        XCTAssertEqual(
            controller.dispatchFocusValidation(
                approved: approved,
                current: pasteTarget(isProtectedContent: true),
                elementsEqual: true
            ),
            .unsafe
        )
        XCTAssertEqual(
            controller.dispatchFocusValidation(
                approved: approved,
                current: pasteTarget(role: "AXSecureTextField"),
                elementsEqual: true
            ),
            .unsafe
        )
    }

    func testDescendantFocusFailureIsIndeterminateForPasteCandidate() {
        XCTAssertEqual(
            resolvePasteDescendantFocus(
                focused: .failed("error=-25204"),
                candidateCouldReceivePaste: true
            ),
            .safetyIndeterminate("focusedState=error=-25204")
        )
        XCTAssertEqual(
            resolvePasteDescendantFocus(
                focused: .failed("error=-25204"),
                candidateCouldReceivePaste: false
            ),
            .focused(false)
        )
        XCTAssertEqual(
            resolvePasteDescendantFocus(
                focused: .missing,
                candidateCouldReceivePaste: true
            ),
            .safetyIndeterminate("focusedState=missing")
        )
        XCTAssertEqual(
            resolvePasteDescendantFocus(
                focused: .value(true),
                candidateCouldReceivePaste: true
            ),
            .focused(true)
        )
        XCTAssertFalse(
            pasteTreeSearchWasIncomplete(
                queuedNodeCount: 0,
                depthWasTruncated: false
            )
        )
        XCTAssertTrue(
            pasteTreeSearchWasIncomplete(
                queuedNodeCount: 1,
                depthWasTruncated: false
            )
        )
        XCTAssertTrue(
            pasteTreeSearchWasIncomplete(
                queuedNodeCount: 0,
                depthWasTruncated: true
            )
        )
    }

    func testPasteTreeTraversalPropagatesChildReadFailure() {
        let result: PasteTreeSearchResult<Int> = searchPasteTree(
            root: 0,
            maxDepth: 10,
            maxNodes: 300,
            nodeKey: { $0 },
            inspect: { _ in
                PasteTreeNodeObservation(
                    focused: .value(false),
                    resolution: .noCandidate(detail: "notEditable")
                )
            },
            readChildren: { _ in
                [
                    PasteTreeChildRead(
                        nodes: [],
                        failureDetail: "childrenAttribute=AXChildren error=-25204 wrongType=false"
                    )
                ]
            }
        )

        guard case .safetyIndeterminate(let detail) = result else {
            return XCTFail("expected child read failure to be indeterminate")
        }
        XCTAssertTrue(detail.contains("childrenAttribute=AXChildren"))
        XCTAssertTrue(detail.contains("visited=1"))
    }

    func testPasteTreeTraversalFailsClosedWhenDepthLimitTruncates() {
        let result: PasteTreeSearchResult<Int> = searchPasteTree(
            root: 0,
            maxDepth: 0,
            maxNodes: 300,
            nodeKey: { $0 },
            inspect: { _ in
                PasteTreeNodeObservation(
                    focused: .value(false),
                    resolution: .noCandidate(detail: "notEditable")
                )
            },
            readChildren: { node in
                [
                    PasteTreeChildRead(
                        nodes: node == 0 ? [1] : [],
                        failureDetail: nil
                    )
                ]
            }
        )

        guard case .safetyIndeterminate(let detail) = result else {
            return XCTFail("expected depth truncation to be indeterminate")
        }
        XCTAssertTrue(detail.contains("depthTruncated=true"))
    }

    func testPasteTreeTraversalFailsClosedWhenNodeLimitTruncates() {
        let result: PasteTreeSearchResult<Int> = searchPasteTree(
            root: 0,
            maxDepth: 10,
            maxNodes: 1,
            nodeKey: { $0 },
            inspect: { _ in
                PasteTreeNodeObservation(
                    focused: .value(false),
                    resolution: .noCandidate(detail: "notEditable")
                )
            },
            readChildren: { node in
                [
                    PasteTreeChildRead(
                        nodes: node == 0 ? [1] : [],
                        failureDetail: nil
                    )
                ]
            }
        )

        guard case .safetyIndeterminate(let detail) = result else {
            return XCTFail("expected node truncation to be indeterminate")
        }
        XCTAssertTrue(detail.contains("queued=1"))
        XCTAssertTrue(detail.contains("visited=1"))
    }

    func testPasteTreeTraversalResolvesFocusedCandidatesAndMissingFocus() {
        for kind in [
            PasteTreeCandidateKind.eligible,
            PasteTreeCandidateKind.unsafe
        ] {
            let result: PasteTreeSearchResult<String> = searchPasteTree(
                root: 0,
                maxDepth: 10,
                maxNodes: 300,
                nodeKey: { $0 },
                inspect: { _ in
                    PasteTreeNodeObservation(
                        focused: .value(true),
                        resolution: .candidate(
                            context: "target",
                            kind: kind,
                            sample: "role=AXTextArea",
                            foundDetail: "redacted"
                        )
                    )
                },
                readChildren: { _ in [] }
            )
            guard case .found(let context, let detail) = result else {
                return XCTFail(
                    "expected focused \(kind) candidate to be found"
                )
            }
            XCTAssertEqual(context, "target")
            XCTAssertTrue(detail.contains("foundFocused"))
        }

        let unfocused: PasteTreeSearchResult<String> = searchPasteTree(
            root: 0,
            maxDepth: 10,
            maxNodes: 300,
            nodeKey: { $0 },
            inspect: { _ in
                PasteTreeNodeObservation(
                    focused: .value(false),
                    resolution: .candidate(
                        context: "target",
                        kind: .eligible,
                        sample: "role=AXTextArea",
                        foundDetail: "redacted"
                    )
                )
            },
            readChildren: { _ in [] }
        )
        guard case .noTarget(let unfocusedDetail) = unfocused else {
            return XCTFail("expected unfocused candidate not to be selected")
        }
        XCTAssertTrue(
            unfocusedDetail.contains(
                "noFocusedCandidate eligible=1 unsafe=0"
            )
        )

        let missingFocus: PasteTreeSearchResult<String> = searchPasteTree(
            root: 0,
            maxDepth: 10,
            maxNodes: 300,
            nodeKey: { $0 },
            inspect: { _ in
                PasteTreeNodeObservation(
                    focused: .missing,
                    resolution: .candidate(
                        context: "target",
                        kind: .eligible,
                        sample: "role=AXTextArea",
                        foundDetail: "redacted"
                    )
                )
            },
            readChildren: { _ in [] }
        )
        guard case .safetyIndeterminate(let missingDetail) = missingFocus else {
            return XCTFail("expected missing candidate focus to fail closed")
        }
        XCTAssertEqual(missingDetail, "focusedState=missing")
    }

    func testPasteTreeTraversalAllowsExactDepthAndNodeLimits() {
        func exactLimitResult(
            maxDepth: Int,
            maxNodes: Int
        ) -> PasteTreeSearchResult<Int> {
            searchPasteTree(
                root: 0,
                maxDepth: maxDepth,
                maxNodes: maxNodes,
                nodeKey: { $0 },
                inspect: { _ in
                    PasteTreeNodeObservation(
                        focused: .value(false),
                        resolution: .noCandidate(
                            detail: "notEditable"
                        )
                    )
                },
                readChildren: { node in
                    [
                        PasteTreeChildRead(
                            nodes: node == 0 ? [1] : [],
                            failureDetail: nil
                        )
                    ]
                }
            )
        }

        guard case .noTarget(let depthDetail) = exactLimitResult(
            maxDepth: 1,
            maxNodes: 300
        ) else {
            return XCTFail("expected exact depth limit to complete")
        }
        XCTAssertTrue(depthDetail.contains("visited=2"))

        guard case .noTarget(let nodeDetail) = exactLimitResult(
            maxDepth: 10,
            maxNodes: 2
        ) else {
            return XCTFail("expected exact node limit to complete")
        }
        XCTAssertTrue(nodeDetail.contains("visited=2"))
    }

    private func pasteTarget(
        pid: pid_t = 123,
        bundleIdentifier: String = "com.example.target",
        role: String = "AXTextArea",
        subrole: String = "",
        windowTitle: String = "Document",
        windowFrame: CGRect = CGRect(x: 0, y: 0, width: 600, height: 400),
        elementIdentifier: String = "editor",
        elementFrame: CGRect = CGRect(x: 20, y: 20, width: 200, height: 28),
        hasEditableValue: Bool = true,
        canSetSelectedText: Bool = false,
        canSetSelectedTextRange: Bool = false,
        hasReadableValue: Bool = true,
        hasReadableSelectedTextRange: Bool = false,
        isProtectedContent: Bool = false,
        searchableText: String = "body",
        discovery: String = "focused"
    ) -> PasteTargetSnapshot {
        PasteTargetSnapshot(
            pid: pid,
            bundleIdentifier: bundleIdentifier,
            role: role,
            subrole: subrole,
            windowTitle: windowTitle,
            windowFrame: windowFrame,
            elementIdentifier: elementIdentifier,
            elementFrame: elementFrame,
            hasEditableValue: hasEditableValue,
            canSetSelectedText: canSetSelectedText,
            canSetSelectedTextRange: canSetSelectedTextRange,
            hasReadableValue: hasReadableValue,
            hasReadableSelectedTextRange: hasReadableSelectedTextRange,
            isProtectedContent: isProtectedContent,
            searchableText: searchableText,
            discovery: discovery
        )
    }

    private func registryJSON(
        version: Int = 1,
        duplicateEngine: Bool = false,
        duplicateModel: Bool = false,
        unknownLanguageEngine: Bool = false,
        blankEngineLabel: Bool = false,
        blankModelLabel: Bool = false,
        blankLanguageLabel: Bool = false,
        blankLanguageID: Bool = false,
        defaultLanguageMissingDefaultEngine: Bool = false,
        engineWithoutLanguage: Bool = false
    ) throws -> URL {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("zen-whisper-registry-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let url = root.appendingPathComponent("model_registry.json")
        let secondEngine = duplicateEngine
            ? """
            ,{"id":"mlx-whisper","label":"Duplicate","default_model":"model-a","models":[{"id":"model-a","label":"A"}]}
            """
            : engineWithoutLanguage
                ? """
                ,{"id":"mlx-qwen3-asr","label":"MLX Qwen3-ASR","default_model":"model-q","models":[{"id":"model-q","label":"Q"}]}
                """
            : ""
        let secondModel = duplicateModel ? #",{"id":"model-a","label":"Duplicate A"}"# : ""
        let languageEngine = unknownLanguageEngine
            ? "missing"
            : defaultLanguageMissingDefaultEngine ? "" : "mlx-whisper"
        let engineLabel = blankEngineLabel ? "" : "MLX Whisper"
        let modelLabel = blankModelLabel ? "" : "A"
        let languageLabel = blankLanguageLabel ? "" : "Japanese"
        let extraLanguage = blankLanguageID
            ? #","": {"label": "Blank", "engines": {"mlx-whisper": "ja"}}"#
            : ""
        let json = """
        {
          "version": \(version),
          "default_engine": "mlx-whisper",
          "default_language": "ja",
          "languages": {
            "ja": {"label": "\(languageLabel)", "engines": {\(languageEngine.isEmpty ? "" : "\"\(languageEngine)\": \"ja\"")}}\(extraLanguage)
          },
          "engines": [
            {
              "id": "mlx-whisper",
              "label": "\(engineLabel)",
              "default_model": "model-a",
              "models": [
                {"id": "model-a", "label": "\(modelLabel)"}\(secondModel)
              ]
            }
            \(secondEngine)
          ]
        }
        """
        try json.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    private struct BackendInstallFixture {
        let paths: AppPaths
        let validator: BackendInstallValidator
    }

    private func makePerUserBackendInstallFixture() throws -> BackendInstallFixture {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("zen-whisper-validator-\(UUID().uuidString)", isDirectory: true)
        let app = root.appendingPathComponent("zen-whisper.app", isDirectory: true)
        let appBackend = app.appendingPathComponent("Contents/Resources/backend", isDirectory: true)
        let appSupport = root.appendingPathComponent("support", isDirectory: true)
        let paths = AppPaths(
            appSupport: appSupport,
            logs: root.appendingPathComponent("logs", isDirectory: true)
        )
        try FileManager.default.createDirectory(at: appBackend, withIntermediateDirectories: true)
        try paths.prepare()
        try FileManager.default.createDirectory(
            at: appSupport.appendingPathComponent("backend", isDirectory: true),
            withIntermediateDirectories: true
        )

        let runtimeRoot = appSupport.appendingPathComponent("runtime", isDirectory: true)
        let runtimeBin = runtimeRoot.appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: runtimeBin, withIntermediateDirectories: true)
        let runtimePython = runtimeBin.appendingPathComponent("python")
        let runtimeScript = """
        #!/bin/sh
        printf '{"protocol_version":1,"backend_version":"0.1-test","python_arch":"arm64","registry_hash":"REGISTRY_HASH","backend_code_hash":"BACKEND_CODE_HASH"}\\n'
        """
        try runtimeScript.write(to: runtimePython, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: runtimePython.path)

        let venvRoot = appSupport.appendingPathComponent("backend/.venv", isDirectory: true)
        let venvBin = venvRoot.appendingPathComponent("bin", isDirectory: true)
        let package = venvRoot
            .appendingPathComponent("lib/python3.12/site-packages/zen_whisper_mac_backend", isDirectory: true)
        let resources = package.appendingPathComponent("resources", isDirectory: true)
        try FileManager.default.createDirectory(at: venvBin, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(
            at: venvBin.appendingPathComponent("python"),
            withDestinationURL: runtimePython
        )
        let initFile = package.appendingPathComponent("__init__.py")
        try "BACKEND_VERSION = '0.1-test'\nPROTOCOL_VERSION = 1\n"
            .write(to: initFile, atomically: true, encoding: .utf8)
        let registry = resources.appendingPathComponent("model_registry.json")
        try "{\"engines\":[]}\n".write(to: registry, atomically: true, encoding: .utf8)

        let actualBackendCodeHash = try testBackendCodeHash(packageDir: package)
        let actualRegistryHash = try testSHA256(file: registry)
        let patchedRuntimeScript = runtimeScript
            .replacingOccurrences(of: "REGISTRY_HASH", with: actualRegistryHash)
            .replacingOccurrences(of: "BACKEND_CODE_HASH", with: actualBackendCodeHash)
        try patchedRuntimeScript.write(to: runtimePython, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: runtimePython.path)

        let venvEntries = try testManifestEntries(root: venvRoot)
        let venvManifest = appSupport.appendingPathComponent("backend/venv_manifest.json")
        try writeManifest(entries: venvEntries, rootPath: nil, to: venvManifest)
        let venvManifestHash = try testSHA256(file: venvManifest)

        let runtimeEntries = try testManifestEntries(root: runtimeRoot)
        let runtimeManifest = appSupport.appendingPathComponent("backend/python_runtime_manifest.json")
        try writeManifest(entries: runtimeEntries, rootPath: runtimeRoot.path, to: runtimeManifest)
        let runtimeManifestHash = try testSHA256(file: runtimeManifest)
        let pythonSourceHash = try testSHA256(file: runtimePython)

        let wheelHash = "wheel-hash"
        let requirementsHash = "requirements-hash"
        try writeJSONObject([
            "backend_code_hash": actualBackendCodeHash,
            "backend_version": "0.1-test",
            "protocol_version": 1,
            "python_runtime_manifest_hash": "per-user",
            "registry_hash": actualRegistryHash,
            "requirements_hash": requirementsHash,
            "venv_manifest_hash": "per-user",
            "wheel_hash": wheelHash
        ], to: appBackend.appendingPathComponent("BackendBundleManifest.json"))

        try writeJSONObject([
            "app_path": app.path,
            "backend_code_hash": actualBackendCodeHash,
            "backend_version": "0.1-test",
            "protocol_version": 1,
            "python_arch": "arm64",
            "python_path": paths.backendPython.path,
            "python_runtime_manifest_hash": runtimeManifestHash,
            "python_runtime_prefix": runtimeRoot.path,
            "python_source": runtimePython.path,
            "python_source_sha256": pythonSourceHash,
            "registry_hash": actualRegistryHash,
            "requirements_hash": requirementsHash,
            "venv_manifest_hash": venvManifestHash,
            "wheel_hash": wheelHash
        ], to: appSupport.appendingPathComponent("backend/install.json"))

        return BackendInstallFixture(
            paths: paths,
            validator: BackendInstallValidator(paths: paths, bundleURL: app)
        )
    }

    private func writeManifest(
        entries: [BackendVenvManifestEntry],
        rootPath: String?,
        to url: URL
    ) throws {
        var payload: [String: Any] = [
            "files": entries.map { entry -> [String: Any] in
                var item: [String: Any] = [
                    "kind": entry.kind,
                    "mode": entry.mode,
                    "path": entry.path
                ]
                if let sha256 = entry.sha256 {
                    item["sha256"] = sha256
                }
                if let target = entry.target {
                    item["target"] = target
                }
                return item
            },
            "root_hash": rootHash(entries: entries),
            "schema_version": 1
        ]
        if let rootPath {
            payload["root_path"] = rootPath
        }
        try writeJSONObject(payload, to: url)
    }

    private func writeJSONObject(_ object: [String: Any], to url: URL) throws {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: .atomic)
    }

    private func rewriteVenvManifestAndInstallHash(_ paths: AppPaths) throws {
        let venvRoot = paths.appSupport.appendingPathComponent("backend/.venv", isDirectory: true)
        let venvManifest = paths.appSupport.appendingPathComponent("backend/venv_manifest.json")
        try writeManifest(entries: testManifestEntries(root: venvRoot), rootPath: nil, to: venvManifest)
        let hash = try testSHA256(file: venvManifest)
        try updateInstallRecord(paths) { install in
            install["venv_manifest_hash"] = hash
        }
    }

    private func rewriteRuntimeManifestAndInstallHash(_ paths: AppPaths) throws {
        let installURL = paths.appSupport.appendingPathComponent("backend/install.json")
        let data = try Data(contentsOf: installURL)
        guard let install = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let runtimePrefix = install["python_runtime_prefix"] as? String else {
            throw NSError(domain: "test", code: 3)
        }
        let runtimeRoot = URL(fileURLWithPath: runtimePrefix, isDirectory: true)
        let runtimeManifest = paths.appSupport.appendingPathComponent("backend/python_runtime_manifest.json")
        try writeManifest(entries: testManifestEntries(root: runtimeRoot), rootPath: runtimeRoot.path, to: runtimeManifest)
        let hash = try testSHA256(file: runtimeManifest)
        try updateInstallRecord(paths) { install in
            install["python_runtime_manifest_hash"] = hash
        }
    }

    private func updateInstallRecord(
        _ paths: AppPaths,
        update: (inout [String: Any]) -> Void
    ) throws {
        let installURL = paths.appSupport.appendingPathComponent("backend/install.json")
        let data = try Data(contentsOf: installURL)
        guard var install = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NSError(domain: "test", code: 2)
        }
        update(&install)
        try writeJSONObject(install, to: installURL)
    }

    private func testManifestEntries(root: URL) throws -> [BackendVenvManifestEntry] {
        let fm = FileManager.default
        let files = try fm.subpathsOfDirectory(atPath: root.path).sorted()
        return try files.compactMap { relative in
            let url = root.appendingPathComponent(relative)
            var info = stat()
            guard lstat(url.path, &info) == 0 else {
                throw NSError(domain: "test", code: 1)
            }
            let mode = String(format: "0o%o", info.st_mode & 0o7777)
            let type = info.st_mode & S_IFMT
            if type == S_IFDIR {
                return nil
            }
            if type == S_IFLNK {
                return BackendVenvManifestEntry(
                    path: relative,
                    kind: "symlink",
                    mode: mode,
                    sha256: nil,
                    target: try fm.destinationOfSymbolicLink(atPath: url.path)
                )
            }
            return BackendVenvManifestEntry(
                path: relative,
                kind: "file",
                mode: mode,
                sha256: try testSHA256(file: url),
                target: nil
            )
        }
    }

    private func testBackendCodeHash(packageDir: URL) throws -> String {
        let fm = FileManager.default
        let files = try fm.subpathsOfDirectory(atPath: packageDir.path)
            .filter { !$0.split(separator: "/").contains("__pycache__") && !$0.hasSuffix(".pyc") }
            .sorted()
        var digest = SHA256()
        for relative in files {
            let url = packageDir.appendingPathComponent(relative)
            var isDirectory: ObjCBool = false
            guard fm.fileExists(atPath: url.path, isDirectory: &isDirectory), !isDirectory.boolValue else {
                continue
            }
            digest.update(data: Data("zen_whisper_mac_backend/\(relative)\0\(try testSHA256(file: url))\n".utf8))
        }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private func testSHA256(file url: URL) throws -> String {
        let data = try Data(contentsOf: url)
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func exitedProcess(status: Int32) throws -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", "exit \(status)"]
        try process.run()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, status)
        return process
    }
}

private extension String {
    func append(to url: URL) throws {
        let handle = try FileHandle(forWritingTo: url)
        defer {
            try? handle.close()
        }
        try handle.seekToEnd()
        try handle.write(contentsOf: Data(utf8))
    }
}

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
        XCTAssertEqual(StatusIconFactory.kind(for: .copySkipped("x")), .warning)
        XCTAssertEqual(StatusIconFactory.kind(for: .copyFailed("x")), .warning)
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
        XCTAssertEqual(HotkeyShortcut.controlOptionCommandReturn.label, "Ctrl+Option+Cmd+Return")
        XCTAssertEqual(HotkeyShortcut.controlOptionCommandReturn.storageValue, "ctrl+option+cmd+return")
        XCTAssertEqual(HotkeyShortcut.submitPresets, [.controlOptionCommandReturn])
        XCTAssertNil(HotkeyShortcut.optionalFromStorageValue(""))
        XCTAssertNil(HotkeyShortcut.optionalFromStorageValue("off"))
        XCTAssertEqual(
            HotkeyShortcut.optionalFromStorageValue("ctrl+option+cmd+return"),
            .controlOptionCommandReturn
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
            "Paste + Enter"
        )
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
    }

    func testBackendErrorPreservesRecoverableFlag() throws {
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

    func testPasteDecisionRequiresCompleteStableTargetHistory() {
        let controller = PasteController()
        let target = pasteTarget()
        let changed = pasteTarget(elementFrame: CGRect(x: 40, y: 20, width: 200, height: 28))

        XCTAssertEqual(
            controller.decide(start: nil, stop: target, current: target),
            .copyOnly("missing recording start AX target")
        )
        XCTAssertEqual(
            controller.decide(start: target, stop: nil, current: target),
            .copyOnly("missing recording stop AX target")
        )
        XCTAssertEqual(
            controller.decide(start: nil, stop: nil, current: target),
            .copyOnly("missing recording start AX target")
        )
        XCTAssertEqual(
            controller.decide(start: target, stop: target, current: nil),
            .copyOnly("missing current AX target")
        )
        XCTAssertEqual(
            controller.decide(start: target, stop: changed, current: changed),
            .copyOnly("target changed during recording")
        )
        XCTAssertEqual(
            controller.decide(start: target, stop: target, current: changed),
            .copyOnly("target changed")
        )
        let ineligible = pasteTarget(hasEditableValue: false)
        XCTAssertEqual(
            controller.decide(start: ineligible, stop: ineligible, current: ineligible),
            .copyOnly("target is not editable")
        )
        XCTAssertEqual(
            controller.decide(start: ineligible, stop: target, current: target),
            .copyOnly("target is not editable")
        )
        let unsafe = pasteTarget(role: "AXSecureTextField")
        XCTAssertEqual(
            controller.decide(start: unsafe, stop: unsafe, current: unsafe),
            .skipCopy("target is unsafe")
        )
        XCTAssertEqual(
            controller.decide(start: unsafe, stop: target, current: target),
            .skipCopy("target is unsafe")
        )
        XCTAssertEqual(
            controller.decide(start: unsafe, stop: nil, current: target),
            .skipCopy("target is unsafe")
        )
        XCTAssertEqual(
            controller.decide(start: nil, stop: unsafe, current: target),
            .skipCopy("target is unsafe")
        )
        XCTAssertEqual(
            controller.decide(start: nil, stop: nil, current: unsafe),
            .skipCopy("target is unsafe")
        )
        XCTAssertEqual(
            controller.decide(start: target, stop: target, current: target),
            .paste
        )
    }

    func testPasteEligibilityRejectsProtectedAndNonEditableTargets() {
        let controller = PasteController()

        XCTAssertTrue(controller.isEligible(pasteTarget()))
        XCTAssertFalse(controller.isEligible(pasteTarget(hasEditableValue: false)))
        XCTAssertFalse(controller.isEligible(pasteTarget(isProtectedContent: true)))
        XCTAssertFalse(controller.isEligible(pasteTarget(role: "AXSecureTextField")))
        XCTAssertFalse(controller.isEligible(pasteTarget(role: "AXWebArea")))
        XCTAssertFalse(controller.isUnsafeForClipboard(pasteTarget(searchableText: "pinboard")))
        XCTAssertTrue(controller.isUnsafeForClipboard(pasteTarget(searchableText: "pin")))
        XCTAssertFalse(controller.isEligible(pasteTarget(searchableText: "password")))
        XCTAssertFalse(controller.isEligible(pasteTarget(searchableText: "api key")))
        XCTAssertFalse(controller.isEligible(pasteTarget(searchableText: "認証コード")))
        XCTAssertTrue(controller.isEligible(pasteTarget(subrole: "AXSearchField")))
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
            isProtectedContent: isProtectedContent,
            searchableText: searchableText,
            discovery: discovery
        )
    }

    private func registryJSON(
        version: Int = 1,
        duplicateEngine: Bool = false,
        duplicateModel: Bool = false,
        unknownLanguageEngine: Bool = false
    ) throws -> URL {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("zen-whisper-registry-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let url = root.appendingPathComponent("model_registry.json")
        let secondEngine = duplicateEngine
            ? """
            ,{"id":"mlx-whisper","label":"Duplicate","default_model":"model-a","models":[{"id":"model-a","label":"A"}]}
            """
            : ""
        let secondModel = duplicateModel ? #",{"id":"model-a","label":"Duplicate A"}"# : ""
        let languageEngine = unknownLanguageEngine ? "missing" : "mlx-whisper"
        let json = """
        {
          "version": \(version),
          "default_engine": "mlx-whisper",
          "default_language": "ja",
          "languages": {
            "ja": {"label": "Japanese", "engines": {"\(languageEngine)": "ja"}}
          },
          "engines": [
            {
              "id": "mlx-whisper",
              "label": "MLX Whisper",
              "default_model": "model-a",
              "models": [
                {"id": "model-a", "label": "A"}\(secondModel)
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

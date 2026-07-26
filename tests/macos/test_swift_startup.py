from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"
APP_DELEGATE = SWIFT_SRC / "AppDelegate.swift"


def test_hotkey_registration_failure_is_not_overwritten_by_startup() -> None:
    text = APP_DELEGATE.read_text(encoding="utf-8")
    register_start = text.index("private func registerHotkey() throws")
    register_end = text.index("private func verifySignatureAndStartBackend()")
    register_body = text[register_start:register_end]

    assert "throw AppStartupError.hotkeyRegistration" in register_body
    assert "setState(.error" not in register_body
    assert "try registerHotkey()\n            verifySignatureAndStartBackend()" in text
    assert "case hotkeyRegistration(String, String)" in text


def test_hotkey_registration_failure_can_be_recovered_from_menu() -> None:
    app_delegate = APP_DELEGATE.read_text(encoding="utf-8")
    app_state = (SWIFT_SRC / "AppState.swift").read_text(encoding="utf-8")
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")

    assert "setState(.hotkeyError(" in app_delegate
    assert "if statusController == nil" in app_delegate
    assert "statusController = StatusController()" in app_delegate
    assert "statusController.updateSettings(registry: registry, settings: settings)" in app_delegate
    assert "private func recoverFromHotkeyErrorIfReady()" in app_delegate
    assert "guard case .hotkeyError = state else" in app_delegate
    assert "case hotkeyError(String)" in app_state
    assert "case .hotkeyError(let message):" in status_controller


def test_hotkey_handler_install_failure_is_checked() -> None:
    hotkey = (SWIFT_SRC / "HotkeyManager.swift").read_text(encoding="utf-8")

    assert "case handlerInstallFailed(OSStatus)" in hotkey
    assert "let handlerStatus = InstallEventHandler" in hotkey
    assert "if handlerStatus != noErr" in hotkey
    assert "try? hotkeyManager.register(choice: previous)" not in (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")


def test_hotkey_replacement_keeps_old_registration_until_new_succeeds() -> None:
    hotkey = (SWIFT_SRC / "HotkeyManager.swift").read_text(encoding="utf-8")
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    register_start = hotkey.index("func register(shortcut: HotkeyShortcut")
    register_end = hotkey.index("func suspend()")
    register_body = hotkey[register_start:register_end]

    assert "unregister()" not in register_body
    assert "var newHotKeyRef: EventHotKeyRef?" in register_body
    assert "let oldHotKeyRef = hotKeyRef" in register_body
    assert "hotKeyRef = newHotKeyRef" in register_body
    assert "UnregisterEventHotKey(oldHotKeyRef)" in register_body
    assert "var hasActiveRegistration: Bool" in hotkey
    assert "if !hotkeyManager.hasActiveRegistration" in app_delegate
    assert "static let submitSignature" in hotkey
    assert "private var registeredHotKeyID: EventHotKeyID?" in hotkey
    assert "GetEventParameter(" in hotkey
    assert "EventParamType(typeEventHotKeyID)" in hotkey
    assert "return OSStatus(eventNotHandledErr)" in hotkey
    assert "private let submitHotkeyManager = HotkeyManager(signature: HotkeyManager.submitSignature)" in app_delegate
    assert "try registerSubmitHotkeyIfNeeded()" in app_delegate
    assert "self?.toggleRecording(submitAfterPaste: true)" in app_delegate


def test_hotkey_error_recovery_verifies_both_hotkeys_before_backend_start() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")

    assert "private func recoverFromHotkeyErrorIfReady()" in app_delegate
    recover_start = app_delegate.index("private func recoverFromHotkeyErrorIfReady()")
    recover_end = app_delegate.index("private func verifySignatureAndStartBackend()")
    recover_body = app_delegate[recover_start:recover_end]

    assert "HotkeyPairRegistrationTransaction(" in recover_body
    assert "try transaction.reconcile(" in recover_body
    pair_registration = (
        SWIFT_SRC / "HotkeyPairRegistration.swift"
    ).read_text(encoding="utf-8")
    assert "primaryManager.activeShortcut != settings.hotkey" in pair_registration
    assert "submitManager.activeShortcut != settings.submitHotkey" in pair_registration
    assert "verifySignatureAndStartBackend()" in recover_body
    assert "setHotkeyError(" in recover_body


def test_backend_install_validation_runs_off_main_actor_with_probe_timeout() -> None:
    app_delegate = APP_DELEGATE.read_text(encoding="utf-8")
    validator = (SWIFT_SRC / "BackendInstallValidator.swift").read_text(encoding="utf-8")

    start_backend = app_delegate[
        app_delegate.index("private func startBackend()"):
        app_delegate.index("private func preloadSelectedModel()")
    ]

    assert 'setState(.preloading(message: "backend"))' in start_backend
    assert "DispatchQueue.global(qos: .userInitiated).async" in start_backend
    assert "switch validator.validateInstallMetadata()" in start_backend
    assert "backend install metadata invalid" in start_backend
    assert "let finished = DispatchSemaphore(value: 0)" in validator
    assert "process.terminationHandler" in validator
    assert "finished.wait(timeout: .now() + .seconds(8))" in validator
    assert "process.terminate()" in validator
    assert "case probeTimedOut" in validator
    assert "backend.stopDetailed()" in start_backend


def test_backend_client_tracks_process_before_auth_token_write() -> None:
    backend_client = (SWIFT_SRC / "BackendClient.swift").read_text(encoding="utf-8")
    start = backend_client[
        backend_client.index("func start() throws"):
        backend_client.index("@discardableResult")
    ]

    assert "try process.run()" in start
    assert "self.process = process" in start
    assert "let stopResult = stopDetailed()" in start
    assert start.index("self.process = process") < start.index("try authPipe.fileHandleForWriting.write")
    assert start.index("let stopResult = stopDetailed()") < start.index("throw BackendClientError.authTokenWriteFailed")


def test_backend_static_validation_runs_before_python_probe() -> None:
    validator = (SWIFT_SRC / "BackendInstallValidator.swift").read_text(encoding="utf-8")

    static_index = validator.index("try validateStaticBackendInstall(manifest: manifest, install: install)")
    probe_index = validator.index("let probe = try probeBackendPython()")
    assert static_index < probe_index
    assert "let venvManifestHash: String" in validator
    assert "guard install.venvManifestHash == manifest.venvManifestHash else" in validator
    assert "backend venv manifest is not sealed by signed app" in validator
    assert "let pythonSourceSha256: String" in validator
    assert "let pythonRuntimeManifestHash: String" in validator
    assert "let pythonRuntimePrefix: String" in validator
    assert 'private static let perUserManifestHash = "per-user"' in validator
    assert "manifest.venvManifestHash == Self.perUserManifestHash" in validator
    assert "let venvManifestData = try Data(contentsOf: venvManifestURL)" in validator
    assert "sha256Hex(data: venvManifestData)" in validator
    assert "backend venv manifest record mismatch" in validator
    assert "actualVenvManifestHash == install.venvManifestHash" in validator
    assert "manifest.pythonRuntimeManifestHash == Self.perUserManifestHash" in validator
    assert "let runtimeManifestData = try Data(contentsOf: runtimeManifestURL)" in validator
    assert "sha256Hex(data: runtimeManifestData)" in validator
    assert "Python runtime manifest record mismatch" in validator
    assert "actualRuntimeManifestHash == install.pythonRuntimeManifestHash" in validator
    assert "validatePythonRuntimeInstall" in validator
    assert "allowedSymlinkRoots: [runtimeRoot]" in validator
    assert "symlinkTargetIsInsideAllowedRoots" in validator
    assert "external backend venv symlink" in validator
    assert "external Python runtime symlink" in validator
    assert "private struct BackendVenvManifest" in validator
    assert "let rootPath: String?" in validator
    assert "let mode: String" in validator
    assert "allowedSymlinkRoots: [runtimeRoot]" in validator
    assert "rootHash(entries: venvManifest.files) == venvManifest.rootHash" in validator
    assert "rootHash(entries: Array(actualEntries.values)) == venvManifest.rootHash" in validator
    assert "func posixModeString" in validator
    assert "func canonicalJSONString" in validator
    assert "backend venv file set mismatch" in validator
    assert "backend venv root hash mismatch" in validator
    assert "backend Python executable hash mismatch" in validator
    assert "duplicate backend venv manifest path" in validator
    assert "unexpected Python bytecode in backend venv" in validator
    assert "unexpected Python startup hook" in validator
    assert "unexpected Python .pth startup hook" in validator
    assert '"distutils-precedence.pth"' in validator
    assert "static backend code hash mismatch" in validator
    assert "backend Python symlink target mismatch" in validator


def test_backend_process_output_is_redirected_to_private_startup_log() -> None:
    client = (SWIFT_SRC / "BackendClient.swift").read_text(encoding="utf-8")
    paths = (SWIFT_SRC / "AppPaths.swift").read_text(encoding="utf-8")

    assert "var backendStartupLog: URL" in paths
    assert "private func prepareStartupLog() throws -> URL" in client
    assert "try Data().write(to: logURL, options: .atomic)" in client
    assert "let startupLog = try FileHandle(forWritingTo: prepareStartupLog())" in client
    assert "process.standardOutput = startupLog" in client
    assert "process.standardError = startupLog" in client
    assert "private func startupLogTail" in client
    assert "case healthTimeout(String)" in client
    assert "healthFailureDetail(" in client
    assert "prefix: processExitSummary()" in client
    assert 'prefix: "backend health timed out"' in client
    assert 'parts.append("startup log: <empty>")' in client


def test_unix_socket_client_caps_response_size() -> None:
    socket_client = (SWIFT_SRC / "UnixSocketClient.swift").read_text(encoding="utf-8")

    assert "static let maxResponseBytes = 1_048_576" in socket_client
    assert "case responseTooLarge" in socket_client
    assert "case timeoutSetupFailed(Int32)" in socket_client
    assert "guard setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0 else" in socket_client
    assert "guard setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0 else" in socket_client
    assert socket_client.count("throw UnixSocketError.timeoutSetupFailed(errno)") == 2
    assert "response.count <= Self.maxResponseBytes" in socket_client

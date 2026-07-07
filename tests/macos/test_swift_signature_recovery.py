from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_signature_changed_state_has_explicit_menu_recovery() -> None:
    status_controller = (SWIFT_SRC / "StatusController.swift").read_text(encoding="utf-8")
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")

    assert "Accept Signature Change" in status_controller
    assert "Accept Signature Change (Daily app only)" in status_controller
    assert "onAcceptSignatureChange" in status_controller
    assert "case .appSignatureChanged" in status_controller
    assert "canAcceptCurrentSignatureChange()" in status_controller
    assert "onAcceptSignatureChange = { [weak self] in self?.acceptSignatureChange() }" in app_delegate


def test_signature_acceptance_requires_confirmation_and_daily_baseline_writer() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    baseline_store = (SWIFT_SRC / "SignatureBaselineStore.swift").read_text(encoding="utf-8")

    assert "Accept App Signature Change?" in app_delegate
    assert "Cannot Accept Signature Here" in app_delegate
    assert "Regrant macOS Permissions" in app_delegate
    assert "SignatureBaselineStore.acceptCurrentDailyApp" in app_delegate
    assert "AppRuntimeIdentity.currentBundlePath == AppRuntimeIdentity.dailyAppPath" in app_delegate
    assert "logInfo(\"signature acceptance failed: \\(error)\")" in app_delegate
    assert 'alert.informativeText = "See Open Logs for details."' in app_delegate
    assert "AppRuntimeIdentity.dailyAppPath" in baseline_store
    assert "SignatureValidator.executableHash" in baseline_store
    assert "SignatureValidator.verifyBundleIntegrity" in baseline_store
    assert '"designated_requirement"' in baseline_store
    assert '"executable_sha256"' in baseline_store
    assert '"accepted_in_app"' in baseline_store


def test_signature_validation_checks_executable_hash_baseline() -> None:
    validator = (SWIFT_SRC / "SignatureValidator.swift").read_text(encoding="utf-8")
    install_app = (REPO_ROOT / "macos/scripts/install_app.sh").read_text(encoding="utf-8")

    assert 'let expectedExecutableHash = object["executable_sha256"] as? String' in validator
    assert "Self.executableHash(for: Bundle.main.bundleURL) == expectedExecutableHash" in validator
    assert "static func executableHash(for appURL: URL) -> String?" in validator
    assert "SHA256.hash(data: data)" in validator
    assert 'APP_EXECUTABLE_HASH="$(/usr/bin/shasum -a 256 "$APP_TEMP/Contents/MacOS/zen-whisper"' in install_app
    assert '--string executable_sha256 "$APP_EXECUTABLE_HASH"' in install_app


def test_backend_repair_runs_signed_bundled_installer_explicitly() -> None:
    app_delegate = (SWIFT_SRC / "AppDelegate.swift").read_text(encoding="utf-8")
    repair_runner = (SWIFT_SRC / "BackendRepairRunner.swift").read_text(encoding="utf-8")
    process_environment = (SWIFT_SRC / "ProcessEnvironment.swift").read_text(encoding="utf-8")

    repair_start = app_delegate.index("private func repairBackend()")
    repair_end = app_delegate.index("private func acceptSignatureChange()")
    repair_body = app_delegate[repair_start:repair_end]

    assert "setState(.repairingBackend)" in repair_body
    assert "BackendRepairRunner(paths: paths, bundleURL: Bundle.main.bundleURL)" in repair_body
    assert "try runner.repair()" in repair_body
    assert "self.startBackend()" in repair_body
    assert "Repair From Terminal" not in repair_body
    assert "install_backend_from_app.sh" in repair_runner
    assert "process.executableURL = installer" in repair_runner
    assert "ProcessEnvironment.backendRepair(appSupport: paths.appSupport)" in repair_runner
    assert '"ZEN_WHISPER_ALLOW_BUNDLED_TOOLCHAIN"] = "1"' in process_environment
    assert '"ZEN_WHISPER_ALLOW_RECORDED_TOOLCHAIN"] = "1"' in process_environment

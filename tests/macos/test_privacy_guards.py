from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_root_transcriber_does_not_log_transcript_text() -> None:
    transcriber = (REPO_ROOT / "src/transcriber.py").read_text(encoding="utf-8")

    assert 'logger.debug("認識結果: %s", text)' not in transcriber
    assert "認識結果メタデータ" in transcriber


def test_backend_errors_do_not_embed_recording_paths() -> None:
    adapters = (
        REPO_ROOT / "macos/backend/src/zen_whisper_mac_backend/adapters.py"
    ).read_text(encoding="utf-8")
    service = (
        REPO_ROOT / "macos/backend/src/zen_whisper_mac_backend/service.py"
    ).read_text(encoding="utf-8")

    assert "{audio_path}" not in adapters
    assert "_public_error" not in adapters
    assert "Audio file not found" in service
    assert "_error_message_for(exc)" in service


def test_phase0_gate_does_not_print_transcript_snippets() -> None:
    gate = (REPO_ROOT / "macos/scripts/phase0_asr_gate.sh").read_text(encoding="utf-8")

    assert "whisper_text[:80]" not in gate
    assert "qwen_text[:80]" not in gate
    assert "chars=" in gate


def test_native_logs_enforce_private_permissions() -> None:
    app_paths = (
        REPO_ROOT / "macos/ZenWhisper/ZenWhisper/AppPaths.swift"
    ).read_text(encoding="utf-8")
    app_logger = (
        REPO_ROOT / "macos/ZenWhisper/ZenWhisper/AppLogger.swift"
    ).read_text(encoding="utf-8")
    backend_logging = (
        REPO_ROOT / "macos/backend/src/zen_whisper_mac_backend/logging_config.py"
    ).read_text(encoding="utf-8")

    assert ".posixPermissions: 0o700" in app_paths
    assert "ofItemAtPath: url.path" in app_paths
    assert ".posixPermissions: 0o600" in app_logger
    assert "rotateIfNeeded(logURL: logURL)" in app_logger
    assert "os.chmod(log_dir, 0o700)" in backend_logging
    assert "os.chmod(log_file, 0o600)" in backend_logging
    assert "class PrivateRotatingFileHandler" in backend_logging


def test_native_menu_error_text_uses_sanitized_summaries_and_logs_details() -> None:
    status_controller = (
        REPO_ROOT / "macos/ZenWhisper/ZenWhisper/StatusController.swift"
    ).read_text(encoding="utf-8")
    app_state = (
        REPO_ROOT / "macos/ZenWhisper/ZenWhisper/AppState.swift"
    ).read_text(encoding="utf-8")
    app_delegate = (
        REPO_ROOT / "macos/ZenWhisper/ZenWhisper/AppDelegate.swift"
    ).read_text(encoding="utf-8")

    assert "static func visibleErrorSummary(_ message: String)" in app_state
    assert 'return "See logs"' in app_state
    assert "Model Not Available: \\(StatusText.visibleErrorSummary(message))" in status_controller
    assert "Backend Repair Required: \\(StatusText.visibleErrorSummary(message))" in status_controller
    assert "Microphone Error: \\(StatusText.visibleErrorSummary(message))" in status_controller
    assert "logInfo(\"backend operation error code=\\(code) recoverable=\\(recoverable): \\(message)\")" in app_delegate
    assert 'let repairLog = paths.logs.appendingPathComponent("backend-repair.log")' in app_delegate
    assert '"backend-repair.log:"' in app_delegate
    assert 'logInfo("backend repair requested")' in app_delegate
    assert 'logInfo("backend repair aborted because the existing backend did not stop")' in app_delegate
    assert 'logInfo("backend repair failed: \\(error)")' in app_delegate
    assert "logInfo(\"signature acceptance failed: \\(error)\")" in app_delegate
    assert "Self.visibleRepairFailureMessage(error)" in app_delegate
    assert 'return "[unavailable: \\(url.path): \\(error.localizedDescription)]"' in app_delegate
    assert 'return "[unreadable UTF-8: \\(url.path)]"' in app_delegate
    assert 'return "[empty]"' in app_delegate
    assert "alert.informativeText = String(describing: error)" not in app_delegate


def test_root_logger_uses_private_rotating_file_handler() -> None:
    main = (REPO_ROOT / "src/main.py").read_text(encoding="utf-8")

    assert "RotatingFileHandler" in main
    assert "maxBytes=5 * 1024 * 1024" in main
    assert "backupCount=5" in main
    assert "os.chmod(path, 0o600)" in main


def test_repo_source_tree_has_no_python_bytecode_artifacts() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    offenders = [
        path for path in tracked
        if "__pycache__" in path.split("/") or Path(path).suffix in {".pyc", ".pyo"}
    ]

    assert offenders == []


def test_legacy_macos_helpers_use_absolute_system_tools() -> None:
    darwin = (REPO_ROOT / "src/platform/darwin.py").read_text(encoding="utf-8")

    assert '["/bin/launchctl", "load", str(_PLIST_PATH)]' in darwin
    assert '["/bin/launchctl", "unload", str(_PLIST_PATH)]' in darwin
    assert '"/usr/bin/osascript"' in darwin
    assert '"osascript"' not in darwin.replace('"/usr/bin/osascript"', "")

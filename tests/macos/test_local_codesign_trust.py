from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_runtime_accepts_only_local_self_signed_trust_failure() -> None:
    validator = (SWIFT_SRC / "SignatureValidator.swift").read_text(encoding="utf-8")
    assert "CSSMERR_TP_NOT_TRUSTED" in validator
    assert "static func isLocalTrustOnlyFailure" in validator
    assert "integrityFailureTerms" in validator
    assert "resource envelope" in validator
    assert "let outputPipe = Pipe()" in validator
    assert "let errorPipe = Pipe()" in validator
    assert "process.standardOutput = outputPipe" in validator
    assert "process.standardError = errorPipe" in validator


def test_installers_accept_local_self_signed_trust_failure() -> None:
    install_app = (REPO_ROOT / "macos/scripts/install_app.sh").read_text(encoding="utf-8")
    install_backend = (REPO_ROOT / "macos/scripts/install_backend_from_app.sh").read_text(encoding="utf-8")
    for text in (install_app, install_backend):
        assert "verify_app_signature()" in text
        assert "is_local_trust_only_codesign_failure()" in text
        assert "CSSMERR_TP_NOT_TRUSTED" in text
        assert "accepting local self-signed code signature" in text
        assert "resource envelope" in text

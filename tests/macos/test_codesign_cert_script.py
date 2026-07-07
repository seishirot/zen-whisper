from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "macos/scripts/create_local_codesign_cert.sh"


def test_local_codesign_cert_uses_non_empty_pkcs12_password() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "P12_PASSWORD=" in text
    assert "valid_identity_exists()" in text
    assert "explain_codesign_prompt_policy()" in text
    assert "Passwordless codesign key authorization is intentionally unsupported" in text
    assert "ZEN_WHISPER_TRUST_LOCAL_CERT" in text
    assert "trust_cert()" in text
    assert "security find-certificate" in text
    assert "security add-trusted-cert" in text
    assert "trustAsRoot" in text
    assert "trustRoot" not in text
    assert "basicConstraints=critical,CA:FALSE" in text
    assert "basicConstraints=critical,CA:TRUE" not in text
    assert "keyCertSign" not in text
    assert '-passout "pass:$P12_PASSWORD"' in text
    assert '-P "$P12_PASSWORD"' in text
    assert "security set-key-partition-list" not in text
    assert "ZEN_WHISPER_AUTHORIZE_CODESIGN_KEY" not in text
    assert "ZEN_WHISPER_KEYCHAIN_PASSWORD" not in text
    assert "Authorized codesign access for identity" not in text
    assert "-T /usr/bin/codesign" not in text
    assert "-passout pass:" not in text
    assert '-P ""' not in text
    assert '-k ""' not in text

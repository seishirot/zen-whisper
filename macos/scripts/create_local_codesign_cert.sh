#!/usr/bin/env bash
set -euo pipefail

IDENTITY="${ZEN_WHISPER_LOCAL_CERT_NAME:-zen-whisper Local Code Signing}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
TMP_DIR="$(/usr/bin/mktemp -d)"
trap '/bin/rm -rf "$TMP_DIR"' EXIT
P12_PASSWORD="$(/usr/bin/openssl rand -hex 24)"

valid_identity_exists() {
  /usr/bin/security find-identity -p codesigning -v | /usr/bin/grep -F "\"$IDENTITY\"" >/dev/null
}

explain_codesign_prompt_policy() {
  echo "Passwordless codesign key authorization is intentionally unsupported for this TCC-bearing app." >&2
  echo "Use the macOS keychain prompt or a dedicated Developer ID workflow instead." >&2
}

trust_cert() {
  if [[ "${ZEN_WHISPER_TRUST_LOCAL_CERT:-}" != "1" ]]; then
    echo "Local certificate trust was not changed."
    echo "If macOS does not list the identity as valid, rerun this command with ZEN_WHISPER_TRUST_LOCAL_CERT=1."
    return 0
  fi
  /usr/bin/security add-trusted-cert \
    -r trustAsRoot \
    -p codeSign \
    -k "$KEYCHAIN" \
    "$1" >/dev/null
}

if valid_identity_exists; then
  explain_codesign_prompt_policy
  echo "Code signing identity already exists: $IDENTITY"
  exit 0
fi

if /usr/bin/security find-certificate -c "$IDENTITY" -p "$KEYCHAIN" > "$TMP_DIR/existing.crt" 2>/dev/null \
  && [[ -s "$TMP_DIR/existing.crt" ]]; then
  trust_cert "$TMP_DIR/existing.crt"
  if valid_identity_exists; then
    explain_codesign_prompt_policy
    echo "Trusted existing code signing identity: $IDENTITY"
    exit 0
  fi
fi

/usr/bin/openssl req \
  -newkey rsa:2048 \
  -nodes \
  -keyout "$TMP_DIR/codesign.key" \
  -x509 \
  -days 3650 \
  -out "$TMP_DIR/codesign.crt" \
  -subj "/CN=$IDENTITY" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "extendedKeyUsage=codeSigning" \
  -addext "keyUsage=critical,digitalSignature"

/usr/bin/openssl pkcs12 \
  -export \
  -out "$TMP_DIR/codesign.p12" \
  -inkey "$TMP_DIR/codesign.key" \
  -in "$TMP_DIR/codesign.crt" \
  -passout "pass:$P12_PASSWORD"

/usr/bin/security import "$TMP_DIR/codesign.p12" \
  -k "$KEYCHAIN" \
  -P "$P12_PASSWORD"

trust_cert "$TMP_DIR/codesign.crt"

explain_codesign_prompt_policy

if ! valid_identity_exists; then
  echo "Created certificate, but macOS still does not report it as a valid code signing identity." >&2
  echo "Run with ZEN_WHISPER_TRUST_LOCAL_CERT=1 or trust '$IDENTITY' for Code Signing in Keychain Access, then rerun install_app.sh." >&2
  exit 1
fi

echo "Created local code signing identity: $IDENTITY"

#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"
SWIFT_DIR="$REPO_ROOT/macos/ZenWhisper"
BACKEND_DIR="$REPO_ROOT/macos/backend"
BUILD_DIR="$REPO_ROOT/macos/build"
APP_STAGING="$BUILD_DIR/zen-whisper.app"
APP_DEST="${ZEN_WHISPER_APP_DEST:-/Applications/zen-whisper.app}"
APP_SUPPORT="${ZEN_WHISPER_APP_SUPPORT:-"$HOME/Library/Application Support/zen-whisper"}"
DEFAULT_APP_SUPPORT="$HOME/Library/Application Support/zen-whisper"
IDENTITY="${ZEN_WHISPER_CODE_SIGN_IDENTITY:-zen-whisper Local Code Signing}"

fail() {
  echo "install_app.sh: $*" >&2
  exit 1
}

handle_signal() {
  local signal="${1:-TERM}"
  trap - INT TERM
  if [[ "$signal" == "INT" ]]; then
    exit 130
  fi
  exit 143
}

path_exists() {
  local path="$1"
  [[ -e "$path" || -L "$path" ]]
}

ensure_absent() {
  local path="$1"
  local label="$2"
  if path_exists "$path"; then
    fail "$label path already exists: $path"
  fi
}

DEFERRED_SIGNAL=""
defer_signal() {
  local signal="$1"
  DEFERRED_SIGNAL="$signal"
}

begin_deferred_signal() {
  DEFERRED_SIGNAL=""
  trap 'defer_signal INT' INT
  trap 'defer_signal TERM' TERM
}

end_deferred_signal() {
  trap 'handle_signal INT' INT
  trap 'handle_signal TERM' TERM
  local signal="$DEFERRED_SIGNAL"
  DEFERRED_SIGNAL=""
  if [[ -n "$signal" ]]; then
    handle_signal "$signal"
  fi
}

acquire_dir_lock() {
  local lock_path="$1"
  local flag_name="$2"
  local error_message="$3"
  begin_deferred_signal
  if ! /bin/mkdir "$lock_path" 2>/dev/null; then
    end_deferred_signal
    fail "$error_message: $lock_path"
  fi
  printf -v "$flag_name" '1'
  end_deferred_signal
}

write_json_object() {
  PYTHONSAFEPATH=1 "$PYTHON_PATH" -P "$REPO_ROOT/macos/scripts/write_json_object.py" "$@"
}

verify_app_signature() {
  local app_path="$1"
  local output
  if output="$(/usr/bin/codesign --verify --strict "$app_path" 2>&1)"; then
    return 0
  fi
  if is_local_trust_only_codesign_failure "$output"; then
    echo "install_app.sh: accepting local self-signed code signature for $app_path: CSSMERR_TP_NOT_TRUSTED" >&2
    return 0
  fi
  echo "$output" >&2
  return 1
}

is_local_trust_only_codesign_failure() {
  local output="$1"
  /usr/bin/grep -F "CSSMERR_TP_NOT_TRUSTED" <<<"$output" >/dev/null || return 1
  if /usr/bin/grep -Eiq "bundle format|code object is not signed|invalid|main executable failed|modified|not signed|rejected|resource envelope|sealed resource|unsealed" <<<"$output"; then
    return 1
  fi
  return 0
}

validate_app_destination() {
  local dest="$1"
  [[ "$dest" = /* ]] || fail "ZEN_WHISPER_APP_DEST must be an absolute path: $dest"
  [[ "$(/usr/bin/basename "$dest")" == "zen-whisper.app" ]] || fail "ZEN_WHISPER_APP_DEST must end with zen-whisper.app: $dest"
  [[ "$dest" != "/" ]] || fail "refusing to install to root"
  [[ "$dest" == "/Applications/zen-whisper.app" ]] || fail "daily app path must be /Applications/zen-whisper.app: $dest"
  if path_exists "$dest"; then
    local plist="$dest/Contents/Info.plist"
    [[ -f "$plist" ]] || fail "refusing to replace non-app or malformed destination: $dest"
    local bundle_id
    bundle_id="$(/usr/libexec/PlistBuddy -c 'Print:CFBundleIdentifier' "$plist" 2>/dev/null || true)"
    [[ "$bundle_id" == "com.seishirot.zenwhisper" ]] || fail "refusing to replace app with unexpected bundle id: ${bundle_id:-<missing>}"
  fi
}

validate_app_support() {
  local path="$1"
  [[ "$path" = /* ]] || fail "ZEN_WHISPER_APP_SUPPORT must be an absolute path: $path"
  case "$path" in
    "/"|"$HOME"|"$HOME/Library"|"$HOME/Library/Application Support"|"/Applications")
      fail "refusing unsafe App Support path: $path"
      ;;
  esac
  if [[ "$path" != "$DEFAULT_APP_SUPPORT" && "${ZEN_WHISPER_ALLOW_CUSTOM_APP_SUPPORT:-}" != "1" ]]; then
    fail "custom ZEN_WHISPER_APP_SUPPORT requires ZEN_WHISPER_ALLOW_CUSTOM_APP_SUPPORT=1"
  fi
  if [[ "$path" != "$DEFAULT_APP_SUPPORT" && "$(/usr/bin/basename "$path")" != *"zen-whisper"* ]]; then
    fail "custom ZEN_WHISPER_APP_SUPPORT basename must contain zen-whisper: $path"
  fi
}

command -v mise >/dev/null 2>&1 || fail "mise is required"
MISE_PATH="$(command -v mise)"
validate_app_destination "$APP_DEST"
validate_app_support "$APP_SUPPORT"

/bin/mkdir -p "$APP_SUPPORT/install"
/bin/chmod 700 "$APP_SUPPORT" "$APP_SUPPORT/install"
APP_INSTALL_LOCK="$APP_SUPPORT/install/app.lock"
APP_TEMP=""
APP_SUPPORT_STAGE=""
APP_LOCK_HELD=0
BACKEND_LOCK_HELD=0
APP_WAS_RUNNING=0
APP_REOPENED=0
reopen_app_if_needed() {
  if [[ "${APP_WAS_RUNNING:-0}" != "1" || "${APP_REOPENED:-0}" == "1" || "${ZEN_WHISPER_REOPEN_AFTER_INSTALL:-1}" == "0" ]]; then
    return 0
  fi
  if [[ ! -d "$APP_DEST" ]]; then
    return 0
  fi
  /usr/bin/open "$APP_DEST" >/dev/null 2>&1 || true
  APP_REOPENED=1
  echo "Reopened app: $APP_DEST"
}
cleanup_app_lock() {
  local status=$?
  trap '' INT TERM
  set +e
  if [[ "${INSTALL_COMMITTED:-0}" != "1" ]]; then
    if [[ -n "${APP_TEMP:-}" ]]; then
      /bin/rm -rf "$APP_TEMP"
    fi
    if [[ -n "${APP_SUPPORT_STAGE:-}" ]]; then
      /bin/rm -rf "$APP_SUPPORT_STAGE"
    fi
  fi
  if [[ "${BACKEND_LOCK_HELD:-0}" == "1" ]]; then
    /bin/rmdir "$BACKEND_INSTALL_LOCK" 2>/dev/null || true
  fi
  if [[ "${APP_LOCK_HELD:-0}" == "1" ]]; then
    /bin/rmdir "$APP_INSTALL_LOCK" 2>/dev/null || true
  fi
  if [[ "${INSTALL_COMMITTED:-0}" != "1" ]]; then
    reopen_app_if_needed
  fi
  set -e
  return "$status"
}
trap cleanup_app_lock EXIT
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM
acquire_dir_lock "$APP_INSTALL_LOCK" APP_LOCK_HELD "app install is already running"
BACKEND_INSTALL_LOCK="$APP_SUPPORT/install/backend.lock"
acquire_dir_lock "$BACKEND_INSTALL_LOCK" BACKEND_LOCK_HELD "backend install or repair is already running"

if "$MISE_PATH" trust --show "$REPO_ROOT/.mise.toml" | /usr/bin/grep -q "untrusted"; then
  fail "mise config is not trusted. Run: mise trust $REPO_ROOT/.mise.toml"
fi

UV_PATH="$("$MISE_PATH" exec -- which uv)"
PYTHON_PATH="$("$MISE_PATH" exec -- python -P -c 'import sys; print(sys.executable)')"
[[ -x "$UV_PATH" ]] || fail "uv is not executable: $UV_PATH"
[[ -x "$PYTHON_PATH" ]] || fail "Python is not executable: $PYTHON_PATH"

if ! /usr/bin/security find-identity -p codesigning -v | /usr/bin/grep -F "\"$IDENTITY\"" >/dev/null; then
  fail "code signing identity not found: $IDENTITY. Run macos/scripts/create_local_codesign_cert.sh or set ZEN_WHISPER_CODE_SIGN_IDENTITY."
fi

/bin/rm -rf "$BUILD_DIR"
/bin/mkdir -p "$BUILD_DIR/backend-dist" "$APP_STAGING/Contents/MacOS" "$APP_STAGING/Contents/Resources/backend"

"$MISE_PATH" exec -- uv build --project "$BACKEND_DIR" --wheel --out-dir "$BUILD_DIR/backend-dist"
"$MISE_PATH" exec -- uv export --project "$BACKEND_DIR" --locked --extra mlx --no-dev --no-emit-project --format requirements.txt --output-file "$BUILD_DIR/requirements-mlx.txt"
read_backend_constant() {
  local name="$1"
  PYTHONSAFEPATH=1 "$PYTHON_PATH" -P - "$BACKEND_DIR/src/zen_whisper_mac_backend/__init__.py" "$name" <<'PY'
import ast
import sys
from pathlib import Path

module = ast.parse(Path(sys.argv[1]).read_text())
name = sys.argv[2]
for node in module.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                print(ast.literal_eval(node.value))
                raise SystemExit(0)
raise SystemExit(f"constant not found: {name}")
PY
}
BACKEND_VERSION="$(read_backend_constant BACKEND_VERSION)"
PROTOCOL_VERSION="$(read_backend_constant PROTOCOL_VERSION)"
BACKEND_CODE_HASH="$(PYTHONSAFEPATH=1 "$PYTHON_PATH" -P "$BACKEND_DIR/src/zen_whisper_mac_backend/probe.py" wheel-hash "$BUILD_DIR"/backend-dist/zen_whisper_mac_backend-*.whl)"

SWIFT_PATH="$(/usr/bin/xcrun --find swift 2>/dev/null || true)"
[[ -x "$SWIFT_PATH" ]] || fail "Swift toolchain is required. Check xcode-select -p."
"$SWIFT_PATH" build -c release --package-path "$SWIFT_DIR" --arch arm64
EXECUTABLE="$(/usr/bin/find "$SWIFT_DIR/.build" -path "*/release/zen-whisper" -type f -perm -111 | /usr/bin/head -n 1)"
[[ -x "$EXECUTABLE" ]] || fail "Swift executable not found"
/bin/cp "$EXECUTABLE" "$APP_STAGING/Contents/MacOS/zen-whisper"

/usr/bin/sed \
  -e "s/\$(PRODUCT_BUNDLE_IDENTIFIER)/com.seishirot.zenwhisper/g" \
  -e "s/\$(DEVELOPMENT_LANGUAGE)/en/g" \
  -e "s/\$(ZEN_WHISPER_BUILD_CHANNEL)/daily/g" \
  "$SWIFT_DIR/ZenWhisper/Info.plist" > "$APP_STAGING/Contents/Info.plist"

/bin/cp "$BACKEND_DIR/src/zen_whisper_mac_backend/resources/model_registry.json" "$APP_STAGING/Contents/Resources/backend/model_registry.json"
/bin/cp "$BACKEND_DIR/src/zen_whisper_mac_backend/resources/model_registry.json" "$APP_STAGING/Contents/Resources/model_registry.json"
/bin/cp "$BUILD_DIR"/backend-dist/zen_whisper_mac_backend-*.whl "$APP_STAGING/Contents/Resources/backend/"
/bin/cp "$BUILD_DIR/requirements-mlx.txt" "$APP_STAGING/Contents/Resources/backend/requirements-mlx.txt"
/bin/cp "$REPO_ROOT/.mise.toml" "$APP_STAGING/Contents/Resources/backend/.mise.toml"
/bin/cp "$REPO_ROOT/macos/scripts/install_backend_from_app.sh" "$APP_STAGING/Contents/Resources/backend/install_backend_from_app.sh"
/bin/chmod 755 "$APP_STAGING/Contents/Resources/backend/install_backend_from_app.sh"

REGISTRY_HASH="$(/usr/bin/shasum -a 256 "$APP_STAGING/Contents/Resources/backend/model_registry.json" | /usr/bin/awk '{print $1}')"
REQ_HASH="$(/usr/bin/shasum -a 256 "$APP_STAGING/Contents/Resources/backend/requirements-mlx.txt" | /usr/bin/awk '{print $1}')"
WHEEL="$(/bin/ls "$APP_STAGING"/Contents/Resources/backend/zen_whisper_mac_backend-*.whl | /usr/bin/head -n 1)"
WHEEL_HASH="$(/usr/bin/shasum -a 256 "$WHEEL" | /usr/bin/awk '{print $1}')"
write_json_object "$APP_STAGING/Contents/Resources/backend/BackendBundleManifest.json" \
  --int protocol_version "$PROTOCOL_VERSION" \
  --string backend_version "$BACKEND_VERSION" \
  --string wheel_hash "$WHEEL_HASH" \
  --string backend_code_hash "$BACKEND_CODE_HASH" \
  --string registry_hash "$REGISTRY_HASH" \
  --string requirements_hash "$REQ_HASH" \
  --string venv_manifest_hash "pending-install" \
  --string python_runtime_manifest_hash "pending-install" \
  --string supported_python ">=3.12,<3.13" \
  --string bundle_id "com.seishirot.zenwhisper"

/usr/bin/codesign --force --deep --sign "$IDENTITY" "$APP_STAGING"
verify_app_signature "$APP_STAGING"
/usr/bin/file "$APP_STAGING/Contents/MacOS/zen-whisper" | /usr/bin/grep -F "arm64" >/dev/null || fail "app executable is not arm64"

APP_PARENT="$(/usr/bin/dirname "$APP_DEST")"
APP_TEMP="$APP_PARENT/.zen-whisper.app.installing.$$"
APP_BACKUP="$APP_PARENT/.zen-whisper.app.previous.$$"
TOOLCHAIN_JSON="$APP_SUPPORT/install/toolchain.json"
SIGNING_JSON="$APP_SUPPORT/install/signing.json"
APP_SUPPORT_STAGE="$APP_SUPPORT/.zen-whisper-installing.$$"
TOOLCHAIN_TEMP="$APP_SUPPORT_STAGE/install/toolchain.json"
SIGNING_TEMP="$APP_SUPPORT_STAGE/install/signing.json"
TOOLCHAIN_BACKUP="$APP_SUPPORT/install/toolchain.json.previous.$$"
SIGNING_BACKUP="$APP_SUPPORT/install/signing.json.previous.$$"
BACKEND_FINAL="$APP_SUPPORT/backend"
BACKEND_STAGE="$APP_SUPPORT_STAGE/backend"
BACKEND_BACKUP="$APP_SUPPORT/backend.previous.$$"
/bin/rm -rf "$APP_TEMP" "$APP_SUPPORT_STAGE"
ensure_absent "$APP_TEMP" "app staging"
ensure_absent "$APP_BACKUP" "app backup"
ensure_absent "$APP_SUPPORT_STAGE" "App Support staging"
ensure_absent "$BACKEND_BACKUP" "backend backup"
ensure_absent "$TOOLCHAIN_BACKUP" "toolchain metadata backup"
ensure_absent "$SIGNING_BACKUP" "signing metadata backup"
/bin/cp -R "$APP_STAGING" "$APP_TEMP"
verify_app_signature "$APP_TEMP"

quit_running_app() {
  if [[ "${ZEN_WHISPER_SKIP_APP_QUIT:-}" == "1" ]]; then
    return 0
  fi
  if ! /usr/bin/pgrep -x zen-whisper >/dev/null 2>&1; then
    return 0
  fi

  APP_WAS_RUNNING=1
  echo "install_app.sh: quitting running zen-whisper app"
  /usr/bin/osascript -e 'tell application id "com.seishirot.zenwhisper" to quit' >/dev/null 2>&1 || true
  for _ in {1..50}; do
    if ! /usr/bin/pgrep -x zen-whisper >/dev/null 2>&1; then
      return 0
    fi
    /bin/sleep 0.1
  done
  fail "zen-whisper is still running. Quit it and rerun install_app.sh, or set ZEN_WHISPER_SKIP_APP_QUIT=1."
}

quit_running_app

remove_legacy_python_launch_agent() {
  local label="com.zen-whisper"
  local plist="$HOME/Library/LaunchAgents/${label}.plist"
  if [[ ! -f "$plist" ]]; then
    return 0
  fi
  if ! launch_agent_is_legacy_python "$plist"; then
    echo "Preserved existing LaunchAgent: $plist"
    return 0
  fi
  /bin/launchctl bootout "gui/$(/usr/bin/id -u)" "$plist" >/dev/null 2>&1 || true
  /bin/launchctl remove "$label" >/dev/null 2>&1 || true
  /bin/rm -f "$plist"
  echo "Removed legacy Python LaunchAgent: $plist"
}

launch_agent_is_legacy_python() {
  local plist="$1"
  local first_arg
  local second_arg
  first_arg="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$plist" 2>/dev/null || true)"
  second_arg="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' "$plist" 2>/dev/null || true)"
  if [[ "$first_arg" == "/usr/bin/open" && "$second_arg" == "/Applications/zen-whisper.app" ]]; then
    return 1
  fi
  if launch_agent_is_legacy_uv "$plist"; then
    return 0
  fi

  local args
  args="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments' "$plist" 2>/dev/null || true)"
  if /usr/bin/grep -Eiq 'python([0-9.]+|w)?$|python([0-9.]+|w)?[[:space:]]|pythonw|src/main.py|zen_whisper|zen-whisper\.py' <<<"$args"; then
    return 0
  fi
  return 1
}

launch_agent_is_legacy_uv() {
  local plist="$1"
  local first_arg
  first_arg="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$plist" 2>/dev/null || true)"
  [[ "$(/usr/bin/basename "$first_arg")" == "uv" ]] || return 1

  local saw_run=0
  local saw_zen_whisper=0
  local index
  local arg
  for index in {1..20}; do
    arg="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:${index}" "$plist" 2>/dev/null || true)"
    [[ -n "$arg" ]] || continue
    if [[ "$arg" == "run" ]]; then
      saw_run=1
    elif [[ "$arg" == "zen-whisper" ]]; then
      saw_zen_whisper=1
    fi
  done
  [[ "$saw_run" == "1" && "$saw_zen_whisper" == "1" ]]
}

/bin/mkdir -p "$APP_SUPPORT_STAGE/install"
/bin/chmod 700 "$APP_SUPPORT" "$APP_SUPPORT/install" "$APP_SUPPORT_STAGE" "$APP_SUPPORT_STAGE/install"
REPO_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
CREATED_AT="$(/bin/date -u +"%Y-%m-%dT%H:%M:%SZ")"
write_json_object "$TOOLCHAIN_TEMP" \
  --string mise_path "$MISE_PATH" \
  --string uv_path "$UV_PATH" \
  --string python_path "$PYTHON_PATH" \
  --string repo_root "$REPO_ROOT" \
  --string repo_commit "$REPO_COMMIT" \
  --string app_path "$APP_DEST" \
  --string backend_registry_hash "$REGISTRY_HASH" \
  --string backend_requirements_hash "$REQ_HASH" \
  --string created_at "$CREATED_AT"
/bin/chmod 600 "$TOOLCHAIN_TEMP"

ZEN_WHISPER_MISE_PATH="$MISE_PATH" \
ZEN_WHISPER_UV_PATH="$UV_PATH" \
ZEN_WHISPER_PYTHON_PATH="$PYTHON_PATH" \
ZEN_WHISPER_APP_SUPPORT="$APP_SUPPORT_STAGE" \
ZEN_WHISPER_ALLOW_CUSTOM_APP_SUPPORT=1 \
ZEN_WHISPER_BOOTSTRAP_BACKEND_INSTALL=1 \
ZEN_WHISPER_FINAL_BACKEND_PYTHON="$APP_SUPPORT/backend/.venv/bin/python" \
ZEN_WHISPER_FINAL_APP_PATH="$APP_DEST" \
  "$APP_TEMP/Contents/Resources/backend/install_backend_from_app.sh" "$APP_TEMP"

VENV_MANIFEST_HASH="$(PYTHONSAFEPATH=1 "$PYTHON_PATH" -P - "$BACKEND_STAGE/venv_manifest.json" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
PYTHON_RUNTIME_MANIFEST_HASH="$(PYTHONSAFEPATH=1 "$PYTHON_PATH" -P - "$BACKEND_STAGE/python_runtime_manifest.json" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
write_json_object "$APP_TEMP/Contents/Resources/backend/BackendBundleManifest.json" \
  --int protocol_version "$PROTOCOL_VERSION" \
  --string backend_version "$BACKEND_VERSION" \
  --string wheel_hash "$WHEEL_HASH" \
  --string backend_code_hash "$BACKEND_CODE_HASH" \
  --string registry_hash "$REGISTRY_HASH" \
  --string requirements_hash "$REQ_HASH" \
  --string venv_manifest_hash "per-user" \
  --string python_runtime_manifest_hash "per-user" \
  --string supported_python ">=3.12,<3.13" \
  --string bundle_id "com.seishirot.zenwhisper"
/usr/bin/codesign --force --deep --sign "$IDENTITY" "$APP_TEMP"
verify_app_signature "$APP_TEMP"
DESIGNATED_REQUIREMENT="$(/usr/bin/codesign -dr - "$APP_TEMP" 2>&1 | /usr/bin/sed -n 's/^.*designated => //p')"
APP_EXECUTABLE_HASH="$(/usr/bin/shasum -a 256 "$APP_TEMP/Contents/MacOS/zen-whisper" | /usr/bin/awk '{print $1}')"
if [[ -f "$APP_SUPPORT/install/signing.json" ]]; then
  PREVIOUS_REQUIREMENT="$(PYTHONSAFEPATH=1 "$PYTHON_PATH" -P - "$APP_SUPPORT/install/signing.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    print(json.loads(Path(sys.argv[1]).read_text()).get("designated_requirement", ""))
except Exception:
    print("")
PY
)"
  if [[ -z "$PREVIOUS_REQUIREMENT" && "${ZEN_WHISPER_ACCEPT_SIGNATURE_CHANGE:-}" != "1" ]]; then
    fail "existing signing baseline is unreadable. Rerun with ZEN_WHISPER_ACCEPT_SIGNATURE_CHANGE=1 only if you trust this app build. After launch, regrant Microphone and Accessibility permissions if recording or paste stops working."
  fi
  if [[ -n "$PREVIOUS_REQUIREMENT" && "$PREVIOUS_REQUIREMENT" != "$DESIGNATED_REQUIREMENT" && "${ZEN_WHISPER_ACCEPT_SIGNATURE_CHANGE:-}" != "1" ]]; then
    fail "app signing requirement changed. Rerun with ZEN_WHISPER_ACCEPT_SIGNATURE_CHANGE=1 only if you trust this app build. After launch, regrant Microphone and Accessibility permissions if recording or paste stops working."
  fi
fi
write_json_object "$SIGNING_TEMP" \
  --string identity "$IDENTITY" \
  --string designated_requirement "$DESIGNATED_REQUIREMENT" \
  --string executable_sha256 "$APP_EXECUTABLE_HASH" \
  --string app_path "$APP_DEST" \
  --string created_at "$CREATED_AT"
/bin/chmod 600 "$SIGNING_TEMP"

CRITICAL_INSTALL=0
INSTALL_COMMITTED=0
OLD_APP_MOVED=0
NEW_APP_MOVED=0
OLD_BACKEND_MOVED=0
NEW_BACKEND_MOVED=0
OLD_TOOLCHAIN_MOVED=0
NEW_TOOLCHAIN_MOVED=0
OLD_SIGNING_MOVED=0
NEW_SIGNING_MOVED=0
rollback_note() {
  echo "install_app.sh: rollback warning: $*" >&2
}

rollback_mv() {
  local source="$1"
  local destination="$2"
  /bin/mv "$source" "$destination" || rollback_note "failed to restore $source to $destination"
}

rollback_rm() {
  local path="$1"
  local label="$2"
  /bin/rm -rf "$path" || rollback_note "failed to remove $label: $path"
}

rollback_restore() {
  local backup="$1"
  local destination="$2"
  local label="$3"
  if [[ ! -e "$backup" && ! -L "$backup" ]]; then
    rollback_note "cannot restore $label; backup missing: $backup"
    return 0
  fi
  if [[ -e "$destination" || -L "$destination" ]]; then
    rollback_note "cannot restore $label; destination still exists: $destination; backup: $backup"
    return 0
  fi
  rollback_mv "$backup" "$destination"
}

rollback_install() {
  if [[ "$CRITICAL_INSTALL" == "1" && "$INSTALL_COMMITTED" != "1" ]]; then
    if [[ "$NEW_APP_MOVED" == "1" ]]; then
      rollback_rm "$APP_DEST" "incomplete app"
    fi
    if [[ "$OLD_APP_MOVED" == "1" ]]; then
      rollback_restore "$APP_BACKUP" "$APP_DEST" "previous app"
    fi
    rollback_rm "$APP_TEMP" "app staging path"

    if [[ "$NEW_BACKEND_MOVED" == "1" ]]; then
      rollback_rm "$BACKEND_FINAL" "incomplete backend"
    fi
    if [[ "$OLD_BACKEND_MOVED" == "1" ]]; then
      rollback_restore "$BACKEND_BACKUP" "$BACKEND_FINAL" "previous backend"
    fi

    if [[ "$NEW_TOOLCHAIN_MOVED" == "1" ]]; then
      rollback_rm "$TOOLCHAIN_JSON" "incomplete toolchain metadata"
    fi
    if [[ "$OLD_TOOLCHAIN_MOVED" == "1" ]]; then
      rollback_restore "$TOOLCHAIN_BACKUP" "$TOOLCHAIN_JSON" "previous toolchain metadata"
    fi
    if [[ "$NEW_SIGNING_MOVED" == "1" ]]; then
      rollback_rm "$SIGNING_JSON" "incomplete signing metadata"
    fi
    if [[ "$OLD_SIGNING_MOVED" == "1" ]]; then
      rollback_restore "$SIGNING_BACKUP" "$SIGNING_JSON" "previous signing metadata"
    fi
    rollback_rm "$APP_SUPPORT_STAGE" "App Support staging path"
  fi
}
cleanup_install_and_lock() {
  local status=$?
  trap '' INT TERM
  set +e
  rollback_install
  cleanup_app_lock
  set -e
  return "$status"
}

move_and_mark() {
  local source="$1"
  local destination="$2"
  local flag_name="$3"
  local error_message="$4"
  begin_deferred_signal
  if ! /bin/mv "$source" "$destination"; then
    if ! path_exists "$source" && path_exists "$destination"; then
      printf -v "$flag_name" '1'
    fi
    end_deferred_signal
    fail "$error_message"
  fi
  printf -v "$flag_name" '1'
  end_deferred_signal
}

trap cleanup_install_and_lock EXIT
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM
CRITICAL_INSTALL=1

if path_exists "$APP_DEST"; then
  move_and_mark "$APP_DEST" "$APP_BACKUP" OLD_APP_MOVED "failed to move existing app aside"
fi
move_and_mark "$APP_TEMP" "$APP_DEST" NEW_APP_MOVED "failed to install app atomically"

if path_exists "$BACKEND_FINAL"; then
  move_and_mark "$BACKEND_FINAL" "$BACKEND_BACKUP" OLD_BACKEND_MOVED "failed to move existing backend aside"
fi
move_and_mark "$BACKEND_STAGE" "$BACKEND_FINAL" NEW_BACKEND_MOVED "failed to install staged backend"

if path_exists "$TOOLCHAIN_JSON"; then
  move_and_mark "$TOOLCHAIN_JSON" "$TOOLCHAIN_BACKUP" OLD_TOOLCHAIN_MOVED "failed to move existing toolchain metadata aside"
fi
move_and_mark "$TOOLCHAIN_TEMP" "$TOOLCHAIN_JSON" NEW_TOOLCHAIN_MOVED "failed to install toolchain metadata"

if path_exists "$SIGNING_JSON"; then
  move_and_mark "$SIGNING_JSON" "$SIGNING_BACKUP" OLD_SIGNING_MOVED "failed to move existing signing metadata aside"
fi
move_and_mark "$SIGNING_TEMP" "$SIGNING_JSON" NEW_SIGNING_MOVED "failed to install signing metadata"

INSTALL_COMMITTED=1
CRITICAL_INSTALL=0
cleanup_app_lock
trap - EXIT INT TERM
remove_legacy_python_launch_agent

echo "Installed app: $APP_DEST"
reopen_app_if_needed
/bin/rm -rf "$APP_BACKUP" || true
/bin/rm -rf "$BACKEND_BACKUP" "$APP_SUPPORT_STAGE" || true
/bin/rm -f "$TOOLCHAIN_BACKUP" "$SIGNING_BACKUP" || true

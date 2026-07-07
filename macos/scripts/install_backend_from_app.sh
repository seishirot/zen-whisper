#!/usr/bin/env bash
set -euo pipefail

APP_PATH="${1:-/Applications/zen-whisper.app}"
APP_SUPPORT="${ZEN_WHISPER_APP_SUPPORT:-"$HOME/Library/Application Support/zen-whisper"}"
DEFAULT_APP_SUPPORT="$HOME/Library/Application Support/zen-whisper"
BACKEND_RESOURCES="$APP_PATH/Contents/Resources/backend"
TOOLCHAIN_JSON="$APP_SUPPORT/install/toolchain.json"
INSTALL_JSON="$APP_SUPPORT/backend/install.json"
VENV_MANIFEST_JSON="$APP_SUPPORT/backend/venv_manifest.json"

fail() {
  echo "install_backend_from_app.sh: $*" >&2
  exit 1
}

ACTIVE_CHILD=""

run_child() {
  "$@" &
  ACTIVE_CHILD="$!"
  wait "$ACTIVE_CHILD"
  local status="$?"
  ACTIVE_CHILD=""
  return "$status"
}

terminate_active_child() {
  local child="$ACTIVE_CHILD"
  if [[ -z "$child" ]]; then
    return 0
  fi
  echo "install_backend_from_app.sh: terminating active child process: $child" >&2
  /bin/kill -TERM "$child" 2>/dev/null || true
  local deadline=$((SECONDS + 5))
  while /bin/kill -0 "$child" 2>/dev/null && [[ "$SECONDS" -lt "$deadline" ]]; do
    sleep 0.1
  done
  if /bin/kill -0 "$child" 2>/dev/null; then
    echo "install_backend_from_app.sh: killing active child process: $child" >&2
    /bin/kill -KILL "$child" 2>/dev/null || true
  fi
  wait "$child" 2>/dev/null || true
  ACTIVE_CHILD=""
}

handle_signal() {
  local signal="${1:-TERM}"
  terminate_active_child
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

verify_app_signature() {
  local app_path="$1"
  local output
  if output="$(/usr/bin/codesign --verify --strict "$app_path" 2>&1)"; then
    return 0
  fi
  if is_local_trust_only_codesign_failure "$output"; then
    echo "install_backend_from_app.sh: accepting local self-signed code signature for $app_path: CSSMERR_TP_NOT_TRUSTED" >&2
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

json_value() {
  local key="$1"
  /usr/bin/sed -n "s/.*\"$key\": \"\\([^\"]*\\)\".*/\\1/p" "$TOOLCHAIN_JSON" | /usr/bin/head -n 1
}

resolve_executable() {
  local recorded="$1"
  local command_name="$2"
  if [[ -n "$recorded" && -x "$recorded" ]]; then
    echo "$recorded"
    return 0
  fi
  command -v "$command_name" 2>/dev/null || true
}

resolve_mise_executable() {
  local recorded="$1"
  if [[ -n "$recorded" && -x "$recorded" ]]; then
    echo "$recorded"
    return 0
  fi
  local candidate
  for candidate in \
    "$HOME/.local/bin/mise" \
    "$HOME/.local/share/mise/bin/mise" \
    "/opt/homebrew/bin/mise" \
    "/usr/local/bin/mise" \
    "/usr/bin/mise"
  do
    if [[ -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  command -v mise 2>/dev/null || true
}

resolve_recorded_executable() {
  local recorded="$1"
  if [[ -n "$recorded" && -x "$recorded" ]]; then
    echo "$recorded"
  fi
}

validate_app_support "$APP_SUPPORT"
[[ -d "$BACKEND_RESOURCES" ]] || fail "backend resources not found: $BACKEND_RESOURCES"
verify_app_signature "$APP_PATH" || fail "app bundle signature verification failed: $APP_PATH"
/bin/mkdir -p "$APP_SUPPORT/backend" "$APP_SUPPORT/install" "$APP_SUPPORT/models/huggingface" "$APP_SUPPORT/run" "$APP_SUPPORT/recordings"
/bin/chmod 700 "$APP_SUPPORT" "$APP_SUPPORT/backend" "$APP_SUPPORT/install" "$APP_SUPPORT/models" "$APP_SUPPORT/models/huggingface" "$APP_SUPPORT/run" "$APP_SUPPORT/recordings"
APP_INSTALL_LOCK="$APP_SUPPORT/install/app.lock"
if [[ -d "$APP_INSTALL_LOCK" ]]; then
  fail "app install is already running: $APP_INSTALL_LOCK"
fi
INSTALL_LOCK="$APP_SUPPORT/install/backend.lock"
INSTALL_LOCK_HELD=0
cleanup_lock() {
  local status=$?
  trap '' INT TERM
  set +e
  if [[ "${INSTALL_LOCK_HELD:-0}" == "1" ]]; then
    /bin/rmdir "$INSTALL_LOCK" 2>/dev/null || true
  fi
  set -e
  return "$status"
}
trap cleanup_lock EXIT
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM
acquire_dir_lock "$INSTALL_LOCK" INSTALL_LOCK_HELD "backend install or repair is already running"

MISE_PATH="${ZEN_WHISPER_MISE_PATH:-}"
UV_PATH="${ZEN_WHISPER_UV_PATH:-}"
PYTHON_PATH="${ZEN_WHISPER_PYTHON_PATH:-}"
MISE_CONFIG_DIR=""
MISE_RESOLVE_ERROR=""
MISE_RESOLVE_OUTPUT=""

mise_exec_capture() {
  local output
  local status
  if [[ -n "$MISE_CONFIG_DIR" ]]; then
    output="$("$MISE_PATH" -C "$MISE_CONFIG_DIR" exec -- "$@" 2>&1)"
  else
    output="$("$MISE_PATH" exec -- "$@" 2>&1)"
  fi
  status=$?
  if [[ "$status" -ne 0 ]]; then
    MISE_RESOLVE_ERROR="$output"
    return "$status"
  fi
  MISE_RESOLVE_ERROR=""
  MISE_RESOLVE_OUTPUT="$(printf '%s\n' "$output" | /usr/bin/tail -n 1)"
}

fail_missing_tool() {
  local tool_name="$1"
  if [[ -n "$MISE_RESOLVE_ERROR" ]]; then
    fail "$tool_name is not executable. mise output: $(printf '%s\n' "$MISE_RESOLVE_ERROR" | /usr/bin/tail -n 6 | /usr/bin/tr '\n' ' ')"
  fi
  fail "$tool_name is not executable. Re-run macos/scripts/install_app.sh from the repo."
}

if [[ -z "$MISE_PATH" || -z "$UV_PATH" || -z "$PYTHON_PATH" ]]; then
  if [[ "${ZEN_WHISPER_ALLOW_RECORDED_TOOLCHAIN:-}" != "1" && "${ZEN_WHISPER_ALLOW_BUNDLED_TOOLCHAIN:-}" != "1" ]]; then
    fail "toolchain paths must be provided by install_app.sh. Re-run macos/scripts/install_app.sh from the repo."
  fi
  if [[ -f "$TOOLCHAIN_JSON" ]]; then
    MISE_PATH="${MISE_PATH:-$(json_value mise_path)}"
    UV_PATH="${UV_PATH:-$(json_value uv_path)}"
    PYTHON_PATH="${PYTHON_PATH:-$(json_value python_path)}"
  fi
  if [[ -z "$UV_PATH" || -z "$PYTHON_PATH" ]]; then
    if [[ "${ZEN_WHISPER_ALLOW_BUNDLED_TOOLCHAIN:-}" == "1" && -f "$BACKEND_RESOURCES/.mise.toml" ]]; then
      MISE_CONFIG_DIR="$BACKEND_RESOURCES"
    fi
  fi
fi

MISE_PATH="$(resolve_mise_executable "$MISE_PATH")"
[[ -x "$MISE_PATH" ]] || fail "mise is not executable. Re-run macos/scripts/install_app.sh from the repo."
if [[ -z "$MISE_CONFIG_DIR" && "${ZEN_WHISPER_ALLOW_BUNDLED_TOOLCHAIN:-}" == "1" && -f "$BACKEND_RESOURCES/.mise.toml" ]]; then
  if [[ -z "$UV_PATH" || ! -x "$UV_PATH" || -z "$PYTHON_PATH" || ! -x "$PYTHON_PATH" ]]; then
    MISE_CONFIG_DIR="$BACKEND_RESOURCES"
  fi
fi
if [[ -n "$MISE_CONFIG_DIR" ]]; then
  "$MISE_PATH" trust "$MISE_CONFIG_DIR/.mise.toml" >/dev/null
fi
UV_PATH="$(resolve_recorded_executable "$UV_PATH")"
if [[ ! -x "$UV_PATH" ]]; then
  if mise_exec_capture which uv; then
    UV_PATH="$MISE_RESOLVE_OUTPUT"
  else
    UV_PATH=""
  fi
fi
[[ -x "$UV_PATH" ]] || fail_missing_tool "uv"
PYTHON_PATH="$(resolve_recorded_executable "$PYTHON_PATH")"
if [[ ! -x "$PYTHON_PATH" ]]; then
  if mise_exec_capture python -P -c 'import sys; print(sys.executable)'; then
    PYTHON_PATH="$MISE_RESOLVE_OUTPUT"
  else
    PYTHON_PATH=""
  fi
fi
[[ -x "$PYTHON_PATH" ]] || fail_missing_tool "Python"

json_file_value() {
  local file="$1"
  local key="$2"
  PYTHONSAFEPATH=1 "$PYTHON_PATH" -P - "$file" "$key" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text())
    print(data.get(sys.argv[2], ""))
except Exception:
    print("")
PY
}

SIGNING_JSON="$APP_SUPPORT/install/signing.json"
CURRENT_REQUIREMENT="$(/usr/bin/codesign -dr - "$APP_PATH" 2>&1 | /usr/bin/sed -n 's/^.*designated => //p')"
EXPECTED_APP_PATH="${ZEN_WHISPER_FINAL_APP_PATH:-"$APP_PATH"}"
BOOTSTRAP_BACKEND_INSTALL="${ZEN_WHISPER_BOOTSTRAP_BACKEND_INSTALL:-0}"
BASELINE_REQUIREMENT=""
BASELINE_APP_PATH=""
BASELINE_EXECUTABLE_HASH=""
if [[ -f "$SIGNING_JSON" ]]; then
  BASELINE_REQUIREMENT="$(json_file_value "$SIGNING_JSON" designated_requirement)"
  BASELINE_APP_PATH="$(json_file_value "$SIGNING_JSON" app_path)"
  BASELINE_EXECUTABLE_HASH="$(json_file_value "$SIGNING_JSON" executable_sha256)"
fi
if [[ -z "$BASELINE_REQUIREMENT" && "$BOOTSTRAP_BACKEND_INSTALL" != "1" ]]; then
  fail "signing baseline missing or unreadable: $SIGNING_JSON"
fi
if [[ -z "$BASELINE_APP_PATH" && "$BOOTSTRAP_BACKEND_INSTALL" != "1" ]]; then
  fail "signing app path missing or unreadable: $SIGNING_JSON"
fi
if [[ -z "$BASELINE_EXECUTABLE_HASH" && "$BOOTSTRAP_BACKEND_INSTALL" != "1" ]]; then
  fail "signing executable hash missing or unreadable: $SIGNING_JSON"
fi
if [[ -n "$BASELINE_APP_PATH" && "$BASELINE_APP_PATH" != "$EXPECTED_APP_PATH" && "$BOOTSTRAP_BACKEND_INSTALL" != "1" ]]; then
  fail "app path changed; refusing backend repair"
fi
if [[ "$EXPECTED_APP_PATH" != "/Applications/zen-whisper.app" ]]; then
  fail "daily app path must be /Applications/zen-whisper.app: $EXPECTED_APP_PATH"
fi
if [[ -n "$BASELINE_REQUIREMENT" && "$BASELINE_REQUIREMENT" != "$CURRENT_REQUIREMENT" && "$BOOTSTRAP_BACKEND_INSTALL" != "1" ]]; then
  fail "app signing requirement changed; refusing backend repair"
fi
CURRENT_EXECUTABLE_HASH="$(/usr/bin/shasum -a 256 "$APP_PATH/Contents/MacOS/zen-whisper" | /usr/bin/awk '{print $1}')"
if [[ -n "$BASELINE_EXECUTABLE_HASH" && "$BASELINE_EXECUTABLE_HASH" != "$CURRENT_EXECUTABLE_HASH" && "$BOOTSTRAP_BACKEND_INSTALL" != "1" ]]; then
  fail "app executable hash changed; refusing backend repair"
fi

WHEEL="$(/bin/ls "$BACKEND_RESOURCES"/zen_whisper_mac_backend-*.whl 2>/dev/null | /usr/bin/head -n 1)"
[[ -n "$WHEEL" ]] || fail "backend wheel missing in app resources"
REQUIREMENTS="$BACKEND_RESOURCES/requirements-mlx.txt"
MANIFEST="$BACKEND_RESOURCES/BackendBundleManifest.json"
[[ -f "$REQUIREMENTS" ]] || fail "requirements file missing: $REQUIREMENTS"
[[ -f "$MANIFEST" ]] || fail "manifest missing: $MANIFEST"
manifest_value() {
  local key="$1"
  json_file_value "$MANIFEST" "$key"
}

WHEEL_HASH="$(/usr/bin/shasum -a 256 "$WHEEL" | /usr/bin/awk '{print $1}')"
REQ_HASH="$(/usr/bin/shasum -a 256 "$REQUIREMENTS" | /usr/bin/awk '{print $1}')"
REGISTRY_HASH="$(/usr/bin/shasum -a 256 "$BACKEND_RESOURCES/model_registry.json" | /usr/bin/awk '{print $1}')"
[[ "$WHEEL_HASH" == "$(manifest_value wheel_hash)" ]] || fail "backend wheel hash does not match manifest"
[[ "$REQ_HASH" == "$(manifest_value requirements_hash)" ]] || fail "requirements hash does not match manifest"
[[ "$REGISTRY_HASH" == "$(manifest_value registry_hash)" ]] || fail "registry hash does not match manifest"
EXPECTED_BACKEND_VERSION="$(manifest_value backend_version)"
EXPECTED_PROTOCOL_VERSION="$(manifest_value protocol_version)"
EXPECTED_BACKEND_CODE_HASH="$(manifest_value backend_code_hash)"
EXPECTED_VENV_MANIFEST_HASH="$(manifest_value venv_manifest_hash)"
EXPECTED_PYTHON_RUNTIME_MANIFEST_HASH="$(manifest_value python_runtime_manifest_hash)"
[[ -n "$EXPECTED_BACKEND_VERSION" ]] || fail "manifest backend_version missing"
[[ -n "$EXPECTED_PROTOCOL_VERSION" ]] || fail "manifest protocol_version missing"
[[ -n "$EXPECTED_BACKEND_CODE_HASH" ]] || fail "manifest backend_code_hash missing"
[[ -n "$EXPECTED_VENV_MANIFEST_HASH" ]] || fail "manifest venv_manifest_hash missing"
[[ -n "$EXPECTED_PYTHON_RUNTIME_MANIFEST_HASH" ]] || fail "manifest python_runtime_manifest_hash missing"

STAGING="$APP_SUPPORT/backend/.venv.staging.$$"
CURRENT="$APP_SUPPORT/backend/.venv"
PREVIOUS="$APP_SUPPORT/backend/.venv.previous.$$"
INSTALL_STAGING="$APP_SUPPORT/backend/install.json.staging.$$"
INSTALL_PREVIOUS="$APP_SUPPORT/backend/install.json.previous.$$"
VENV_MANIFEST_STAGING="$APP_SUPPORT/backend/venv_manifest.json.staging.$$"
VENV_MANIFEST_PREVIOUS="$APP_SUPPORT/backend/venv_manifest.json.previous.$$"
PYTHON_RUNTIME_MANIFEST_JSON="$APP_SUPPORT/backend/python_runtime_manifest.json"
PYTHON_RUNTIME_MANIFEST_STAGING="$APP_SUPPORT/backend/python_runtime_manifest.json.staging.$$"
PYTHON_RUNTIME_MANIFEST_PREVIOUS="$APP_SUPPORT/backend/python_runtime_manifest.json.previous.$$"
/bin/rm -rf "$STAGING" "$INSTALL_STAGING" "$VENV_MANIFEST_STAGING" "$PYTHON_RUNTIME_MANIFEST_STAGING"
ensure_absent "$STAGING" "backend staging venv"
ensure_absent "$PREVIOUS" "previous backend venv backup"
ensure_absent "$INSTALL_STAGING" "install metadata staging"
ensure_absent "$INSTALL_PREVIOUS" "previous install metadata backup"
ensure_absent "$VENV_MANIFEST_STAGING" "backend venv manifest staging"
ensure_absent "$VENV_MANIFEST_PREVIOUS" "previous backend venv manifest backup"
ensure_absent "$PYTHON_RUNTIME_MANIFEST_STAGING" "Python runtime manifest staging"
ensure_absent "$PYTHON_RUNTIME_MANIFEST_PREVIOUS" "previous Python runtime manifest backup"

cleanup_staging_and_lock() {
  local status=$?
  trap '' INT TERM
  set +e
  if [[ "${CRITICAL_SWAP:-0}" != "1" ]]; then
    /bin/rm -rf "$STAGING"
    /bin/rm -f "$INSTALL_STAGING" "$VENV_MANIFEST_STAGING" "$PYTHON_RUNTIME_MANIFEST_STAGING"
  fi
  cleanup_lock
  set -e
  return "$status"
}
trap cleanup_staging_and_lock EXIT
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM

run_child "$UV_PATH" venv --relocatable --python "$PYTHON_PATH" "$STAGING"
run_child "$UV_PATH" pip install --python "$STAGING/bin/python" -r "$REQUIREMENTS" "$WHEEL"

PROBE_JSON="$(PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P -m zen_whisper_mac_backend.probe runtime-probe)"
PROBE_ARCH="$(PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P - "$PROBE_JSON" python_arch <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get(sys.argv[2], ""))
PY
)"
PROBE_BACKEND_VERSION="$(PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P - "$PROBE_JSON" backend_version <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get(sys.argv[2], ""))
PY
)"
PROBE_PROTOCOL_VERSION="$(PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P - "$PROBE_JSON" protocol_version <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get(sys.argv[2], ""))
PY
)"
PROBE_REGISTRY_HASH="$(PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P - "$PROBE_JSON" registry_hash <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get(sys.argv[2], ""))
PY
)"
PROBE_BACKEND_CODE_HASH="$(PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P - "$PROBE_JSON" backend_code_hash <<'PY'
import json
import sys
print(json.loads(sys.argv[1]).get(sys.argv[2], ""))
PY
)"
[[ "$PROBE_ARCH" == "arm64" ]] || fail "backend Python is not arm64: $PROBE_ARCH"
[[ "$PROBE_BACKEND_VERSION" == "$EXPECTED_BACKEND_VERSION" ]] || fail "unexpected backend version: $PROBE_BACKEND_VERSION"
[[ "$PROBE_PROTOCOL_VERSION" == "$EXPECTED_PROTOCOL_VERSION" ]] || fail "unexpected protocol version: $PROBE_PROTOCOL_VERSION"
[[ "$PROBE_REGISTRY_HASH" == "$REGISTRY_HASH" ]] || fail "installed backend registry hash mismatch"
[[ "$PROBE_BACKEND_CODE_HASH" == "$EXPECTED_BACKEND_CODE_HASH" ]] || fail "installed backend code hash mismatch"
/usr/bin/find "$STAGING" \( -name '__pycache__' -type d -o -name '*.pyc' -type f \) -print -exec /bin/rm -rf {} + >/dev/null 2>&1 || true

PYTHON_RUNTIME_PREFIX="$(PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P - <<'PY'
import sys
print(sys.base_prefix)
PY
)"
[[ "$PYTHON_RUNTIME_PREFIX" = /* ]] || fail "Python runtime prefix is not absolute: $PYTHON_RUNTIME_PREFIX"

PYTHONSAFEPATH=1 "$PYTHON_PATH" -P - "$STAGING" "$PYTHON_RUNTIME_PREFIX" "$VENV_MANIFEST_STAGING" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
runtime_root = Path(sys.argv[2]).resolve()
output = Path(sys.argv[3])


def is_python_bytecode(path: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    return "__pycache__" in rel_parts or path.suffix == ".pyc"


def target_inside_allowed_roots(path: Path) -> bool:
    target = Path(os.readlink(path))
    if not target.is_absolute():
        target = path.parent / target
    if not target.exists():
        return False
    resolved = target.resolve()
    for allowed_root in (root, runtime_root):
        try:
            resolved.relative_to(allowed_root)
            return True
        except ValueError:
            continue
    return False


entries = []
for path in sorted(root.rglob("*")):
    if is_python_bytecode(path):
        raise SystemExit(f"unexpected Python bytecode in backend venv: {path.relative_to(root).as_posix()}")
    rel = path.relative_to(root).as_posix()
    st = path.lstat()
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        continue
    if stat.S_ISLNK(st.st_mode):
        if not target_inside_allowed_roots(path):
            raise SystemExit(f"external backend venv symlink: {rel}")
        entries.append(
            {
                "path": rel,
                "kind": "symlink",
                "mode": oct(mode),
                "target": os.readlink(path),
            }
        )
    elif stat.S_ISREG(st.st_mode):
        entries.append(
            {
                "path": rel,
                "kind": "file",
                "mode": oct(mode),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    else:
        raise SystemExit(f"unsupported backend venv entry: {rel}")

root_digest = hashlib.sha256()
for entry in sorted(entries, key=lambda item: item["path"]):
    root_digest.update(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())
    root_digest.update(b"\n")

data = {
    "schema_version": 1,
    "root_hash": root_digest.hexdigest(),
    "files": entries,
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
/bin/chmod 600 "$VENV_MANIFEST_STAGING"
VENV_MANIFEST_HASH="$(/usr/bin/shasum -a 256 "$VENV_MANIFEST_STAGING" | /usr/bin/awk '{print $1}')"
if [[ "$EXPECTED_VENV_MANIFEST_HASH" != "pending-install" && "$EXPECTED_VENV_MANIFEST_HASH" != "per-user" && "$EXPECTED_VENV_MANIFEST_HASH" != "$VENV_MANIFEST_HASH" ]]; then
  fail "backend venv manifest hash does not match signed app manifest"
fi

PYTHONSAFEPATH=1 "$PYTHON_PATH" -P - "$PYTHON_RUNTIME_PREFIX" "$PYTHON_RUNTIME_MANIFEST_STAGING" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2])


def target_inside_root(path: Path) -> bool:
    target = Path(os.readlink(path))
    if not target.is_absolute():
        target = path.parent / target
    if not target.exists():
        return False
    try:
        target.resolve().relative_to(root)
        return True
    except ValueError:
        return False


entries = []
for path in sorted(root.rglob("*")):
    rel = path.relative_to(root).as_posix()
    st = path.lstat()
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        continue
    if stat.S_ISLNK(st.st_mode):
        if not target_inside_root(path):
            raise SystemExit(f"external Python runtime symlink: {rel}")
        entries.append(
            {
                "path": rel,
                "kind": "symlink",
                "mode": oct(mode),
                "target": os.readlink(path),
            }
        )
    elif stat.S_ISREG(st.st_mode):
        entries.append(
            {
                "path": rel,
                "kind": "file",
                "mode": oct(mode),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    else:
        raise SystemExit(f"unsupported Python runtime entry: {rel}")

root_digest = hashlib.sha256()
for entry in sorted(entries, key=lambda item: item["path"]):
    root_digest.update(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())
    root_digest.update(b"\n")

data = {
    "schema_version": 1,
    "root_path": str(root),
    "root_hash": root_digest.hexdigest(),
    "files": entries,
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
/bin/chmod 600 "$PYTHON_RUNTIME_MANIFEST_STAGING"
PYTHON_RUNTIME_MANIFEST_HASH="$(/usr/bin/shasum -a 256 "$PYTHON_RUNTIME_MANIFEST_STAGING" | /usr/bin/awk '{print $1}')"
if [[ "$EXPECTED_PYTHON_RUNTIME_MANIFEST_HASH" != "pending-install" && "$EXPECTED_PYTHON_RUNTIME_MANIFEST_HASH" != "per-user" && "$EXPECTED_PYTHON_RUNTIME_MANIFEST_HASH" != "$PYTHON_RUNTIME_MANIFEST_HASH" ]]; then
  fail "Python runtime manifest hash does not match signed app manifest"
fi

PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P - \
  "$INSTALL_STAGING" \
  "${ZEN_WHISPER_FINAL_BACKEND_PYTHON:-"$CURRENT/bin/python"}" \
  "$PYTHON_PATH" \
  "$PYTHON_RUNTIME_PREFIX" \
  "${ZEN_WHISPER_FINAL_APP_PATH:-"$APP_PATH"}" \
  "$WHEEL_HASH" \
  "$REQ_HASH" \
  "$REGISTRY_HASH" \
  "$EXPECTED_BACKEND_VERSION" \
  "$EXPECTED_PROTOCOL_VERSION" \
  "$EXPECTED_BACKEND_CODE_HASH" \
  "$VENV_MANIFEST_HASH" \
  "$PYTHON_RUNTIME_MANIFEST_HASH" \
  "$(/usr/bin/shasum -a 256 "$PYTHON_PATH" | /usr/bin/awk '{print $1}')" <<'PY'
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

import zen_whisper_mac_backend

(
    output,
    python_path,
    python_source,
    python_runtime_prefix,
    app_path,
    wheel_hash,
    req_hash,
    registry_hash,
    expected_backend_version,
    expected_protocol_version,
    backend_code_hash,
    venv_manifest_hash,
    python_runtime_manifest_hash,
    python_source_hash,
) = sys.argv[1:]
expected_protocol_version = int(expected_protocol_version)
if zen_whisper_mac_backend.BACKEND_VERSION != expected_backend_version:
    raise SystemExit("metadata backend version mismatch")
if zen_whisper_mac_backend.PROTOCOL_VERSION != expected_protocol_version:
    raise SystemExit("metadata protocol version mismatch")
data = {
    "backend_version": zen_whisper_mac_backend.BACKEND_VERSION,
    "protocol_version": zen_whisper_mac_backend.PROTOCOL_VERSION,
    "python_path": python_path,
    "python_runtime_prefix": python_runtime_prefix,
    "python_source": python_source,
    "python_source_sha256": python_source_hash,
    "python_version": ".".join(map(str, sys.version_info[:3])),
    "python_arch": platform.machine(),
    "app_path": app_path,
    "wheel_hash": wheel_hash,
    "backend_code_hash": backend_code_hash,
    "requirements_hash": req_hash,
    "registry_hash": registry_hash,
    "venv_manifest_hash": venv_manifest_hash,
    "python_runtime_manifest_hash": python_runtime_manifest_hash,
    "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
Path(output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
/bin/chmod 600 "$INSTALL_STAGING"

CRITICAL_SWAP=0
SWAP_COMMITTED=0
CURRENT_MOVED=0
STAGING_MOVED=0
OLD_INSTALL_MOVED=0
NEW_INSTALL_MOVED=0
OLD_MANIFEST_MOVED=0
NEW_MANIFEST_MOVED=0
OLD_RUNTIME_MANIFEST_MOVED=0
NEW_RUNTIME_MANIFEST_MOVED=0
rollback_note() {
  echo "install_backend_from_app.sh: rollback warning: $*" >&2
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

rollback_swap() {
  if [[ "$CRITICAL_SWAP" == "1" && "$SWAP_COMMITTED" != "1" ]]; then
    if [[ "$STAGING_MOVED" == "1" ]]; then
      rollback_rm "$CURRENT" "incomplete backend venv"
    fi
    rollback_rm "$STAGING" "backend staging venv"
    if [[ "$CURRENT_MOVED" == "1" ]]; then
      rollback_restore "$PREVIOUS" "$CURRENT" "previous backend venv"
    fi
    if [[ "$NEW_INSTALL_MOVED" == "1" ]]; then
      rollback_rm "$INSTALL_JSON" "incomplete install metadata"
    fi
    rollback_rm "$INSTALL_STAGING" "install metadata staging file"
    if [[ "$OLD_INSTALL_MOVED" == "1" ]]; then
      rollback_restore "$INSTALL_PREVIOUS" "$INSTALL_JSON" "previous install metadata"
    fi
    if [[ "$NEW_MANIFEST_MOVED" == "1" ]]; then
      rollback_rm "$VENV_MANIFEST_JSON" "incomplete backend venv manifest"
    fi
    rollback_rm "$VENV_MANIFEST_STAGING" "backend venv manifest staging file"
    if [[ "$OLD_MANIFEST_MOVED" == "1" ]]; then
      rollback_restore "$VENV_MANIFEST_PREVIOUS" "$VENV_MANIFEST_JSON" "previous backend venv manifest"
    fi
    if [[ "$NEW_RUNTIME_MANIFEST_MOVED" == "1" ]]; then
      rollback_rm "$PYTHON_RUNTIME_MANIFEST_JSON" "incomplete Python runtime manifest"
    fi
    rollback_rm "$PYTHON_RUNTIME_MANIFEST_STAGING" "Python runtime manifest staging file"
    if [[ "$OLD_RUNTIME_MANIFEST_MOVED" == "1" ]]; then
      rollback_restore "$PYTHON_RUNTIME_MANIFEST_PREVIOUS" "$PYTHON_RUNTIME_MANIFEST_JSON" "previous Python runtime manifest"
    fi
  fi
}
cleanup_swap_and_lock() {
  local status=$?
  trap '' INT TERM
  set +e
  if [[ "${CRITICAL_SWAP:-0}" == "1" ]]; then
    rollback_swap
  else
    rollback_rm "$STAGING" "backend staging venv"
    rollback_rm "$INSTALL_STAGING" "install metadata staging file"
    rollback_rm "$VENV_MANIFEST_STAGING" "backend venv manifest staging file"
    rollback_rm "$PYTHON_RUNTIME_MANIFEST_STAGING" "Python runtime manifest staging file"
  fi
  cleanup_lock
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

trap cleanup_swap_and_lock EXIT
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM
CRITICAL_SWAP=1
if path_exists "$CURRENT"; then
  move_and_mark "$CURRENT" "$PREVIOUS" CURRENT_MOVED "failed to move existing backend venv aside"
fi
move_and_mark "$STAGING" "$CURRENT" STAGING_MOVED "failed to atomically install backend venv"
if path_exists "$INSTALL_JSON"; then
  move_and_mark "$INSTALL_JSON" "$INSTALL_PREVIOUS" OLD_INSTALL_MOVED "failed to move existing backend metadata aside"
fi
move_and_mark "$INSTALL_STAGING" "$INSTALL_JSON" NEW_INSTALL_MOVED "failed to atomically install backend metadata"
if path_exists "$VENV_MANIFEST_JSON"; then
  move_and_mark "$VENV_MANIFEST_JSON" "$VENV_MANIFEST_PREVIOUS" OLD_MANIFEST_MOVED "failed to move existing backend venv manifest aside"
fi
move_and_mark "$VENV_MANIFEST_STAGING" "$VENV_MANIFEST_JSON" NEW_MANIFEST_MOVED "failed to atomically install backend venv manifest"
[[ "$(/usr/bin/shasum -a 256 "$VENV_MANIFEST_JSON" | /usr/bin/awk '{print $1}')" == "$VENV_MANIFEST_HASH" ]] || fail "backend venv manifest hash mismatch after swap"
if path_exists "$PYTHON_RUNTIME_MANIFEST_JSON"; then
  move_and_mark "$PYTHON_RUNTIME_MANIFEST_JSON" "$PYTHON_RUNTIME_MANIFEST_PREVIOUS" OLD_RUNTIME_MANIFEST_MOVED "failed to move existing Python runtime manifest aside"
fi
move_and_mark "$PYTHON_RUNTIME_MANIFEST_STAGING" "$PYTHON_RUNTIME_MANIFEST_JSON" NEW_RUNTIME_MANIFEST_MOVED "failed to atomically install Python runtime manifest"
[[ "$(/usr/bin/shasum -a 256 "$PYTHON_RUNTIME_MANIFEST_JSON" | /usr/bin/awk '{print $1}')" == "$PYTHON_RUNTIME_MANIFEST_HASH" ]] || fail "Python runtime manifest hash mismatch after swap"
PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$CURRENT/bin/python" -P - "$INSTALL_JSON" "$REGISTRY_HASH" "$EXPECTED_BACKEND_VERSION" "$EXPECTED_PROTOCOL_VERSION" "$EXPECTED_BACKEND_CODE_HASH" <<'PY'
import json
import sys
from pathlib import Path

from zen_whisper_mac_backend.probe import runtime_probe

install_json, expected_registry_hash, expected_backend_version, expected_protocol_version, expected_backend_code_hash = sys.argv[1:]
expected_protocol_version = int(expected_protocol_version)
metadata = json.loads(Path(install_json).read_text())
probe = runtime_probe()
if metadata["python_arch"] != probe["python_arch"]:
    raise SystemExit("install metadata Python arch mismatch")
if metadata["backend_version"] != probe["backend_version"]:
    raise SystemExit("install metadata backend version mismatch")
if metadata["backend_version"] != expected_backend_version:
    raise SystemExit("install metadata manifest backend version mismatch")
if metadata["protocol_version"] != probe["protocol_version"]:
    raise SystemExit("install metadata protocol mismatch")
if metadata["protocol_version"] != expected_protocol_version:
    raise SystemExit("install metadata manifest protocol mismatch")
if probe["registry_hash"] != expected_registry_hash:
    raise SystemExit("installed backend registry hash mismatch after swap")
if metadata["backend_code_hash"] != expected_backend_code_hash:
    raise SystemExit("install metadata backend code hash mismatch")
if probe["backend_code_hash"] != expected_backend_code_hash:
    raise SystemExit("installed backend code hash mismatch after swap")
PY

SWAP_COMMITTED=1
CRITICAL_SWAP=0
cleanup_lock
trap - EXIT INT TERM
rollback_rm "$PREVIOUS" "previous backend venv backup"
rollback_rm "$INSTALL_PREVIOUS" "previous install metadata backup"
rollback_rm "$VENV_MANIFEST_PREVIOUS" "previous backend venv manifest backup"
rollback_rm "$PYTHON_RUNTIME_MANIFEST_PREVIOUS" "previous Python runtime manifest backup"

echo "Installed backend venv: $CURRENT"

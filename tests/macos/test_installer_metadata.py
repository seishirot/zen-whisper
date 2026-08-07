from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WRITER = REPO_ROOT / "macos/scripts/write_json_object.py"
INSTALL_APP = REPO_ROOT / "macos/scripts/install_app.sh"


def test_signing_json_writer_preserves_designated_requirement_quotes(tmp_path: Path) -> None:
    output = tmp_path / "signing.json"
    requirement = (
        'identifier "com.seishirot.zenwhisper" and '
        'certificate leaf[subject.CN] = "zen-whisper Local Code Signing" and '
        "anchor apple generic"
    )

    subprocess.run(
        [
            sys.executable,
            str(WRITER),
            str(output),
            "--string",
            "identity",
            "zen-whisper Local Code Signing",
            "--string",
            "designated_requirement",
            requirement,
            "--string",
            "app_path",
            "/Applications/zen-whisper.app",
            "--string",
            "created_at",
            "2026-07-02T00:00:00Z",
        ],
        check=True,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["designated_requirement"] == requirement
    assert data["app_path"] == "/Applications/zen-whisper.app"


def test_manifest_json_writer_keeps_protocol_version_numeric(tmp_path: Path) -> None:
    output = tmp_path / "BackendBundleManifest.json"
    subprocess.run(
        [
            sys.executable,
            str(WRITER),
            str(output),
            "--int",
            "protocol_version",
            "1",
            "--string",
            "backend_version",
            "0.1.0",
        ],
        check=True,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["protocol_version"] == 1
    assert data["backend_version"] == "0.1.0"


def test_install_app_does_not_embed_designated_requirement_in_raw_json() -> None:
    text = INSTALL_APP.read_text(encoding="utf-8")
    assert '"designated_requirement": "$DESIGNATED_REQUIREMENT"' not in text
    assert 'write_json_object "$SIGNING_TEMP"' in text


def test_scripts_run_managed_tools_from_repo_root() -> None:
    install_app = INSTALL_APP.read_text(encoding="utf-8")
    phase0_gate = (REPO_ROOT / "macos/scripts/phase0_asr_gate.sh").read_text(encoding="utf-8")

    assert 'REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"\ncd "$REPO_ROOT"' in install_app
    assert 'REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"\ncd "$REPO_ROOT"' in phase0_gate


def test_daily_app_destination_is_not_customizable() -> None:
    install_app = INSTALL_APP.read_text(encoding="utf-8")
    install_backend = (REPO_ROOT / "macos/scripts/install_backend_from_app.sh").read_text(encoding="utf-8")

    assert '[[ "$dest" == "/Applications/zen-whisper.app" ]] || fail "daily app path must be /Applications/zen-whisper.app: $dest"' in install_app
    assert "ZEN_WHISPER_ALLOW_CUSTOM_APP_DEST" not in install_app
    assert 'if [[ "$EXPECTED_APP_PATH" != "/Applications/zen-whisper.app" ]]; then' in install_backend
    assert "ZEN_WHISPER_ALLOW_CUSTOM_APP_DEST" not in install_backend


def test_backend_repair_does_not_use_recorded_toolchain_without_opt_in() -> None:
    install_backend = (REPO_ROOT / "macos/scripts/install_backend_from_app.sh").read_text(encoding="utf-8")
    install_app = INSTALL_APP.read_text(encoding="utf-8")

    assert "ZEN_WHISPER_ALLOW_RECORDED_TOOLCHAIN" in install_backend
    assert "ZEN_WHISPER_ALLOW_BUNDLED_TOOLCHAIN" in install_backend
    assert "toolchain paths must be provided by install_app.sh" in install_backend
    assert '"/opt/homebrew/bin/mise"' in install_backend
    assert '"$MISE_PATH" trust "$MISE_CONFIG_DIR/.mise.toml"' in install_backend
    assert 'if [[ -z "$UV_PATH" || ! -x "$UV_PATH" || -z "$PYTHON_PATH" || ! -x "$PYTHON_PATH" ]]; then' in install_backend
    assert "mise_exec_capture()" in install_backend
    assert '"$MISE_PATH" -C "$MISE_CONFIG_DIR" exec -- "$@" 2>&1' in install_backend
    assert "fail_missing_tool()" in install_backend
    assert "resolve_recorded_executable()" in install_backend
    assert 'UV_PATH="$(resolve_recorded_executable "$UV_PATH")"' in install_backend
    assert 'PYTHON_PATH="$(resolve_recorded_executable "$PYTHON_PATH")"' in install_backend
    assert 'command -v "$command_name"' in install_backend
    assert 'ZEN_WHISPER_MISE_PATH="$MISE_PATH"' in install_app
    assert 'ZEN_WHISPER_UV_PATH="$UV_PATH"' in install_app
    assert 'ZEN_WHISPER_PYTHON_PATH="$PYTHON_PATH"' in install_app
    assert '/bin/cp "$REPO_ROOT/.mise.toml" "$APP_STAGING/Contents/Resources/backend/.mise.toml"' in install_app


def test_install_app_bootstraps_backend_before_final_signing_baseline() -> None:
    install_backend = (REPO_ROOT / "macos/scripts/install_backend_from_app.sh").read_text(encoding="utf-8")
    install_app = INSTALL_APP.read_text(encoding="utf-8")

    assert "ZEN_WHISPER_BOOTSTRAP_BACKEND_INSTALL=1" in install_app
    assert 'BOOTSTRAP_BACKEND_INSTALL="${ZEN_WHISPER_BOOTSTRAP_BACKEND_INSTALL:-0}"' in install_backend
    assert '"$BOOTSTRAP_BACKEND_INSTALL" != "1"' in install_backend
    assert "ZEN_WHISPER_ACCEPT_SIGNATURE_CHANGE" not in install_backend
    backend_install_index = install_app.index("ZEN_WHISPER_BOOTSTRAP_BACKEND_INSTALL=1")
    final_signing_index = install_app.index('write_json_object "$SIGNING_TEMP"')
    assert backend_install_index < final_signing_index


def test_backend_repair_checks_signing_executable_hash() -> None:
    install_backend = (REPO_ROOT / "macos/scripts/install_backend_from_app.sh").read_text(encoding="utf-8")

    assert "BASELINE_EXECUTABLE_HASH" in install_backend
    assert 'json_file_value "$SIGNING_JSON" executable_sha256' in install_backend
    assert "signing executable hash missing or unreadable" in install_backend
    assert 'CURRENT_EXECUTABLE_HASH="$(/usr/bin/shasum -a 256 "$APP_PATH/Contents/MacOS/zen-whisper"' in install_backend
    assert "app executable hash changed; refusing backend repair" in install_backend


def test_backend_install_writes_static_venv_manifest() -> None:
    install_backend = (REPO_ROOT / "macos/scripts/install_backend_from_app.sh").read_text(encoding="utf-8")
    install_app = INSTALL_APP.read_text(encoding="utf-8")

    assert 'VENV_MANIFEST_JSON="$APP_SUPPORT/backend/venv_manifest.json"' in install_backend
    assert 'VENV_MANIFEST_STAGING="$APP_SUPPORT/backend/venv_manifest.json.staging.$$"' in install_backend
    assert 'VENV_MANIFEST_HASH="$(/usr/bin/shasum -a 256 "$VENV_MANIFEST_STAGING"' in install_backend
    assert 'EXPECTED_VENV_MANIFEST_HASH="$(manifest_value venv_manifest_hash)"' in install_backend
    assert 'EXPECTED_PYTHON_RUNTIME_MANIFEST_HASH="$(manifest_value python_runtime_manifest_hash)"' in install_backend
    assert 'EXPECTED_VENV_MANIFEST_HASH" != "pending-install" && "$EXPECTED_VENV_MANIFEST_HASH" != "per-user"' in install_backend
    assert 'EXPECTED_PYTHON_RUNTIME_MANIFEST_HASH" != "pending-install" && "$EXPECTED_PYTHON_RUNTIME_MANIFEST_HASH" != "per-user"' in install_backend
    assert '"venv_manifest_hash": venv_manifest_hash' in install_backend
    assert '"python_runtime_manifest_hash": python_runtime_manifest_hash' in install_backend
    assert '"python_runtime_prefix": python_runtime_prefix' in install_backend
    assert '"python_source_sha256": python_source_hash' in install_backend
    assert '"$UV_PATH" venv --relocatable --python "$PYTHON_PATH" "$STAGING"' in install_backend
    assert '--string venv_manifest_hash "pending-install"' in install_app
    assert '--string python_runtime_manifest_hash "pending-install"' in install_app
    assert '--string venv_manifest_hash "per-user"' in install_app
    assert '--string python_runtime_manifest_hash "per-user"' in install_app
    assert 'verify_app_signature "$APP_TEMP"' in install_app
    assert 'unexpected Python bytecode in backend venv' in install_backend
    assert 'external backend venv symlink' in install_backend
    assert 'external Python runtime symlink' in install_backend
    assert "stat.S_ISLNK" in install_backend
    assert "/usr/bin/find \"$STAGING\"" in install_backend
    assert 'move_and_mark "$VENV_MANIFEST_STAGING" "$VENV_MANIFEST_JSON" NEW_MANIFEST_MOVED' in install_backend
    assert 'move_and_mark "$PYTHON_RUNTIME_MANIFEST_STAGING" "$PYTHON_RUNTIME_MANIFEST_JSON" NEW_RUNTIME_MANIFEST_MOVED' in install_backend
    assert "unsupported backend venv entry" in install_backend
    assert '"kind": "symlink"' in install_backend
    assert 'for entry in sorted(entries, key=lambda item: item["path"]):' in install_backend
    sealed_manifest_body = install_backend[
        install_backend.index('PYTHONSAFEPATH=1 "$PYTHON_PATH" -P - "$STAGING" "$PYTHON_RUNTIME_PREFIX" "$VENV_MANIFEST_STAGING"'):
        install_backend.index('PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P -')
    ]
    assert '"created_at"' not in sealed_manifest_body


def test_backend_install_cleans_staging_before_critical_swap() -> None:
    install_backend = (REPO_ROOT / "macos/scripts/install_backend_from_app.sh").read_text(encoding="utf-8")

    assert "cleanup_staging_and_lock()" in install_backend
    assert 'if [[ "${CRITICAL_SWAP:-0}" != "1" ]]; then' in install_backend
    assert '/bin/rm -rf "$STAGING"' in install_backend
    assert '/bin/rm -rf "$STAGING" "$INSTALL_STAGING" "$VENV_MANIFEST_STAGING" "$PYTHON_RUNTIME_MANIFEST_STAGING"' in install_backend
    assert "trap cleanup_staging_and_lock EXIT" in install_backend
    assert "trap cleanup_swap_and_lock EXIT" in install_backend
    swap_cleanup_body = install_backend[
        install_backend.index("cleanup_swap_and_lock() {"):
        install_backend.index("move_and_mark()")
    ]
    assert "trap '' INT TERM" in swap_cleanup_body
    assert "set +e" in swap_cleanup_body
    assert "set -e" in swap_cleanup_body
    assert 'if [[ "${CRITICAL_SWAP:-0}" == "1" ]]; then' in swap_cleanup_body
    assert 'rollback_rm "$STAGING" "backend staging venv"' in swap_cleanup_body
    assert 'rollback_rm "$INSTALL_STAGING" "install metadata staging file"' in swap_cleanup_body
    assert "trap 'handle_signal INT' INT" in install_backend
    assert "trap 'handle_signal TERM' TERM" in install_backend
    assert "terminate_active_child()" in install_backend
    assert 'run_child "$UV_PATH" venv --relocatable --python "$PYTHON_PATH" "$STAGING"' in install_backend
    assert (
        'run_child "$UV_PATH" pip install --no-config --python "$STAGING/bin/python" '
        '--require-hashes -r "$REQUIREMENTS"'
    ) in install_backend
    assert (
        'run_child "$UV_PATH" pip install --no-config --python "$STAGING/bin/python" '
        '--no-deps "$WHEEL"'
    ) in install_backend
    assert install_backend.index("trap cleanup_staging_and_lock EXIT") < install_backend.index("CRITICAL_SWAP=1")
    assert 'rollback_note()' in install_backend
    assert 'rollback warning' in install_backend
    assert 'rollback_restore "$PREVIOUS" "$CURRENT" "previous backend venv"' in install_backend
    assert "destination still exists" in install_backend
    assert 'ensure_absent "$PREVIOUS" "previous backend venv backup"' in install_backend
    assert 'ensure_absent "$INSTALL_PREVIOUS" "previous install metadata backup"' in install_backend
    assert 'ensure_absent "$VENV_MANIFEST_PREVIOUS" "previous backend venv manifest backup"' in install_backend
    assert 'ensure_absent "$PYTHON_RUNTIME_MANIFEST_PREVIOUS" "previous Python runtime manifest backup"' in install_backend


def test_installers_mark_move_done_when_mv_reports_failure_after_rename() -> None:
    install_backend = (REPO_ROOT / "macos/scripts/install_backend_from_app.sh").read_text(encoding="utf-8")
    install_app = INSTALL_APP.read_text(encoding="utf-8")

    for script in (install_backend, install_app):
        assert 'if ! path_exists "$source" && path_exists "$destination"; then' in script
        assert "printf -v \"$flag_name\" '1'" in script
        assert script.index('if ! /bin/mv "$source" "$destination"; then') < script.index(
            'if ! path_exists "$source" && path_exists "$destination"; then'
        )


def test_install_app_logs_rollback_restore_failures() -> None:
    install_app = INSTALL_APP.read_text(encoding="utf-8")

    assert 'rollback_note()' in install_app
    assert 'install_app.sh: rollback warning' in install_app
    assert 'rollback_restore "$APP_BACKUP" "$APP_DEST" "previous app"' in install_app
    assert 'rollback_restore "$BACKEND_BACKUP" "$BACKEND_FINAL" "previous backend"' in install_app
    assert "destination still exists" in install_app


def test_installer_resolves_swift_with_xcrun() -> None:
    install_app = INSTALL_APP.read_text(encoding="utf-8")

    assert 'SWIFT_PATH="$(/usr/bin/xcrun --find swift' in install_app
    assert '"$SWIFT_PATH" build -c release --package-path "$SWIFT_DIR" --arch arm64' in install_app
    assert "\nswift build " not in install_app


def test_install_app_packages_bundled_postprocessor_catalog() -> None:
    install_app = INSTALL_APP.read_text(encoding="utf-8")
    catalog_store = (
        REPO_ROOT
        / "macos/ZenWhisper/ZenWhisper/EnhancementCatalogStore.swift"
    ).read_text(encoding="utf-8")

    generate_command = (
        'PYTHONSAFEPATH=1 "$PYTHON_PATH" -P \\\n'
        '  "$REPO_ROOT/macos/scripts/generate_postprocessor_catalog.py" \\\n'
        '  "$REPO_ROOT/postprocessors.default.toml" \\\n'
        '  "$APP_STAGING/Contents/Resources/postprocessors.default.json"'
    )
    assert generate_command in install_app
    destination = '"$APP_STAGING/Contents/Resources/postprocessors.default.json"'
    assert install_app.count(destination) == 1
    assert (
        '/bin/cp "$SWIFT_DIR/ZenWhisper/Resources/postprocessors.default.json"'
        not in install_app
    )
    assert install_app.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
    assert install_app.index(generate_command) < install_app.index(
        '/usr/bin/codesign --force --deep --sign "$IDENTITY" "$APP_TEMP"'
    )
    main_lookup = "postprocessorsURL(in: Bundle.main)"
    assert main_lookup in catalog_store
    resource_bundle_probe = 'let resourceBundleName = "ZenWhisper_ZenWhisper.bundle"'
    assert resource_bundle_probe in catalog_store
    assert "Bundle(url: candidate)" in catalog_store
    assert catalog_store.index(main_lookup) < catalog_store.index(
        resource_bundle_probe
    )
    assert "postprocessorsURL(in: Bundle.module)" not in catalog_store
    assert "let bundles = [Bundle.main, Bundle.module]" not in catalog_store


def test_native_installer_removes_legacy_python_launch_agent() -> None:
    install_app = INSTALL_APP.read_text(encoding="utf-8")

    assert "remove_legacy_python_launch_agent()" in install_app
    assert "launch_agent_is_legacy_python()" in install_app
    assert "launch_agent_is_legacy_uv()" in install_app
    assert "com.zen-whisper" in install_app
    assert "/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0'" in install_app
    assert '"/usr/bin/open" && "$second_arg" == "/Applications/zen-whisper.app"' in install_app
    assert "Preserved existing LaunchAgent" in install_app
    assert "/bin/launchctl bootout" in install_app
    assert "launchctl_failure_is_benign()" in install_app
    assert "launchctl_failure_detail()" in install_app
    assert "legacy plist was removed but launchd state may require logout, reboot, or manual cleanup" in install_app
    assert "legacy LaunchAgent bootout failed" in install_app
    assert "legacy LaunchAgent remove failed" in install_app
    assert "removed legacy Python LaunchAgent plist but launchd cleanup may still be pending" in install_app
    assert "/bin/rm -f \"$plist\"" in install_app
    assert "src/main.py|zen_whisper|zen-whisper\\.py" in install_app
    assert 'basename "$first_arg")" == "uv"' in install_app
    assert 'arg" == "run"' in install_app
    assert 'arg" == "zen-whisper"' in install_app

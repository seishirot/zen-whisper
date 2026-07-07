from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_APP = REPO_ROOT / "macos/scripts/install_app.sh"


def test_install_app_quits_running_app_before_replacing_bundle() -> None:
    text = INSTALL_APP.read_text(encoding="utf-8")

    assert "quit_running_app()" in text
    assert "ZEN_WHISPER_SKIP_APP_QUIT" in text
    assert "/usr/bin/pgrep -x zen-whisper" in text
    assert 'tell application id "com.seishirot.zenwhisper" to quit' in text
    quit_call = text.index("quit_running_app\n\nremove_legacy_python_launch_agent()")
    staged_install = text.index('/bin/mkdir -p "$APP_SUPPORT_STAGE/install"')
    assert quit_call < staged_install


def test_install_app_reopens_app_when_it_was_running() -> None:
    text = INSTALL_APP.read_text(encoding="utf-8")

    assert "APP_WAS_RUNNING=1" in text
    assert "APP_REOPENED=0" in text
    assert "reopen_app_if_needed()" in text
    assert "ZEN_WHISPER_REOPEN_AFTER_INSTALL" in text
    assert '/usr/bin/open "$APP_DEST"' in text
    assert 'if ! /usr/bin/open "$APP_DEST" >/dev/null 2>&1; then' in text
    assert 'install_app.sh: warning: could not reopen app: $APP_DEST' in text
    assert 'echo "Reopened app: $APP_DEST"' in text
    assert "echo \"Installed app: $APP_DEST\"\nreopen_app_if_needed" in text


def test_install_app_reopens_previous_app_on_failed_install_cleanup() -> None:
    text = INSTALL_APP.read_text(encoding="utf-8")

    assert 'if [[ "${INSTALL_COMMITTED:-0}" != "1" ]]; then\n    reopen_app_if_needed\n  fi' in text
    assert 'if [[ "${APP_WAS_RUNNING:-0}" != "1" || "${APP_REOPENED:-0}" == "1"' in text


def test_install_app_releases_lock_and_reopens_before_best_effort_post_commit_cleanup() -> None:
    text = INSTALL_APP.read_text(encoding="utf-8")

    committed = text.index("INSTALL_COMMITTED=1")
    cleanup_lock = text.index("cleanup_app_lock\ntrap - EXIT INT TERM\nremove_legacy_python_launch_agent")
    legacy_cleanup = text.index("remove_legacy_python_launch_agent\n\necho \"Installed app: $APP_DEST\"")
    reopen = text.index("echo \"Installed app: $APP_DEST\"\nreopen_app_if_needed")
    best_effort_cleanup = text.index('/bin/rm -rf "$APP_BACKUP" || true')

    assert committed < cleanup_lock < legacy_cleanup < reopen < best_effort_cleanup
    assert '/bin/rm -rf "$BACKEND_BACKUP" "$APP_SUPPORT_STAGE" || true' in text
    assert '/bin/rm -f "$TOOLCHAIN_BACKUP" "$SIGNING_BACKUP" || true' in text


def test_install_app_rejects_stale_backup_paths_before_install_swap() -> None:
    text = INSTALL_APP.read_text(encoding="utf-8")

    preflight_body = text[
        text.index('APP_BACKUP="$APP_PARENT/.zen-whisper.app.previous.$$"'):
        text.index('quit_running_app()')
    ]
    assert '/bin/rm -rf "$APP_TEMP" "$APP_SUPPORT_STAGE"' in preflight_body
    assert '/bin/rm -rf "$APP_TEMP" "$APP_BACKUP"' not in preflight_body
    assert '/bin/rm -rf "$APP_SUPPORT_STAGE" "$BACKEND_BACKUP"' not in preflight_body
    assert '/bin/rm -rf "$TOOLCHAIN_BACKUP" "$SIGNING_BACKUP"' not in preflight_body
    assert 'ensure_absent "$APP_TEMP" "app staging"' in text
    assert 'ensure_absent "$APP_BACKUP" "app backup"' in text
    assert 'ensure_absent "$APP_SUPPORT_STAGE" "App Support staging"' in text
    assert 'ensure_absent "$BACKEND_BACKUP" "backend backup"' in text
    assert 'ensure_absent "$TOOLCHAIN_BACKUP" "toolchain metadata backup"' in text
    assert 'ensure_absent "$SIGNING_BACKUP" "signing metadata backup"' in text
    assert text.index('ensure_absent "$SIGNING_BACKUP" "signing metadata backup"') < text.index("CRITICAL_INSTALL=1")

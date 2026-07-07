from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_APP = REPO_ROOT / "macos/scripts/install_app.sh"
INSTALL_BACKEND = REPO_ROOT / "macos/scripts/install_backend_from_app.sh"


def test_install_app_traps_after_app_lock_before_backend_lock() -> None:
    text = INSTALL_APP.read_text(encoding="utf-8")
    trap_index = text.index("trap cleanup_app_lock EXIT")
    int_trap_index = text.index("trap 'handle_signal INT' INT", trap_index)
    term_trap_index = text.index("trap 'handle_signal TERM' TERM", int_trap_index)
    app_lock_index = text.index('acquire_dir_lock "$APP_INSTALL_LOCK" APP_LOCK_HELD')
    backend_lock_index = text.index('acquire_dir_lock "$BACKEND_INSTALL_LOCK" BACKEND_LOCK_HELD')

    assert trap_index < int_trap_index < term_trap_index < app_lock_index < backend_lock_index
    assert "APP_LOCK_HELD=0" in text
    assert "APP_LOCK_HELD:-0" in text
    assert "BACKEND_LOCK_HELD=0" in text
    cleanup_body = text[text.index("cleanup_app_lock() {"):text.index("trap cleanup_app_lock EXIT")]
    assert "trap '' INT TERM" in cleanup_body
    assert "set +e" in cleanup_body
    assert "set -e" in cleanup_body
    install_cleanup_body = text[text.index("cleanup_install_and_lock() {"):text.index("move_and_mark()")]
    assert "trap '' INT TERM" in install_cleanup_body
    assert "set +e" in install_cleanup_body
    assert "set -e" in install_cleanup_body
    assert "handle_signal()" in text
    assert "acquire_dir_lock()" in text
    assert "begin_deferred_signal" in text
    assert "printf -v \"$flag_name\" '1'" in text
    end_deferred_body = text[text.index("end_deferred_signal() {"):text.index("acquire_dir_lock()")]
    assert end_deferred_body.index("trap 'handle_signal INT' INT") < end_deferred_body.index('local signal="$DEFERRED_SIGNAL"')


def test_backend_repair_traps_before_backend_lock_acquisition() -> None:
    text = INSTALL_BACKEND.read_text(encoding="utf-8")

    trap_index = text.index("trap cleanup_lock EXIT")
    int_trap_index = text.index("trap 'handle_signal INT' INT", trap_index)
    term_trap_index = text.index("trap 'handle_signal TERM' TERM", int_trap_index)
    lock_index = text.index('acquire_dir_lock "$INSTALL_LOCK" INSTALL_LOCK_HELD')

    assert trap_index < int_trap_index < term_trap_index < lock_index
    assert "INSTALL_LOCK_HELD=0" in text
    assert "INSTALL_LOCK_HELD:-0" in text
    cleanup_body = text[text.index("cleanup_lock() {"):text.index("trap cleanup_lock EXIT")]
    assert "trap '' INT TERM" in cleanup_body
    assert "set +e" in cleanup_body
    assert "set -e" in cleanup_body
    assert "acquire_dir_lock()" in text
    assert "begin_deferred_signal" in text
    end_deferred_body = text[text.index("end_deferred_signal() {"):text.index("acquire_dir_lock()")]
    assert end_deferred_body.index("trap 'handle_signal INT' INT") < end_deferred_body.index('local signal="$DEFERRED_SIGNAL"')

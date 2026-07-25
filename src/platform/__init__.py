"""プラットフォーム抽象化レイヤー。OS 判定とプラットフォーム固有機能のファクトリを提供する。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

_SIGKILL = getattr(signal, "SIGKILL", 9)


def is_windows() -> bool:
    """Windows 環境かどうかを返す。"""
    return sys.platform == "win32"


def is_mac() -> bool:
    """macOS 環境かどうかを返す。"""
    return sys.platform == "darwin"


def paste_hotkey() -> tuple[str, str]:
    """ペースト用のキーコンビネーションを返す。"""
    if is_mac():
        return ("command", "v")
    return ("ctrl", "v")


def split_command(command: str) -> list[str]:
    """Split a trusted command string using the current OS command-line rules."""
    if is_windows():
        from src.platform.windows import split_command as windows_split_command

        return windows_split_command(command)

    import shlex

    return shlex.split(command, posix=True)


def subprocess_run_options() -> dict[str, object]:
    """Return platform-specific subprocess options for background GUI use."""
    if is_windows():
        from src.platform.windows import subprocess_run_options as windows_options

        return windows_options()
    return {"start_new_session": True}


def command_uses_windows_batch(
    executable: str,
    environment: dict[str, str],
) -> bool:
    """Return whether an executable resolves to a Windows batch launcher."""
    if not is_windows():
        return False

    from src.platform.windows import command_uses_windows_batch as windows_check

    return windows_check(executable, environment)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a timed-out command and its descendants where supported."""
    if is_windows():
        from src.platform.windows import terminate_process_tree as windows_terminate

        windows_terminate(process)
        return
    try:
        os.killpg(process.pid, _SIGKILL)
    except (OSError, ValueError):
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

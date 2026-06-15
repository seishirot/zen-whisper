"""プラットフォーム抽象化レイヤー。OS 判定とプラットフォーム固有機能のファクトリを提供する。"""

from __future__ import annotations

import sys


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

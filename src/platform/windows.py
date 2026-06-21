"""Windows 固有の実装。win32clipboard, winreg, ctypes.windll を使用する機能を集約。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import time
import tkinter as tk
import winreg
from pathlib import Path

import win32clipboard

logger = logging.getLogger(__name__)


# ── オーディオエンドポイント ─────────────────────────

_AUDIO_CAPTURE_DEVICES_REG_PATH = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
)
_DEVICE_STATE_ACTIVE = 1
_DEVICE_DESC_PROPERTY = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_DEVICE_INTERFACE_FRIENDLY_NAME_PROPERTY = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"


def _is_human_audio_property(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    hidden_fragments = ("\\", "#", "{", "}", "inf:", "%windir%")
    return not any(fragment in text.lower() for fragment in hidden_fragments)


def _endpoint_property_strings(properties_key: object) -> set[str]:
    values: set[str] = set()
    try:
        value_count = winreg.QueryInfoKey(properties_key)[1]
        for index in range(value_count):
            _name, value, _value_type = winreg.EnumValue(properties_key, index)
            if _is_human_audio_property(value):
                values.add(value.strip())
    except OSError:
        logger.debug("Audio endpoint properties enumeration failed", exc_info=True)
    return values


def _endpoint_property(properties_key: object, name: str) -> str | None:
    try:
        value, _value_type = winreg.QueryValueEx(properties_key, name)
    except OSError:
        return None
    return value.strip() if _is_human_audio_property(value) else None


def active_capture_device_names() -> set[str] | None:
    """Return readable names for Windows active capture endpoints.

    PortAudio may expose disabled or stale devices through legacy host APIs.
    The Windows endpoint registry keeps the current active capture endpoints,
    which is enough to filter the tray menu without adding another dependency.
    """
    names: set[str] = set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _AUDIO_CAPTURE_DEVICES_REG_PATH) as root:
            endpoint_count = winreg.QueryInfoKey(root)[0]
            for endpoint_index in range(endpoint_count):
                endpoint_id = winreg.EnumKey(root, endpoint_index)
                with winreg.OpenKey(root, endpoint_id) as endpoint_key:
                    try:
                        state, _state_type = winreg.QueryValueEx(endpoint_key, "DeviceState")
                    except OSError:
                        continue
                    if state != _DEVICE_STATE_ACTIVE:
                        continue
                    with winreg.OpenKey(endpoint_key, "Properties") as properties_key:
                        properties = _endpoint_property_strings(properties_key)
                        desc = _endpoint_property(properties_key, _DEVICE_DESC_PROPERTY)
                        interface = _endpoint_property(
                            properties_key,
                            _DEVICE_INTERFACE_FRIENDLY_NAME_PROPERTY,
                        )
                        names.update(properties)
                        if desc:
                            names.add(desc)
                        if interface:
                            names.add(interface)
                        if desc and interface:
                            names.add(f"{desc} ({interface})")
    except OSError:
        logger.debug("Active capture endpoint names could not be read", exc_info=True)
        return None
    return names


# ── クリップボード ─────────────────────────────────────

_CLIPBOARD_MAX_RETRIES = 3
_CLIPBOARD_RETRY_INTERVAL_SEC = 0.05


def get_clipboard_text() -> str | None:
    """現在のクリップボードのテキストを取得する。テキストでなければ None。最大3回リトライ。"""
    for attempt in range(_CLIPBOARD_MAX_RETRIES):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                    return data
            finally:
                win32clipboard.CloseClipboard()
            return None
        except Exception:
            if attempt < _CLIPBOARD_MAX_RETRIES - 1:
                logger.debug("クリップボード読み取りリトライ (%d/%d)", attempt + 1, _CLIPBOARD_MAX_RETRIES)
                time.sleep(_CLIPBOARD_RETRY_INTERVAL_SEC)
            else:
                logger.warning("クリップボードの読み取りに失敗しました（リトライ上限）", exc_info=True)
    return None


def set_clipboard_text(text: str) -> bool:
    """クリップボードにテキストを設定する。成功時 True、失敗時 False。最大3回リトライ。"""
    for attempt in range(_CLIPBOARD_MAX_RETRIES):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            if attempt < _CLIPBOARD_MAX_RETRIES - 1:
                logger.debug("クリップボード書き込みリトライ (%d/%d)", attempt + 1, _CLIPBOARD_MAX_RETRIES)
                time.sleep(_CLIPBOARD_RETRY_INTERVAL_SEC)
            else:
                logger.warning("クリップボードへの書き込みに失敗しました（リトライ上限）", exc_info=True)
    return False


# ── スタートアップ登録 ────────────────────────────────

_APP_NAME = "zen-whisper"
_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _get_startup_command() -> str:
    """スタートアップ登録用のコマンドを構築する。"""
    vbs_path = Path(__file__).resolve().parent.parent.parent / "start.vbs"
    return f'wscript.exe "{vbs_path}"'


def is_startup_registered() -> bool:
    """スタートアップに登録されているかを返す。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def register_startup() -> None:
    """スタートアップに登録する。"""
    cmd = _get_startup_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)
        logger.info("スタートアップに登録しました: %s", cmd)
    except OSError:
        logger.exception("スタートアップの登録に失敗しました")


def unregister_startup() -> None:
    """スタートアップから解除する。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, _APP_NAME)
        logger.info("スタートアップから解除しました")
    except FileNotFoundError:
        logger.debug("スタートアップに登録されていません")
    except OSError:
        logger.exception("スタートアップの解除に失敗しました")


# ── オーバーレイ（Win32 ウィンドウ属性）───────────────

def get_active_monitor_rect() -> tuple[int, int, int, int]:
    """アクティブウィンドウがあるモニターのワークエリア (left, top, right, bottom) を返す。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            hwnd = user32.GetDesktopWindow()

        hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD),
            ]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        rc = mi.rcWork
        return (rc.left, rc.top, rc.right, rc.bottom)
    except Exception:
        logger.debug("モニター情報の取得に失敗、フォールバック", exc_info=True)
        return (0, 0, 1920, 1080)


def setup_overlay_window(root: tk.Tk) -> None:
    """tkinter ウィンドウに Windows 固有の属性を設定する（WS_EX_NOACTIVATE）。"""
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_APPWINDOW = 0x00040000
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = (style | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


# ── ホットキー（Win32 修飾キー検出）───────────────────

VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

_user32 = ctypes.windll.user32


def is_key_down(vk: int) -> bool:
    """GetAsyncKeyState で指定キーが押下中かを返す。"""
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def is_win_down() -> bool:
    return is_key_down(VK_LWIN) or is_key_down(VK_RWIN)


def is_shift_down() -> bool:
    return is_key_down(VK_LSHIFT) or is_key_down(VK_RSHIFT)


def is_ctrl_down() -> bool:
    return is_key_down(VK_LCONTROL) or is_key_down(VK_RCONTROL)


def is_alt_down() -> bool:
    return is_key_down(VK_LMENU) or is_key_down(VK_RMENU)

"""Observe host keyboard paths while an RDP window owns the foreground.

This diagnostic intentionally does not trigger ZenWhisper or suppress input.
It logs only Ctrl, Shift, Space, and the comparison key A.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes

from pynput import keyboard


if sys.platform != "win32":
    raise SystemExit("This probe is Windows-only")


logger = logging.getLogger("rdp_input_probe")

WM_INPUT = 0x00FF
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
RI_KEY_BREAK = 0x0001
LLKHF_INJECTED = 0x00000010

VK_SPACE = 0x20
VK_A = 0x41
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3

_SHIFT_KEYS = {VK_SHIFT, VK_LSHIFT, VK_RSHIFT}
_CONTROL_KEYS = {VK_CONTROL, VK_LCONTROL, VK_RCONTROL}
_OBSERVED_KEYS = _SHIFT_KEYS | _CONTROL_KEYS | {VK_SPACE, VK_A}
_OBSERVED_SCAN_CODES = {0x1D, 0x1E, 0x2A, 0x36, 0x39}

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _POINT(ctypes.Structure):
    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


class _MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _POINT),
        ("lPrivate", wintypes.DWORD),
    )


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    )


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = (
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    )


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = (
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    )


class _RAWKEYBOARD(ctypes.Structure):
    _fields_ = (
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    )


class _PathObserver:
    def __init__(self, path: str) -> None:
        self._path = path
        self._pressed_modifiers: set[str] = set()
        self._pressed_keys: set[int] = set()

    def observe(
        self,
        virtual_key: int,
        *,
        key_up: bool,
        scan_code: int,
        flags: int,
        injected: bool | None = None,
    ) -> None:
        observed_key = _normalize_observed_key(virtual_key, scan_code)
        if observed_key is None:
            return
        repeated = not key_up and observed_key in self._pressed_keys
        if key_up:
            self._pressed_keys.discard(observed_key)
        else:
            self._pressed_keys.add(observed_key)

        modifier = _modifier_name(observed_key)
        if modifier is not None and not key_up:
            self._pressed_modifiers.add(modifier)

        combo = "-"
        if observed_key in (VK_SPACE, VK_A) and not key_up:
            combo = _matched_combo(observed_key, self._pressed_modifiers)

        injected_text = "n/a" if injected is None else str(injected).lower()
        logger.info(
            "%s event=%s key=%s vk=0x%02X scan=0x%02X flags=0x%02X "
            "mods=%s repeat=%s injected=%s match=%s",
            self._path,
            "up" if key_up else "down",
            _key_name(observed_key),
            virtual_key,
            scan_code,
            flags,
            "+".join(sorted(self._pressed_modifiers)) or "-",
            str(repeated).lower(),
            injected_text,
            combo,
        )

        if modifier is not None and key_up:
            self._pressed_modifiers.discard(modifier)


def _modifier_name(virtual_key: int) -> str | None:
    if virtual_key in _SHIFT_KEYS:
        return "shift"
    if virtual_key in _CONTROL_KEYS:
        return "ctrl"
    return None


def _normalize_observed_key(
    virtual_key: int,
    scan_code: int,
) -> int | None:
    if virtual_key in _OBSERVED_KEYS:
        return virtual_key
    if scan_code == 0x39:
        return VK_SPACE
    if scan_code == 0x1E:
        return VK_A
    if scan_code in (0x2A, 0x36):
        return VK_SHIFT
    if scan_code == 0x1D:
        return VK_CONTROL
    return None


def _key_name(virtual_key: int) -> str:
    if virtual_key == VK_SPACE:
        return "space"
    if virtual_key == VK_A:
        return "a"
    return _modifier_name(virtual_key) or f"vk-0x{virtual_key:02X}"


def _matched_combo(virtual_key: int, modifiers: set[str]) -> str:
    key_name = _key_name(virtual_key)
    if modifiers == {"shift"}:
        return f"shift+{key_name}"
    if modifiers == {"ctrl", "shift"}:
        return f"ctrl+shift+{key_name}"
    return "-"


def _start_low_level_probe() -> keyboard.Listener:
    observer = _PathObserver("LL_HOOK")

    def event_filter(message: int, data: object) -> None:
        if message not in (
            WM_KEYDOWN,
            WM_KEYUP,
            WM_SYSKEYDOWN,
            WM_SYSKEYUP,
        ):
            return
        virtual_key = int(getattr(data, "vkCode"))
        scan_code = int(getattr(data, "scanCode"))
        if (
            virtual_key not in _OBSERVED_KEYS
            and scan_code not in _OBSERVED_SCAN_CODES
        ):
            return
        flags = int(getattr(data, "flags"))
        observer.observe(
            virtual_key,
            key_up=message in (WM_KEYUP, WM_SYSKEYUP),
            scan_code=scan_code,
            flags=flags,
            injected=bool(flags & LLKHF_INJECTED),
        )

    listener = keyboard.Listener(
        on_press=lambda _key: None,
        on_release=lambda _key: None,
        win32_event_filter=event_filter,
    )
    listener.daemon = True
    listener.start()
    return listener


class _RawInputWindow:
    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._observer = _PathObserver("RAW_INPUT")
        self._class_name = f"ZenWhisperRdpInputProbe-{os.getpid()}"
        self._window: int | None = None
        self._window_proc = WNDPROC(self._dispatch)
        self._configure_api()
        self._hinstance = self._kernel32.GetModuleHandleW(None)

    def _configure_api(self) -> None:
        self._kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self._user32.RegisterClassExW.argtypes = (
            ctypes.POINTER(_WNDCLASSEXW),
        )
        self._user32.RegisterClassExW.restype = wintypes.ATOM
        self._user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HANDLE,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        )
        self._user32.CreateWindowExW.restype = wintypes.HWND
        self._user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32.DefWindowProcW.restype = LRESULT
        self._user32.RegisterRawInputDevices.argtypes = (
            ctypes.POINTER(_RAWINPUTDEVICE),
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.RegisterRawInputDevices.restype = wintypes.BOOL
        self._user32.GetRawInputData.argtypes = (
            wintypes.HANDLE,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.UINT),
            wintypes.UINT,
        )
        self._user32.GetRawInputData.restype = wintypes.UINT
        self._user32.GetMessageW.argtypes = (
            ctypes.POINTER(_MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.TranslateMessage.argtypes = (ctypes.POINTER(_MSG),)
        self._user32.DispatchMessageW.argtypes = (ctypes.POINTER(_MSG),)
        self._user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

    def create(self) -> None:
        window_class = _WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        window_class.lpfnWndProc = self._window_proc
        window_class.hInstance = self._hinstance
        window_class.lpszClassName = self._class_name
        if not self._user32.RegisterClassExW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())

        hwnd_message = wintypes.HWND(-3)
        window = self._user32.CreateWindowExW(
            0,
            self._class_name,
            "",
            0,
            0,
            0,
            0,
            0,
            hwnd_message,
            None,
            self._hinstance,
            None,
        )
        if not window:
            raise ctypes.WinError(ctypes.get_last_error())
        self._window = int(window)

        device = _RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, window)
        if not self._user32.RegisterRawInputDevices(
            ctypes.byref(device),
            1,
            ctypes.sizeof(_RAWINPUTDEVICE),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close_after(self, seconds: float) -> None:
        if seconds <= 0 or self._window is None:
            return

        def close() -> None:
            assert self._window is not None
            self._user32.PostMessageW(self._window, WM_CLOSE, 0, 0)

        timer = threading.Timer(seconds, close)
        timer.daemon = True
        timer.start()

    def pump(self) -> None:
        message = _MSG()
        while True:
            result = self._user32.GetMessageW(
                ctypes.byref(message), None, 0, 0
            )
            if result == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            if result == 0:
                return
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

    def _dispatch(
        self,
        hwnd: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        if message == WM_INPUT:
            self._read_input(lparam)
        elif message == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))

    def _read_input(self, raw_handle: int) -> None:
        size = wintypes.UINT()
        header_size = ctypes.sizeof(_RAWINPUTHEADER)
        result = self._user32.GetRawInputData(
            raw_handle,
            RID_INPUT,
            None,
            ctypes.byref(size),
            header_size,
        )
        if result == 0xFFFFFFFF:
            logger.error("GetRawInputData(size) failed: %s", ctypes.WinError())
            return
        buffer = ctypes.create_string_buffer(size.value)
        result = self._user32.GetRawInputData(
            raw_handle,
            RID_INPUT,
            buffer,
            ctypes.byref(size),
            header_size,
        )
        if result == 0xFFFFFFFF:
            logger.error("GetRawInputData(data) failed: %s", ctypes.WinError())
            return
        header = ctypes.cast(
            buffer, ctypes.POINTER(_RAWINPUTHEADER)
        ).contents
        if header.dwType != RIM_TYPEKEYBOARD:
            return
        raw_keyboard = ctypes.cast(
            ctypes.addressof(buffer) + header_size,
            ctypes.POINTER(_RAWKEYBOARD),
        ).contents
        self._observer.observe(
            int(raw_keyboard.VKey),
            key_up=bool(raw_keyboard.Flags & RI_KEY_BREAK),
            scan_code=int(raw_keyboard.MakeCode),
            flags=int(raw_keyboard.Flags),
        )

    def unregister(self) -> None:
        device = _RAWINPUTDEVICE(0x01, 0x06, RIDEV_REMOVE, None)
        self._user32.RegisterRawInputDevices(
            ctypes.byref(device),
            1,
            ctypes.sizeof(_RAWINPUTDEVICE),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=180.0,
        help="seconds before the probe exits automatically; 0 disables",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    raw_window = _RawInputWindow()
    listener: keyboard.Listener | None = None
    try:
        raw_window.create()
        listener = _start_low_level_probe()
        raw_window.close_after(args.duration)
        logger.info(
            "READY duration=%.0fs; observe-only; press Shift+Space and "
            "Ctrl+Shift+Space, or Shift+A for comparison, while mstsc "
            "is foreground/full-screen",
            args.duration,
        )
        raw_window.pump()
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        raw_window.unregister()
        if listener is not None:
            listener.stop()
            listener.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

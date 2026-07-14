"""pytest 共通設定。

GUI / オーディオ等ネイティブ依存のテストモジュールは
ヘッドレス CI 環境ではインポートできないため collection 段階でスキップする。
"""

from __future__ import annotations

import importlib
import os
import sys
import types

sys.dont_write_bytecode = True

# pynput and pystray otherwise try to connect to X11 during test collection.
# Their built-in dummy backends retain the import API needed by headless tests.
_HEADLESS_LINUX = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
if _HEADLESS_LINUX:
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")
    os.environ.setdefault("PYSTRAY_BACKEND", "dummy")

try:
    importlib.import_module("pyautogui")
except Exception:
    sys.modules["pyautogui"] = types.SimpleNamespace(
        FAILSAFE=False,
        hotkey=lambda *_args, **_kwargs: None,
        press=lambda *_args, **_kwargs: None,
    )

collect_ignore: list[str] = []

# pynput's dummy Key enum aliases special keys, so real key-identity tests are
# meaningful only with a native backend. This matches the previous CI guard.
if os.environ.get("PYNPUT_BACKEND") == "dummy":
    collect_ignore.append("test_hotkey.py")

_GUARDED_MODULES: dict[str, str] = {
    "test_hotkey.py": "pynput",
    "test_audio_devices.py": "sounddevice",
    "test_overlay.py": "pystray",
    "test_recorder.py": "sounddevice",
    "test_sounds.py": "sounddevice",
}

for test_file, dependency in _GUARDED_MODULES.items():
    try:
        importlib.import_module(dependency)
    except Exception:
        collect_ignore.append(test_file)

"""pytest 共通設定。

GUI / オーディオ等ネイティブ依存のテストモジュールは
ヘッドレス CI 環境ではインポートできないため collection 段階でスキップする。
"""

from __future__ import annotations

import importlib
import sys
import types

sys.dont_write_bytecode = True

try:
    importlib.import_module("pyautogui")
except Exception:
    sys.modules["pyautogui"] = types.SimpleNamespace(
        FAILSAFE=False,
        hotkey=lambda *_args, **_kwargs: None,
        press=lambda *_args, **_kwargs: None,
    )

collect_ignore: list[str] = []

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

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


def _install_sounddevice_stub() -> None:
    """Let app modules import where PortAudio cannot initialize (headless CI).

    Tests that need real audio are already ignored above; the stub fails loudly
    if any remaining test actually tries to use audio.
    """
    stub = types.ModuleType("sounddevice")

    class PortAudioError(Exception):
        pass

    def _unavailable(*_args, **_kwargs):
        raise PortAudioError("sounddevice is unavailable in this test environment")

    stub.PortAudioError = PortAudioError
    stub.default = types.SimpleNamespace(device=(None, None), samplerate=None, channels=None)
    stub._initialized = 0
    for name in ("query_devices", "query_hostapis", "check_input_settings", "InputStream",
                 "play", "stop", "wait", "get_stream", "_initialize", "_terminate"):
        setattr(stub, name, _unavailable)
    sys.modules["sounddevice"] = stub


if "sounddevice" not in sys.modules:
    try:
        importlib.import_module("sounddevice")
    except Exception:
        _install_sounddevice_stub()

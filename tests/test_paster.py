"""src.paster のテスト。"""

from __future__ import annotations

import sys

import pytest

from src.config import OutputConfig


class TestPasterImport:
    """paster モジュールのインポートテスト。"""

    def test_import_paste_function(self):
        from src.paster import paste
        assert callable(paste)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_clipboard_functions_imported(self):
        """Windows では win32clipboard ベースの関数がインポートされる。"""
        from src.paster import _get_clipboard_text, _set_clipboard_text
        assert callable(_get_clipboard_text)
        assert callable(_set_clipboard_text)

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_mac_clipboard_functions_imported(self):
        """Mac では pyperclip ベースの関数がインポートされる。"""
        from src.paster import _get_clipboard_text, _set_clipboard_text
        assert callable(_get_clipboard_text)
        assert callable(_set_clipboard_text)


class TestClipboardPlatformWindows:
    """Windows クリップボード操作のテスト。"""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_roundtrip(self):
        from src.platform.windows import get_clipboard_text, set_clipboard_text

        test_text = "zen-whisper テスト文字列"
        assert set_clipboard_text(test_text) is True
        result = get_clipboard_text()
        assert result == test_text

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_get_clipboard_returns_str_or_none(self):
        from src.platform.windows import get_clipboard_text

        result = get_clipboard_text()
        assert result is None or isinstance(result, str)


class TestPasteSubmit:
    """貼り付け後 Enter 送信のテスト。"""

    def _patch_paste_dependencies(self, monkeypatch, events):
        import src.paster as paster_module

        monkeypatch.setattr(paster_module, "_get_clipboard_text", lambda: "saved")
        monkeypatch.setattr(
            paster_module,
            "_set_clipboard_text",
            lambda text: events.append(("restore", text)) or True,
        )
        monkeypatch.setattr(
            paster_module.pyperclip,
            "copy",
            lambda text: events.append(("copy", text)),
        )
        monkeypatch.setattr(
            paster_module,
            "paste_hotkey",
            lambda: ("ctrl", "v"),
        )
        monkeypatch.setattr(
            paster_module.pyautogui,
            "hotkey",
            lambda mod, key: events.append(("hotkey", mod, key)),
        )
        monkeypatch.setattr(
            paster_module.pyautogui,
            "press",
            lambda key: events.append(("press", key)),
        )
        monkeypatch.setattr(
            paster_module.time,
            "sleep",
            lambda delay: events.append(("sleep", delay)),
        )
        return paster_module

    def test_paste_does_not_press_enter_by_default(self, monkeypatch):
        events = []
        paster_module = self._patch_paste_dependencies(monkeypatch, events)

        paster_module.paste("hello", OutputConfig(restore_clipboard=True))

        assert ("press", "enter") not in events
        assert events == [
            ("copy", "hello"),
            ("sleep", 0.1),
            ("hotkey", "ctrl", "v"),
            ("sleep", 0.1),
            ("sleep", 0.1),
            ("restore", "saved"),
        ]

    def test_paste_with_submit_presses_enter_before_restore(self, monkeypatch):
        events = []
        paster_module = self._patch_paste_dependencies(monkeypatch, events)

        paster_module.paste(
            "hello",
            OutputConfig(restore_clipboard=True),
            submit_after_paste=True,
        )

        assert events == [
            ("copy", "hello"),
            ("sleep", 0.1),
            ("hotkey", "ctrl", "v"),
            ("sleep", 0.1),
            ("press", "enter"),
            ("sleep", 0.1),
            ("restore", "saved"),
        ]

    def test_submit_does_not_press_enter_when_mac_paste_fallback_fails(
        self,
        monkeypatch,
    ):
        import src.platform.darwin as darwin_module

        events = []
        paster_module = self._patch_paste_dependencies(monkeypatch, events)
        monkeypatch.setattr(paster_module, "is_mac", lambda: True)
        monkeypatch.setattr(
            paster_module.pyautogui,
            "hotkey",
            lambda mod, key: (_ for _ in ()).throw(RuntimeError("paste failed")),
        )
        monkeypatch.setattr(
            darwin_module,
            "paste_via_applescript",
            lambda: events.append(("fallback_paste",)) or False,
        )

        paster_module.paste(
            "hello",
            OutputConfig(restore_clipboard=True),
            submit_after_paste=True,
        )

        assert ("fallback_paste",) in events
        assert ("press", "enter") not in events
        assert events[-1] == ("restore", "saved")

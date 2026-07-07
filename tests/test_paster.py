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
        monkeypatch.setattr(paster_module, "is_mac", lambda: False)
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

    def test_mac_cli_does_not_copy_auto_paste_or_submit(
        self,
        monkeypatch,
    ):
        events = []
        paster_module = self._patch_paste_dependencies(monkeypatch, events)
        monkeypatch.setattr(paster_module, "is_mac", lambda: True)
        notices = []

        paster_module.paste(
            "hello",
            OutputConfig(restore_clipboard=True),
            on_error=notices.append,
            submit_after_paste=True,
        )

        assert events == []
        assert notices == ["macOS CLI auto-paste/copy is disabled. Use the native menu bar app."]

    def test_paste_error_notifies_callback(self, monkeypatch):
        events = []
        paster_module = self._patch_paste_dependencies(monkeypatch, events)

        def fail_copy(text):
            raise RuntimeError("clipboard unavailable")

        monkeypatch.setattr(paster_module.pyperclip, "copy", fail_copy)
        notices = []

        paster_module.paste(
            "hello",
            OutputConfig(restore_clipboard=False),
            on_error=notices.append,
        )

        assert notices == ["ペースト処理中にエラーが発生しました: clipboard unavailable"]

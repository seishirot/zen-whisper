"""src.paster のテスト。"""

from __future__ import annotations

import sys

import pytest


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

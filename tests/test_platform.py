"""src.platform 抽象化レイヤーのテスト。"""

from __future__ import annotations

import sys

import pytest

from src.platform import is_mac, is_windows, paste_hotkey


class TestOSDetection:
    """OS 判定ユーティリティのテスト。"""

    def test_is_windows_returns_bool(self):
        assert isinstance(is_windows(), bool)

    def test_is_mac_returns_bool(self):
        assert isinstance(is_mac(), bool)

    def test_mutually_exclusive(self):
        """Windows と Mac が同時に True にならない。"""
        assert not (is_windows() and is_mac())

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_is_windows_on_windows(self):
        assert is_windows() is True
        assert is_mac() is False

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_is_mac_on_mac(self):
        assert is_mac() is True
        assert is_windows() is False


class TestPasteHotkey:
    """ペーストキーのテスト。"""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_paste_hotkey(self):
        assert paste_hotkey() == ("ctrl", "v")

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_mac_paste_hotkey(self):
        assert paste_hotkey() == ("command", "v")

    def test_returns_tuple_of_two_strings(self):
        result = paste_hotkey()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(s, str) for s in result)

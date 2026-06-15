"""src.hotkey のテスト。"""

from __future__ import annotations

import sys

import pytest

from src.hotkey import _parse_combo, _vk_from_key


class TestParseCombo:
    """ホットキー文字列パーサーのテスト。"""

    def test_simple_win_j(self):
        mods, key = _parse_combo("win+j")
        if sys.platform == "darwin":
            assert mods == {"cmd"}
        else:
            assert mods == {"win"}
        assert key == "j"

    def test_win_shift_j(self):
        mods, key = _parse_combo("win+shift+j")
        if sys.platform == "darwin":
            assert "cmd" in mods
        else:
            assert "win" in mods
        assert "shift" in mods
        assert key == "j"

    def test_ctrl_alt_x(self):
        mods, key = _parse_combo("ctrl+alt+x")
        assert mods == {"ctrl", "alt"}
        assert key == "x"

    def test_single_key(self):
        mods, key = _parse_combo("a")
        assert mods == set()
        assert key == "a"

    def test_case_insensitive(self):
        mods, key = _parse_combo("Win+Shift+J")
        assert key == "j"
        assert "shift" in mods

    def test_spaces_around_plus(self):
        mods, key = _parse_combo("win + shift + j")
        assert key == "j"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_cmd_j_on_mac(self):
        mods, key = _parse_combo("cmd+j")
        assert mods == {"cmd"}
        assert key == "j"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_cmd_becomes_win_on_windows(self):
        mods, key = _parse_combo("cmd+j")
        assert mods == {"win"}
        assert key == "j"


class TestVkFromKey:
    """仮想キーコード変換のテスト。"""

    def test_alpha_key(self):
        assert _vk_from_key("j") == ord("J")
        assert _vk_from_key("a") == ord("A")

    def test_special_keys(self):
        assert _vk_from_key("space") == 0x20
        assert _vk_from_key("enter") == 0x0D
        assert _vk_from_key("tab") == 0x09
        assert _vk_from_key("escape") == 0x1B
        assert _vk_from_key("esc") == 0x1B

    def test_unknown_key_returns_none(self):
        assert _vk_from_key("unknown") is None
        assert _vk_from_key("") is None
        assert _vk_from_key("f1") is None  # ファンクションキーは未対応

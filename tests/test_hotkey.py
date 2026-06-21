"""src.hotkey のテスト。"""

from __future__ import annotations

import sys

import pytest

from pynput import keyboard

import src.hotkey as hotkey_module
from src.hotkey import (
    _DarwinHotkeyListener,
    _WindowsHotkeyListener,
    _combo_list,
    _darwin_key_name,
    _parse_combo,
    _vk_from_key,
)


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

    def test_ctrl_shift_space(self):
        mods, key = _parse_combo("ctrl+shift+space")
        assert mods == {"ctrl", "shift"}
        assert key == "space"

    def test_single_key(self):
        mods, key = _parse_combo("a")
        assert mods == set()
        assert key == "a"

    def test_case_insensitive(self):
        mods, key = _parse_combo("Win+Shift+J")
        assert key == "j"
        assert "shift" in mods

    def test_esc_normalizes_to_escape(self):
        mods, key = _parse_combo("ctrl+esc")
        assert mods == {"ctrl"}
        assert key == "escape"

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


class TestComboList:
    """ホットキー設定値の正規化テスト。"""

    def test_string_returns_single_combo(self):
        assert _combo_list("shift+space") == ["shift+space"]

    def test_empty_string_returns_empty_list(self):
        assert _combo_list("") == []

    def test_list_filters_empty_entries(self):
        assert _combo_list(["shift+space", "", "win+j"]) == ["shift+space", "win+j"]


class TestWindowsModifierMatching:
    """Windows 修飾キー検出のテスト。"""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_ctrl_shift_requires_ctrl_and_shift(self, monkeypatch):
        import src.platform.windows as windows

        monkeypatch.setattr(windows, "is_win_down", lambda: False)
        monkeypatch.setattr(windows, "is_shift_down", lambda: True)
        monkeypatch.setattr(windows, "is_ctrl_down", lambda: True)
        monkeypatch.setattr(windows, "is_alt_down", lambda: False)

        listener = _WindowsHotkeyListener([])

        assert listener._check_modifiers({"ctrl", "shift"}) is True
        assert listener._check_modifiers({"shift"}) is False


class TestDarwinKeyName:
    """macOS キー名正規化のテスト。"""

    def test_special_keys_match_config_names(self):
        assert _darwin_key_name(keyboard.Key.space) == "space"
        assert _darwin_key_name(keyboard.Key.enter) == "enter"
        assert _darwin_key_name(keyboard.Key.tab) == "tab"
        assert _darwin_key_name(keyboard.Key.esc) == "escape"

    def test_listener_matches_escape_alias_after_config_normalization(self, monkeypatch):
        class ThreadStub:
            def __init__(self, target, daemon: bool) -> None:
                self.target = target
                self.daemon = daemon

            def start(self) -> None:
                self.target()

        monkeypatch.setattr(hotkey_module.threading, "Thread", ThreadStub)
        calls = []
        listener = _DarwinHotkeyListener([({"ctrl"}, "escape", lambda: calls.append("hit"))])
        listener._pressed_mods.add("ctrl")

        listener._on_press(keyboard.Key.esc)

        assert calls == ["hit"]

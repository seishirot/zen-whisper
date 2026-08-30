"""src.hotkey のテスト。"""

from __future__ import annotations

import sys

import pytest

from pynput import keyboard

import src.hotkey as hotkey_module
from src.hotkey import (
    _DarwinHotkeyListener,
    _combo_list,
    _darwin_key_name,
    _parse_combo,
    _vk_from_key,
    _windows_modifier_flags,
    validate_hotkey_config,
)
from src.windows_hotkey import MOD_CONTROL, MOD_SHIFT, MOD_WIN
from src.config import HotkeyConfig


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

    def test_control_alias_is_ctrl_not_bare_key(self):
        mods, key = _parse_combo("control+space")
        assert mods == {"ctrl"}
        assert key == "space"

    @pytest.mark.parametrize(
        "combo",
        [
            "ctrl+a+b",
            "ctrl+foo",
            "ctrl++a",
            "ctrl+ctrl+a",
        ],
    )
    def test_rejects_ambiguous_or_unsupported_combo(self, combo):
        with pytest.raises(ValueError):
            _parse_combo(combo)

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
        assert _vk_from_key("あ") is None
        assert _vk_from_key("ß") is None


class TestComboList:
    """ホットキー設定値の正規化テスト。"""

    def test_string_returns_single_combo(self):
        assert _combo_list("shift+space") == ["shift+space"]

    def test_empty_string_returns_empty_list(self):
        assert _combo_list("") == []

    def test_list_filters_empty_entries(self):
        assert _combo_list(["shift+space", "", "win+j"]) == ["shift+space", "win+j"]


def test_hotkey_validation_rejects_duplicate_actions():
    cfg = HotkeyConfig(
        toggle="ctrl+space",
        submit_toggle="control+space",
        switch_lang="alt+space",
    )

    errors = validate_hotkey_config(cfg)

    assert any("重複" in error for error in errors)


def test_hotkey_validation_allows_toggle_and_submit_lists():
    cfg = HotkeyConfig(
        toggle=["space", "win+a"],
        submit_toggle=["ctrl+enter", "alt+tab"],
        switch_lang="shift+escape",
    )

    assert validate_hotkey_config(cfg) == []


def test_hotkey_validation_allows_empty_optional_submit():
    cfg = HotkeyConfig(
        toggle="shift+space",
        submit_toggle="",
        switch_lang="alt+space",
    )

    assert validate_hotkey_config(cfg) == []


@pytest.mark.parametrize("field", ["toggle", "switch_lang"])
def test_hotkey_validation_rejects_empty_required_action(field):
    cfg = HotkeyConfig()
    setattr(cfg, field, "")

    assert any("空にできません" in error for error in validate_hotkey_config(cfg))


def test_hotkey_validation_rejects_duplicate_inside_one_list():
    cfg = HotkeyConfig(
        toggle=["ctrl+space", "control+space"],
        submit_toggle="",
        switch_lang="alt+space",
    )

    assert any("重複" in error for error in validate_hotkey_config(cfg))


def test_windows_modifier_flags_map_parsed_names():
    assert _windows_modifier_flags({"ctrl", "shift"}) == (
        MOD_CONTROL | MOD_SHIFT
    )
    assert _windows_modifier_flags({"win"}) == MOD_WIN


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

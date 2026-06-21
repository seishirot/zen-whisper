"""グローバルホットキーモジュール。

Windows: pynput Listener + win32_event_filter でキー抑制付き。
macOS:   pynput Listener + suppress=True (Quartz Event Tap) でキー抑制。
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable

from pynput import keyboard

from src.config import HotkeyConfig

logger = logging.getLogger(__name__)


def _normalize_key_name(key: str) -> str:
    if key == "esc":
        return "escape"
    return key


def _parse_combo(combo: str) -> tuple[set[str], str]:
    """
    "win+shift+j" のような文字列を (修飾キー集合, 通常キー) に分解する。
    Mac では "win" を "cmd" として解釈する。
    """
    parts = [p.strip().lower() for p in combo.split("+")]
    modifiers = set()
    key = ""
    for p in parts:
        # "win" は Mac では "cmd" として扱う
        if p == "win" and sys.platform == "darwin":
            modifiers.add("cmd")
        elif p == "cmd" and sys.platform == "win32":
            modifiers.add("win")
        elif p in ("win", "cmd", "shift", "ctrl", "alt"):
            modifiers.add(p)
        else:
            key = _normalize_key_name(p)
    return modifiers, key


_SPECIAL_KEY_MAP: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
}


def _vk_from_key(name: str) -> int | None:
    """キー名を仮想キーコードに変換する。アルファベット1文字または特殊キー名に対応。"""
    if name in _SPECIAL_KEY_MAP:
        return _SPECIAL_KEY_MAP[name]
    if len(name) == 1 and name.isalpha():
        return ord(name.upper())
    return None


def _combo_list(value: str | list[str]) -> list[str]:
    """設定値を登録用ホットキー配列に正規化する。"""
    if isinstance(value, list):
        return [combo for combo in value if combo]
    return [value] if value else []


# ══════════════════════════════════════════════════════
# Windows 実装
# ══════════════════════════════════════════════════════


class _WindowsHotkeyListener:
    """
    Windows: win32_event_filter 内でコンボ判定 → suppress → コールバックを別スレッドで発火。
    """

    def __init__(
        self,
        combos: list[tuple[set[str], int, Callable[[], None]]],
    ) -> None:
        self._combos = combos
        self._listener: keyboard.Listener | None = None

    def _check_modifiers(self, required: set[str]) -> bool:
        """GetAsyncKeyState で現在の修飾キー状態が required と一致するか。"""
        from src.platform.windows import (
            is_alt_down,
            is_ctrl_down,
            is_shift_down,
            is_win_down,
        )

        actual: set[str] = set()
        if is_win_down():
            actual.add("win")
        if is_shift_down():
            actual.add("shift")
        if is_ctrl_down():
            actual.add("ctrl")
        if is_alt_down():
            actual.add("alt")
        return actual == required

    def _win32_event_filter(self, msg: int, data: object) -> None:
        """低レベルキーボードフック。on_press より先に呼ばれる。"""
        if msg not in (0x0100, 0x0104):
            return

        try:
            vk = data.vkCode  # type: ignore[attr-defined]
        except Exception:
            logger.exception("win32_event_filter: vkCode 取得エラー")
            return

        for required_mods, target_vk, callback in self._combos:
            if vk == target_vk and self._check_modifiers(required_mods):
                threading.Thread(target=callback, daemon=True).start()
                if self._listener is not None:
                    self._listener.suppress_event()
                return

    def _on_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        pass

    def _on_release(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        pass

    def start(self) -> keyboard.Listener:
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            win32_event_filter=self._win32_event_filter,
        )
        self._listener.daemon = True
        self._listener.start()
        return self._listener


# ══════════════════════════════════════════════════════
# macOS 実装
# ══════════════════════════════════════════════════════


# pynput の Key オブジェクトから修飾キー名へのマッピング
_DARWIN_MOD_MAP: dict[keyboard.Key, str] = {
    keyboard.Key.cmd: "cmd",
    keyboard.Key.cmd_l: "cmd",
    keyboard.Key.cmd_r: "cmd",
    keyboard.Key.shift: "shift",
    keyboard.Key.shift_l: "shift",
    keyboard.Key.shift_r: "shift",
    keyboard.Key.ctrl: "ctrl",
    keyboard.Key.ctrl_l: "ctrl",
    keyboard.Key.ctrl_r: "ctrl",
    keyboard.Key.alt: "alt",
    keyboard.Key.alt_l: "alt",
    keyboard.Key.alt_r: "alt",
}


_DARWIN_SPECIAL_KEY_MAP: dict[keyboard.Key, str] = {
    keyboard.Key.space: "space",
    keyboard.Key.enter: "enter",
    keyboard.Key.tab: "tab",
    keyboard.Key.esc: "escape",
}


def _darwin_key_name(key: keyboard.Key | keyboard.KeyCode) -> str:
    """pynput のキーオブジェクトを設定文字列のキー名に変換する。"""
    if isinstance(key, keyboard.KeyCode) and key.char:
        return key.char.lower()
    return _DARWIN_SPECIAL_KEY_MAP.get(key, "")  # type: ignore[arg-type]


class _DarwinHotkeyListener:
    """
    macOS: pynput の suppress=True (Quartz Event Tap) でキーイベントを抑制。
    修飾キー状態は on_press/on_release で自前管理する。

    アクセシビリティ権限が必要。
    """

    def __init__(
        self,
        combos: list[tuple[set[str], str, Callable[[], None]]],
    ) -> None:
        # combos: (required_mods, key_char, callback)
        self._combos = combos
        self._pressed_mods: set[str] = set()
        self._listener: keyboard.Listener | None = None

    def _on_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key is None:
            return

        # 修飾キーの状態を追跡
        mod_name = _DARWIN_MOD_MAP.get(key)  # type: ignore[arg-type]
        if mod_name:
            self._pressed_mods.add(mod_name)
            return

        key_name = _darwin_key_name(key)
        if not key_name:
            return

        # コンボ判定
        for required_mods, target_key, callback in self._combos:
            if key_name == target_key and self._pressed_mods == required_mods:
                threading.Thread(target=callback, daemon=True).start()
                # suppress=True で動作しているため、このキーは抑制される
                return

    def _on_release(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key is None:
            return
        mod_name = _DARWIN_MOD_MAP.get(key)  # type: ignore[arg-type]
        if mod_name:
            self._pressed_mods.discard(mod_name)

    def start(self) -> keyboard.Listener:
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=True,  # Quartz Event Tap でグローバルにキー抑制
        )
        self._listener.daemon = True
        self._listener.start()
        return self._listener


# ══════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════


def start_hotkey_listener(
    cfg: HotkeyConfig,
    on_toggle: Callable[[], None],
    on_switch_lang: Callable[[], None],
    on_submit_toggle: Callable[[], None] | None = None,
) -> keyboard.Listener:
    """グローバルホットキーリスナーを起動する（デーモンスレッド、キー入力抑制付き）。"""
    toggle_strs = _combo_list(cfg.toggle)
    submit_toggle_strs = _combo_list(cfg.submit_toggle)

    if sys.platform == "darwin":
        return _start_darwin_listener(
            toggle_strs,
            submit_toggle_strs,
            cfg.switch_lang,
            on_toggle,
            on_switch_lang,
            on_submit_toggle,
        )
    else:
        return _start_windows_listener(
            toggle_strs,
            submit_toggle_strs,
            cfg.switch_lang,
            on_toggle,
            on_switch_lang,
            on_submit_toggle,
        )


def _start_windows_listener(
    toggle_strs: list[str],
    submit_toggle_strs: list[str],
    switch_lang_str: str,
    on_toggle: Callable[[], None],
    on_switch_lang: Callable[[], None],
    on_submit_toggle: Callable[[], None] | None = None,
) -> keyboard.Listener:
    """Windows 用ホットキーリスナーを起動する。"""
    combos: list[tuple[set[str], int, Callable[[], None]]] = []

    for combo_str in toggle_strs:
        mods, key_str = _parse_combo(combo_str)
        vk = _vk_from_key(key_str)
        if vk is None:
            raise ValueError(f"ホットキーの解析に失敗: {combo_str}")
        combos.append((mods, vk, on_toggle))

    if on_submit_toggle is not None:
        for combo_str in submit_toggle_strs:
            mods, key_str = _parse_combo(combo_str)
            vk = _vk_from_key(key_str)
            if vk is None:
                raise ValueError(f"ホットキーの解析に失敗: submit_toggle={combo_str}")
            combos.append((mods, vk, on_submit_toggle))

    switch_mods, switch_key_str = _parse_combo(switch_lang_str)
    switch_vk = _vk_from_key(switch_key_str)
    if switch_vk is None:
        raise ValueError(f"ホットキーの解析に失敗: switch={switch_lang_str}")
    combos.append((switch_mods, switch_vk, on_switch_lang))

    logger.info(
        "ホットキー登録 (Windows): toggle=%s, submit_toggle=%s, switch_lang=%s",
        toggle_strs,
        submit_toggle_strs,
        switch_lang_str,
    )

    handler = _WindowsHotkeyListener(combos)
    return handler.start()


def _start_darwin_listener(
    toggle_strs: list[str],
    submit_toggle_strs: list[str],
    switch_lang_str: str,
    on_toggle: Callable[[], None],
    on_switch_lang: Callable[[], None],
    on_submit_toggle: Callable[[], None] | None = None,
) -> keyboard.Listener:
    """macOS 用ホットキーリスナーを起動する。"""
    combos: list[tuple[set[str], str, Callable[[], None]]] = []

    for combo_str in toggle_strs:
        mods, key_str = _parse_combo(combo_str)
        if not key_str:
            raise ValueError(f"ホットキーの解析に失敗: {combo_str}")
        combos.append((mods, key_str, on_toggle))

    if on_submit_toggle is not None:
        for combo_str in submit_toggle_strs:
            mods, key_str = _parse_combo(combo_str)
            if not key_str:
                raise ValueError(f"ホットキーの解析に失敗: submit_toggle={combo_str}")
            combos.append((mods, key_str, on_submit_toggle))

    switch_mods, switch_key_str = _parse_combo(switch_lang_str)
    if not switch_key_str:
        raise ValueError(f"ホットキーの解析に失敗: switch={switch_lang_str}")
    combos.append((switch_mods, switch_key_str, on_switch_lang))

    logger.info(
        "ホットキー登録 (macOS): toggle=%s, submit_toggle=%s, switch_lang=%s",
        toggle_strs,
        submit_toggle_strs,
        switch_lang_str,
    )

    handler = _DarwinHotkeyListener(combos)
    return handler.start()

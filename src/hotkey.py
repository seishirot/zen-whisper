"""グローバルホットキーモジュール。

Windows: Win32 RegisterHotKey + 専用メッセージループ。
macOS:   pynput Listener + suppress=True (Quartz Event Tap) でキー抑制。
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from typing import Protocol

from pynput import keyboard

from src.config import HotkeyConfig
from src.windows_hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    HotkeyRuntimeError,
    HotkeyRuntimeStatus,
    WindowsHotkeyBinding,
    WindowsHotkeyController,
    WindowsHotkeyTransaction,
)

logger = logging.getLogger(__name__)


def _normalize_key_name(key: str) -> str:
    if key == "esc":
        return "escape"
    return key


_MODIFIER_ALIASES = {
    "control": "ctrl",
    "option": "alt",
    "windows": "win",
    "command": "cmd",
}


def _parse_combo(combo: str) -> tuple[set[str], str]:
    """
    "win+shift+j" のような文字列を (修飾キー集合, 通常キー) に分解する。
    Mac では "win" を "cmd" として解釈する。
    """
    if not isinstance(combo, str) or not combo.strip():
        raise ValueError("ホットキーは空にできません")
    parts = [part.strip().lower() for part in combo.split("+")]
    if any(not part for part in parts):
        raise ValueError(f"ホットキーの区切りが不正です: {combo}")

    modifiers: set[str] = set()
    keys: list[str] = []
    for raw_part in parts:
        part = _MODIFIER_ALIASES.get(raw_part, raw_part)
        # "win" は Mac では "cmd" として扱う
        if part == "win" and sys.platform == "darwin":
            modifier = "cmd"
        elif part == "cmd" and sys.platform == "win32":
            modifier = "win"
        elif part in ("win", "cmd", "shift", "ctrl", "alt"):
            modifier = part
        else:
            key = _normalize_key_name(part)
            if _vk_from_key(key) is None:
                raise ValueError(f"未対応のキーです: {raw_part}")
            keys.append(key)
            continue
        if modifier in modifiers:
            raise ValueError(f"修飾キーが重複しています: {raw_part}")
        modifiers.add(modifier)

    if len(keys) != 1:
        raise ValueError(
            "ホットキーには通常キーをちょうど1つ指定してください"
        )
    return modifiers, keys[0]


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
    if len(name) == 1 and name.isascii() and name.isalpha():
        return ord(name.upper())
    return None


def _combo_list(value: str | list[str]) -> list[str]:
    """設定値を登録用ホットキー配列に正規化する。"""
    if isinstance(value, list):
        return [combo for combo in value if combo]
    return [value] if value else []


def validate_hotkey_config(cfg: HotkeyConfig) -> list[str]:
    """Validate syntax and reject overlapping actions without hooks."""
    errors: list[str] = []
    entries: list[tuple[str, str]] = []
    for label, value, required, allow_list in (
        ("録音トグル", cfg.toggle, True, True),
        ("貼り付け＋Enter", cfg.submit_toggle, False, True),
        ("言語切替", cfg.switch_lang, True, False),
    ):
        if isinstance(value, str):
            combos = [value] if value else []
        elif allow_list and isinstance(value, list) and all(
            isinstance(item, str) for item in value
        ):
            combos = [item for item in value if item]
        else:
            errors.append(f"{label}の型が不正です")
            continue
        if required and not combos:
            errors.append(f"{label}は空にできません")
            continue
        entries.extend((label, combo) for combo in combos)

    seen: dict[tuple[frozenset[str], str], str] = {}
    for label, combo in entries:
        try:
            modifiers, key = _parse_combo(combo)
        except ValueError as exc:
            errors.append(f"{label}「{combo}」: {exc}")
            continue
        fingerprint = (frozenset(modifiers), key)
        previous = seen.get(fingerprint)
        if previous is not None:
            errors.append(
                f"{label}「{combo}」は{previous}と重複しています"
            )
        else:
            seen[fingerprint] = label
    return errors


# ══════════════════════════════════════════════════════
# Windows 実装
# ══════════════════════════════════════════════════════


_WINDOWS_MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}


def _windows_modifier_flags(modifiers: set[str]) -> int:
    flags = 0
    for modifier in modifiers:
        try:
            flags |= _WINDOWS_MODIFIERS[modifier]
        except KeyError as exc:
            raise ValueError(
                f"Windowsで未対応の修飾キーです: {modifier}"
            ) from exc
    return flags


class HotkeyHandle(Protocol):
    """Lifecycle shared by the Windows and macOS implementations."""

    supports_live_reconfigure: bool

    def status(self) -> HotkeyRuntimeStatus: ...

    def set_runtime_error_callback(
        self,
        callback: Callable[[HotkeyRuntimeStatus], None] | None,
    ) -> None: ...

    def prepare_reconfigure(
        self,
        cfg: HotkeyConfig,
    ) -> WindowsHotkeyTransaction: ...

    def recover(self, cfg: HotkeyConfig) -> HotkeyRuntimeStatus: ...

    def stop(self) -> bool: ...


class _WindowsHotkeyManager:
    supports_live_reconfigure = True

    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_switch_lang: Callable[[], None],
        on_submit_toggle: Callable[[], None] | None,
        *,
        controller: WindowsHotkeyController | None = None,
    ) -> None:
        self._callbacks = {
            "toggle": on_toggle,
            "switch_lang": on_switch_lang,
        }
        if on_submit_toggle is not None:
            self._callbacks["submit_toggle"] = on_submit_toggle
        self._controller = controller or WindowsHotkeyController()

    def _bindings(
        self,
        cfg: HotkeyConfig,
    ) -> tuple[WindowsHotkeyBinding, ...]:
        bindings: list[WindowsHotkeyBinding] = []
        entries = (
            ("toggle", _combo_list(cfg.toggle)),
            ("submit_toggle", _combo_list(cfg.submit_toggle)),
            ("switch_lang", [cfg.switch_lang]),
        )
        for action, combos in entries:
            callback = self._callbacks.get(action)
            if callback is None:
                continue
            for combo in combos:
                modifiers, key_name = _parse_combo(combo)
                virtual_key = _vk_from_key(key_name)
                if virtual_key is None:
                    raise ValueError(f"ホットキーの解析に失敗: {combo}")
                bindings.append(
                    WindowsHotkeyBinding(
                        combo=combo,
                        modifiers=_windows_modifier_flags(modifiers),
                        virtual_key=virtual_key,
                        action=action,
                        callback=callback,
                    )
                )
        return tuple(bindings)

    def start(self, cfg: HotkeyConfig) -> HotkeyRuntimeStatus:
        return self._controller.start(self._bindings(cfg))

    def status(self) -> HotkeyRuntimeStatus:
        return self._controller.status()

    def set_runtime_error_callback(
        self,
        callback: Callable[[HotkeyRuntimeStatus], None] | None,
    ) -> None:
        self._controller.set_runtime_error_callback(callback)

    def prepare_reconfigure(
        self,
        cfg: HotkeyConfig,
    ) -> WindowsHotkeyTransaction:
        return self._controller.prepare_reconfigure(self._bindings(cfg))

    def recover(self, cfg: HotkeyConfig) -> HotkeyRuntimeStatus:
        return self._controller.recover(self._bindings(cfg))

    def stop(self) -> bool:
        return self._controller.stop()


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


class _PynputHotkeyHandle:
    supports_live_reconfigure = False

    def __init__(self, listener: keyboard.Listener) -> None:
        self._listener = listener

    def status(self) -> HotkeyRuntimeStatus:
        healthy = self._listener.is_alive()
        return HotkeyRuntimeStatus(
            healthy,
            "macOSホットキーは有効です"
            if healthy
            else "macOSホットキーリスナーが停止しています",
        )

    def set_runtime_error_callback(
        self,
        callback: Callable[[HotkeyRuntimeStatus], None] | None,
    ) -> None:
        del callback

    def prepare_reconfigure(
        self,
        cfg: HotkeyConfig,
    ) -> WindowsHotkeyTransaction:
        del cfg
        raise HotkeyRuntimeError(
            "macOSのホットキー変更は再起動後に反映されます"
        )

    def recover(self, cfg: HotkeyConfig) -> HotkeyRuntimeStatus:
        del cfg
        return self.status()

    def stop(self) -> bool:
        self._listener.stop()
        self._listener.join(timeout=2.0)
        return not self._listener.is_alive()


# ══════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════


def start_hotkey_listener(
    cfg: HotkeyConfig,
    on_toggle: Callable[[], None],
    on_switch_lang: Callable[[], None],
    on_submit_toggle: Callable[[], None] | None = None,
) -> HotkeyHandle:
    """グローバルホットキーを起動し、解除可能なhandleを返す。"""
    validation_errors = validate_hotkey_config(cfg)
    if validation_errors:
        raise ValueError(" / ".join(validation_errors))
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
) -> HotkeyHandle:
    """Windows用RegisterHotKey controllerを起動する。"""
    cfg = HotkeyConfig(
        toggle=toggle_strs,
        submit_toggle=submit_toggle_strs,
        switch_lang=switch_lang_str,
    )
    manager = _WindowsHotkeyManager(
        on_toggle,
        on_switch_lang,
        on_submit_toggle,
    )
    status = manager.start(cfg)

    logger.info(
        "ホットキー登録 (Windows): toggle=%s, submit_toggle=%s, switch_lang=%s",
        toggle_strs,
        submit_toggle_strs,
        switch_lang_str,
    )

    if not status.healthy:
        logger.error("Windowsホットキー登録失敗: %s", status.message)
    return manager


def _start_darwin_listener(
    toggle_strs: list[str],
    submit_toggle_strs: list[str],
    switch_lang_str: str,
    on_toggle: Callable[[], None],
    on_switch_lang: Callable[[], None],
    on_submit_toggle: Callable[[], None] | None = None,
) -> HotkeyHandle:
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
    return _PynputHotkeyHandle(handler.start())

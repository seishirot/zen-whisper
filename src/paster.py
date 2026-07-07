"""クリップボード経由ペーストモジュール。

Windows CLI では退避→コピー→ペースト→任意 Enter→復元を行う。
macOS CLI では自動コピー/ペーストを行わず、native menu bar app に委譲する。
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable

import pyautogui
import pyperclip

from src.config import OutputConfig
from src.platform import is_mac, paste_hotkey

logger = logging.getLogger(__name__)

# pyautogui のフェイルセーフを無効化（画面端で例外を投げない）
pyautogui.FAILSAFE = False


# ── プラットフォーム別クリップボード操作 ───────────────

if sys.platform == "win32":
    from src.platform.windows import get_clipboard_text as _get_clipboard_text
    from src.platform.windows import set_clipboard_text as _set_clipboard_text
else:
    from src.platform.darwin import get_clipboard_text as _get_clipboard_text
    from src.platform.darwin import set_clipboard_text as _set_clipboard_text


def _press_enter() -> None:
    try:
        pyautogui.press("enter")
    except Exception:
        if is_mac():
            logger.debug("pyautogui Enter 送信失敗、AppleScript にフォールバック")
            from src.platform.darwin import press_enter_via_applescript

            if press_enter_via_applescript():
                return
        raise


def paste(
    text: str,
    cfg: OutputConfig,
    on_error: Callable[[str], None] | None = None,
    submit_after_paste: bool = False,
) -> None:
    """
    テキストをアクティブウィンドウにペーストする。

    macOS CLI では安全な自動ペースト対象判定を native menu bar app に
    集約しているため、コピーもペーストも行わずエラー通知だけ返す。

    1. クリップボード退避
    2. テキストをコピー
    3. Ctrl+V でペースト
    4. 必要なら Enter 送信
    5. クリップボード復元
    """
    saved_text: str | None = None
    delay_sec = cfg.paste_delay_ms / 1000.0

    try:
        if is_mac():
            logger.warning(
                "macOS CLI auto-paste is disabled; %d characters were not copied. "
                "Use the native menu bar app for safe automatic paste.",
                len(text),
            )
            if submit_after_paste:
                logger.warning("macOS CLI submit-after-paste is disabled")
            if on_error:
                on_error("macOS CLI auto-paste/copy is disabled. Use the native menu bar app.")
            return

        # 1. 退避
        if cfg.restore_clipboard:
            saved_text = _get_clipboard_text()

        # 2. コピー
        pyperclip.copy(text)
        time.sleep(delay_sec)

        # 3. ペースト
        mod, key = paste_hotkey()
        try:
            pyautogui.hotkey(mod, key)
        except Exception:
            # Mac では pyautogui が失敗する場合がある → AppleScript フォールバック
            if is_mac():
                logger.debug("pyautogui ペースト失敗、AppleScript にフォールバック")
                from src.platform.darwin import paste_via_applescript

                if not paste_via_applescript():
                    raise RuntimeError("AppleScript によるペーストに失敗しました")
            else:
                raise
        time.sleep(delay_sec)

        if submit_after_paste:
            _press_enter()
            logger.info("ペースト後に Enter を送信しました")

        logger.info("ペースト完了: %d文字", len(text))

    except Exception as exc:
        logger.exception("ペースト処理中にエラーが発生しました")
        if on_error:
            on_error(f"ペースト処理中にエラーが発生しました: {exc}")

    finally:
        # 5. 復元
        if cfg.restore_clipboard and saved_text is not None:
            time.sleep(delay_sec)
            if _set_clipboard_text(saved_text):
                logger.debug("クリップボードを復元しました")
            else:
                logger.warning("クリップボードの復元に失敗しました")
                if on_error:
                    on_error("クリップボードの復元に失敗しました")

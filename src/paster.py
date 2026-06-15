"""クリップボード経由ペーストモジュール。退避→コピー→ペースト→復元を行う。"""

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


def paste(text: str, cfg: OutputConfig, on_error: Callable[[str], None] | None = None) -> None:
    """
    テキストをアクティブウィンドウにペーストする。

    1. クリップボード退避
    2. テキストをコピー
    3. Ctrl+V (Windows) / Cmd+V (Mac) でペースト
    4. クリップボード復元
    """
    saved_text: str | None = None
    delay_sec = cfg.paste_delay_ms / 1000.0

    try:
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
                paste_via_applescript()
            else:
                raise
        time.sleep(delay_sec)

        logger.info("ペースト完了: %d文字", len(text))

    except Exception:
        logger.exception("ペースト処理中にエラーが発生しました")

    finally:
        # 4. 復元
        if cfg.restore_clipboard and saved_text is not None:
            time.sleep(delay_sec)
            if _set_clipboard_text(saved_text):
                logger.debug("クリップボードを復元しました")
            else:
                logger.warning("クリップボードの復元に失敗しました")
                if on_error:
                    on_error("クリップボードの復元に失敗しました")

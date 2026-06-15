"""スタートアップ自動起動モジュール。プラットフォームに応じた実装に委譲する。

Windows: レジストリ (HKCU\\...\\Run)
macOS:   LaunchAgents plist (~/.Library/LaunchAgents/)
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# プラットフォーム別の実装をインポート
if sys.platform == "win32":
    from src.platform.windows import (
        is_startup_registered as is_registered,
        register_startup as register,
        unregister_startup as unregister,
    )
elif sys.platform == "darwin":
    from src.platform.darwin import (
        is_startup_registered as is_registered,
        register_startup as register,
        unregister_startup as unregister,
    )
else:
    # 未サポートプラットフォーム — スタブ実装
    def is_registered() -> bool:
        logger.warning("スタートアップ登録はこのプラットフォームではサポートされていません")
        return False

    def register() -> None:
        logger.warning("スタートアップ登録はこのプラットフォームではサポートされていません")

    def unregister() -> None:
        logger.warning("スタートアップ解除はこのプラットフォームではサポートされていません")


def toggle() -> bool:
    """スタートアップ登録をトグルし、新しい状態（登録済み=True）を返す。"""
    if is_registered():
        unregister()
        return False
    else:
        register()
        return True

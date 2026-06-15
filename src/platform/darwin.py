"""macOS (Darwin) 固有の実装。PyObjC, pyperclip, plistlib 等を使用。"""

from __future__ import annotations

import logging
import plistlib
import subprocess
import tkinter as tk
from pathlib import Path

import pyperclip

logger = logging.getLogger(__name__)


# ── クリップボード ─────────────────────────────────────


def get_clipboard_text() -> str | None:
    """現在のクリップボードのテキストを取得する。"""
    try:
        text = pyperclip.paste()
        return text if text else None
    except Exception:
        logger.warning("クリップボードの読み取りに失敗しました", exc_info=True)
        return None


def set_clipboard_text(text: str) -> bool:
    """クリップボードにテキストを設定する。"""
    try:
        pyperclip.copy(text)
        return True
    except Exception:
        logger.warning("クリップボードへの書き込みに失敗しました", exc_info=True)
        return False


# ── スタートアップ登録（LaunchAgents）─────────────────

_APP_NAME = "com.zen-whisper"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_APP_NAME}.plist"


def _get_launch_command() -> list[str]:
    """LaunchAgent 用の起動コマンドを構築する。"""
    # uv run zen-whisper で起動
    # uv のパスを探す
    import shutil

    uv_path = shutil.which("uv")
    if uv_path is None:
        # デフォルトパスを試す
        uv_path = str(Path.home() / ".local" / "bin" / "uv")

    project_dir = Path(__file__).resolve().parent.parent.parent
    return [uv_path, "run", "--project", str(project_dir), "zen-whisper"]


def is_startup_registered() -> bool:
    """スタートアップに登録されているかを返す。"""
    return _PLIST_PATH.exists()


def register_startup() -> None:
    """LaunchAgents に plist を作成してスタートアップ登録する。"""
    try:
        cmd = _get_launch_command()
        project_dir = Path(__file__).resolve().parent.parent.parent

        plist_data = {
            "Label": _APP_NAME,
            "ProgramArguments": cmd,
            "WorkingDirectory": str(project_dir),
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": str(project_dir / "zen-whisper-stdout.log"),
            "StandardErrorPath": str(project_dir / "zen-whisper-stderr.log"),
        }

        _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_PLIST_PATH, "wb") as f:
            plistlib.dump(plist_data, f)

        subprocess.run(["launchctl", "load", str(_PLIST_PATH)], check=True)
        logger.info("スタートアップに登録しました: %s", _PLIST_PATH)
    except Exception:
        logger.exception("スタートアップの登録に失敗しました")


def unregister_startup() -> None:
    """LaunchAgents から plist を削除してスタートアップ解除する。"""
    try:
        if _PLIST_PATH.exists():
            subprocess.run(["launchctl", "unload", str(_PLIST_PATH)], check=False)
            _PLIST_PATH.unlink()
            logger.info("スタートアップから解除しました")
        else:
            logger.debug("スタートアップに登録されていません")
    except Exception:
        logger.exception("スタートアップの解除に失敗しました")


# ── オーバーレイ（NSWindow 属性）──────────────────────


def get_active_monitor_rect() -> tuple[int, int, int, int]:
    """メインスクリーンのワークエリア (left, top, right, bottom) を返す。"""
    try:
        from AppKit import NSScreen

        screen = NSScreen.mainScreen()
        if screen is None:
            raise RuntimeError("NSScreen.mainScreen() returned None")

        # visibleFrame はメニューバーと Dock を除いたエリア
        frame = screen.visibleFrame()
        full_frame = screen.frame()

        # macOS の座標系は左下原点だが、tkinter は左上原点
        left = int(frame.origin.x)
        # top = 画面高さ - (visibleFrame.origin.y + visibleFrame.size.height)
        top = int(full_frame.size.height - (frame.origin.y + frame.size.height))
        right = int(frame.origin.x + frame.size.width)
        bottom = int(full_frame.size.height - frame.origin.y)
        return (left, top, right, bottom)
    except Exception:
        logger.debug("モニター情報の取得に失敗、フォールバック", exc_info=True)
        return (0, 0, 1920, 1080)


def setup_overlay_window(root: tk.Tk) -> None:
    """tkinter ウィンドウに macOS 固有の属性を設定する。

    - フォーカス奪取防止 (setCanBecomeKeyWindow_)
    - クリックスルー (setIgnoresMouseEvents_)
    - 常に最前面 (NSFloatingWindowLevel)
    """
    try:
        from AppKit import NSApp, NSFloatingWindowLevel

        root.update_idletasks()

        # tkinter の内部 NSWindow を取得
        # macOS の tk は winfo_id() で NSView のポインタを返すことがある
        # NSApp.windows() から探す方が確実
        nswindow = None
        for window in NSApp.windows():
            # tkinter のウィンドウタイトルやプロパティで識別
            # 最後に作成されたウィンドウが対象のことが多い
            nswindow = window

        if nswindow is None:
            logger.warning("NSWindow の取得に失敗しました")
            return

        nswindow.setLevel_(NSFloatingWindowLevel)
        nswindow.setCanBecomeKeyWindow_(False)
        nswindow.setIgnoresMouseEvents_(True)

        logger.debug("macOS オーバーレイウィンドウ属性を設定しました")
    except ImportError:
        logger.warning("AppKit が利用できません。オーバーレイのウィンドウ属性設定をスキップします")
    except Exception:
        logger.exception("macOS オーバーレイウィンドウ属性の設定に失敗しました")


def setup_overlay_event_monitor(root: tk.Tk) -> None:
    """Cmd キー押下中のみオーバーレイをクリック可能にする（オプション機能）。"""
    try:
        from AppKit import NSApp, NSFloatingWindowLevel
        from Cocoa import NSEventMaskFlagsChanged, NSEventModifierFlagCommand

        nswindow = None
        for window in NSApp.windows():
            nswindow = window

        if nswindow is None:
            return

        from AppKit import NSEvent

        def on_flags_changed(event):
            is_cmd_down = bool(event.modifierFlags() & NSEventModifierFlagCommand)
            nswindow.setIgnoresMouseEvents_(not is_cmd_down)
            nswindow.setAlphaValue_(0.9 if is_cmd_down else 0.6)
            return event

        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, on_flags_changed
        )
        NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, on_flags_changed
        )
        logger.debug("Cmd キートグルイベントモニターを設定しました")
    except ImportError:
        logger.debug("AppKit/Cocoa が利用できません。イベントモニター設定をスキップします")
    except Exception:
        logger.debug("イベントモニターの設定に失敗しました", exc_info=True)


# ── マイク権限チェック ─────────────────────────────────


def check_microphone_permission() -> bool:
    """macOS でマイク入力が利用可能かチェックする。"""
    try:
        import numpy as np
        import sounddevice as sd

        default_input = sd.query_devices(kind="input")
        if default_input["max_input_channels"] < 1:
            return False

        # 短い録音テスト（0.1秒）で実データが取れるか確認
        test_audio = sd.rec(
            int(0.1 * 16000), samplerate=16000, channels=1, dtype="float32"
        )
        sd.wait()

        # 完全無音（全ゼロ）なら権限問題の可能性
        if np.max(np.abs(test_audio)) < 1e-7:
            return False

        return True
    except Exception:
        logger.debug("マイク権限チェックでエラーが発生しました", exc_info=True)
        return False


# ── AppleScript フォールバックペースト ─────────────────


def paste_via_applescript() -> bool:
    """AppleScript 経由で Cmd+V ペーストを実行する。フォールバック用。"""
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to keystroke "v" using command down',
            ],
            check=True,
            timeout=5,
        )
        return True
    except Exception:
        logger.warning("AppleScript によるペーストに失敗しました", exc_info=True)
        return False

"""macOS (Darwin) 固有の実装。PyObjC, pyperclip, plistlib 等を使用。"""

from __future__ import annotations

import logging
import plistlib
import json
import hashlib
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
_NATIVE_APP_PATH = Path("/Applications/zen-whisper.app")
_NATIVE_BUNDLE_ID = "com.seishirot.zenwhisper"
_SIGNING_JSON = Path.home() / "Library" / "Application Support" / "zen-whisper" / "install" / "signing.json"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_APP_NAME}.plist"


def _get_launch_command() -> list[str]:
    """LaunchAgent 用の起動コマンドを構築する。"""
    return ["/usr/bin/open", str(_NATIVE_APP_PATH)]


def _native_app_is_installed() -> bool:
    plist_path = _NATIVE_APP_PATH / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return False
    try:
        with plist_path.open("rb") as f:
            data = plistlib.load(f)
        if data.get("CFBundleIdentifier") != _NATIVE_BUNDLE_ID:
            return False
    except Exception:
        logger.warning("native macOS app の Info.plist を確認できませんでした", exc_info=True)
        return False
    return _native_app_signature_is_valid() and _native_app_matches_signing_baseline()


def _native_app_signature_is_valid() -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(_NATIVE_APP_PATH)],
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        logger.warning("native macOS app の署名検証に失敗しました", exc_info=True)
        return False
    if result.returncode == 0:
        return True
    output = f"{result.stdout}\n{result.stderr}"
    return _is_local_trust_only_codesign_failure(output)


def _is_local_trust_only_codesign_failure(output: str) -> bool:
    if "CSSMERR_TP_NOT_TRUSTED" not in output:
        return False
    lowered = output.lower()
    integrity_terms = (
        "bundle format",
        "code object is not signed",
        "invalid",
        "main executable failed",
        "modified",
        "not signed",
        "rejected",
        "resource envelope",
        "sealed resource",
        "unsealed",
    )
    return not any(term in lowered for term in integrity_terms)


def _native_app_matches_signing_baseline() -> bool:
    try:
        baseline = json.loads(_SIGNING_JSON.read_text(encoding="utf-8"))
        if baseline.get("app_path") != str(_NATIVE_APP_PATH):
            return False
        expected_hash = baseline.get("executable_sha256")
        if not isinstance(expected_hash, str) or not expected_hash:
            return False
        executable = _NATIVE_APP_PATH / "Contents" / "MacOS" / "zen-whisper"
        if not executable.is_file() or _sha256_hex(executable) != expected_hash:
            return False
        expected_requirement = baseline.get("designated_requirement")
        if not isinstance(expected_requirement, str) or not expected_requirement:
            return False
        result = subprocess.run(
            ["/usr/bin/codesign", "-dr", "-", str(_NATIVE_APP_PATH)],
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        logger.warning("native macOS app の署名 baseline を確認できませんでした", exc_info=True)
        return False
    output = f"{result.stdout}\n{result.stderr}"
    prefix = "designated => "
    actual = ""
    for line in output.splitlines():
        if prefix in line:
            actual = line.split(prefix, 1)[1].strip()
            break
    return actual == expected_requirement


def _sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_startup_registered() -> bool:
    """スタートアップに登録されているかを返す。"""
    return _PLIST_PATH.exists()


def register_startup() -> bool:
    """LaunchAgents に plist を作成してスタートアップ登録する。"""
    try:
        if not _native_app_is_installed():
            logger.warning("native macOS app が未インストールのためスタートアップ登録を中止します: %s", _NATIVE_APP_PATH)
            return False
        cmd = _get_launch_command()

        plist_data = {
            "Label": _APP_NAME,
            "ProgramArguments": cmd,
            "WorkingDirectory": "/Applications",
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": str(Path.home() / "Library/Logs/zen-whisper/legacy-startup.stdout.log"),
            "StandardErrorPath": str(Path.home() / "Library/Logs/zen-whisper/legacy-startup.stderr.log"),
        }

        _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        staged_plist = _PLIST_PATH.with_suffix(".plist.staging")
        with open(staged_plist, "wb") as f:
            plistlib.dump(plist_data, f)
        staged_plist.replace(_PLIST_PATH)

        try:
            subprocess.run(["/bin/launchctl", "load", str(_PLIST_PATH)], check=True)
        except Exception:
            _PLIST_PATH.unlink(missing_ok=True)
            raise
        logger.info("スタートアップに登録しました: %s", _PLIST_PATH)
        return True
    except Exception:
        logger.exception("スタートアップの登録に失敗しました")
        return False


def unregister_startup() -> bool:
    """LaunchAgents から plist を削除してスタートアップ解除する。"""
    try:
        if _PLIST_PATH.exists():
            subprocess.run(["/bin/launchctl", "unload", str(_PLIST_PATH)], check=False)
            _PLIST_PATH.unlink()
            logger.info("スタートアップから解除しました")
            return True
        else:
            logger.debug("スタートアップに登録されていません")
            return True
    except Exception:
        logger.exception("スタートアップの解除に失敗しました")
        return False


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
                "/usr/bin/osascript",
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


def press_enter_via_applescript() -> bool:
    """AppleScript 経由で Enter キーを送信する。フォールバック用。"""
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'tell application "System Events" to key code 36',
            ],
            check=True,
            timeout=5,
        )
        return True
    except Exception:
        logger.warning("AppleScript による Enter 送信に失敗しました", exc_info=True)
        return False

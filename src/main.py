"""zen-whisper エントリポイント。全コンポーネントを統合し、システムトレイアプリとして動作する。"""

from __future__ import annotations

import os
import sys

# pythonw.exe (GUI モード) では sys.stdout/stderr が None になる。
# torch.hub 等が sys.stderr.write() を呼ぶためクラッシュを防止する。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# pip の nvidia-* パッケージに含まれる CUDA DLL を検索パスに追加する (Windows)
# os.add_dll_directory() だけでは ctranslate2 が DLL を見つけられないため PATH にも追加する
if sys.platform == "win32":
    _nvidia_dir = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    if os.path.isdir(_nvidia_dir):
        _dll_dirs = []
        for _pkg in os.listdir(_nvidia_dir):
            _bin_dir = os.path.join(_nvidia_dir, _pkg, "bin")
            if os.path.isdir(_bin_dir):
                os.add_dll_directory(_bin_dir)
                _dll_dirs.append(_bin_dir)
        if _dll_dirs:
            os.environ["PATH"] = os.pathsep.join(_dll_dirs) + os.pathsep + os.environ.get("PATH", "")

import atexit
import logging
import threading
from pathlib import Path

# src パッケージをインポート可能にする
_ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT_DIR))

from src.config import (
    ENGINE_QWEN3_ASR,
    ENGINE_WHISPER,
    AppConfig,
    load_config,
    save_config,
)
from src.hotkey import start_hotkey_listener
from src.overlay import OverlayIndicator
from src.paster import paste
from src.platform import is_mac
from src.recorder import log_available_devices, preload_vad, record
from src.sounds import SoundPlayer
from src.transcriber import Transcriber
from src.tray import TrayApp, TrayState

logger = logging.getLogger("zen-whisper")


def _setup_logging(cfg: AppConfig) -> None:
    """ロギングを設定する。"""
    log_path = _ROOT_DIR / cfg.logging.file
    logging.basicConfig(
        level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def _check_mac_permissions(tray: TrayApp) -> None:
    """macOS 固有の権限チェックを実行する（非ブロッキング）。"""
    if not is_mac():
        return

    def _check() -> None:
        try:
            from src.platform.darwin import check_microphone_permission

            if not check_microphone_permission():
                logger.warning("macOS マイク権限が未許可の可能性があります")
                tray.notify(
                    "マイク権限を許可してください：\n"
                    "システム設定 → プライバシーとセキュリティ → マイク"
                )
        except Exception:
            logger.debug("マイク権限チェックでエラーが発生しました", exc_info=True)

    threading.Thread(target=_check, daemon=True).start()


class App:
    """アプリケーション本体。"""

    def __init__(self) -> None:
        self.cfg = load_config()
        _setup_logging(self.cfg)

        logger.info("zen-whisper を起動します")
        log_available_devices(self.cfg.recording.sample_rate)

        self.transcriber = Transcriber()
        self.sound = SoundPlayer(self.cfg.feedback)
        self.overlay = OverlayIndicator(self.cfg.overlay, on_click=self._on_toggle)
        self.tray = TrayApp(
            on_set_language=self._on_set_language,
            on_set_engine=self._on_set_engine,
            on_set_microphone=self._on_set_microphone,
            on_quit=self._on_quit,
            initial_language=self.cfg.recognition.language,
            initial_engine=self.cfg.recognition.engine,
            initial_device=self.cfg.recognition.device,
            initial_microphone=self.cfg.recording.microphone,
            sample_rate=self.cfg.recording.sample_rate,
            initial_qwen3_model=self.cfg.recognition.qwen3_model,
            feedback_config=self.cfg.feedback,
            on_save_config=self._on_save_config,
        )

        self._language = self.cfg.recognition.language
        self._is_recording = False
        self._stop_event = threading.Event()
        self._exit_event = threading.Event()
        self._lock = threading.Lock()
        self._shutdown = False

        atexit.register(self._cleanup)

    def _set_state(self, state: TrayState) -> None:
        """トレイとオーバーレイの状態を同時に変更する。"""
        self.tray.set_state(state)
        self.overlay.set_state(state)

    # ── 設定保存 ──────────────────────────────────────

    def _on_save_config(self) -> bool:
        """現在の設定を config.toml に保存する。"""
        return save_config(self.cfg)

    # ── モデルロード ──────────────────────────────────

    def _load_model_async(self, notify_message: str | None = None) -> None:
        """バックグラウンドでモデルをロードする。完了/失敗時にトレイ通知。"""
        self._set_state(TrayState.LOADING)
        if notify_message:
            self.tray.notify(notify_message)

        def _do_load() -> None:
            try:
                # VAD モデルを事前ロード（初回録音時の遅延を防止）
                preload_vad()

                self.transcriber.load_model(
                    self.cfg.recognition,
                    on_timeout=lambda msg: self.tray.notify(msg),
                )
                if self.transcriber.is_ready:
                    self.tray.notify(
                        f"モデルのロードが完了しました（{self.transcriber.engine_label}）。使用可能です。"
                    )
            except Exception:
                logger.exception("モデルのロードに失敗しました")
                self.tray.notify("モデルのロードに失敗しました。ログを確認してください。")
            finally:
                self._set_state(TrayState.IDLE)

        threading.Thread(target=_do_load, daemon=True).start()

    # ── エンジン切替 ────────────────────────────────────

    def _on_set_engine(
        self,
        engine: str,
        qwen3_model: str | None = None,
        device: str | None = None,
    ) -> None:
        changed = False
        if engine != self.cfg.recognition.engine:
            self.cfg.recognition.engine = engine
            changed = True
        if device is not None and device != self.cfg.recognition.device:
            self.cfg.recognition.device = device
            changed = True
        if qwen3_model and qwen3_model != self.cfg.recognition.qwen3_model:
            self.cfg.recognition.qwen3_model = qwen3_model
            changed = True
        if not changed:
            return
        if not self._on_save_config():
            self.tray.notify("設定の保存に失敗しました。ログを確認してください。")
        label = engine
        if engine == ENGINE_WHISPER and device:
            label = f"{engine} ({device})"
        if engine == ENGINE_QWEN3_ASR and qwen3_model:
            # "Qwen/Qwen3-ASR-0.6B" → "0.6B"
            label = f"{engine} ({qwen3_model.rsplit('-', 1)[-1]})"
        self._load_model_async(notify_message=f"エンジン切替中: {label}")

    # ── マイク切替 ────────────────────────────────────

    def _on_set_microphone(self, microphone: str) -> None:
        if microphone == self.cfg.recording.microphone:
            self.tray.notify(f"マイク: {microphone or 'OS既定'}")
            return
        self.cfg.recording.microphone = microphone
        if self._on_save_config():
            logger.info("マイク設定を保存しました: %s", microphone or "OS既定")
            self.tray.notify(f"マイク: {microphone or 'OS既定'}")
        else:
            logger.warning("マイク設定の保存に失敗しました: %s", microphone or "OS既定")
            self.tray.notify(
                f"マイク: {microphone or 'OS既定'}（設定保存に失敗）"
            )

    # ── 言語切替 ──────────────────────────────────────

    def _on_set_language(self, lang: str) -> None:
        self._language = lang
        logger.info("言語を %s に切替えました", lang)

    def _on_switch_lang(self) -> None:
        new_lang = "en" if self._language == "ja" else "ja"
        self._language = new_lang
        self.tray.set_language(new_lang)
        self.tray.notify(f"言語: {'日本語' if new_lang == 'ja' else 'English'}")
        logger.info("ホットキーで言語を %s に切替えました", new_lang)

    # ── 録音トグル ────────────────────────────────────

    def _on_toggle(self) -> None:
        with self._lock:
            if self._is_recording:
                logger.info("トグルキー: 録音を停止します")
                self._stop_event.set()
                return
            if not self.transcriber.is_ready:
                self.tray.notify("モデルを準備中です。しばらくお待ちください。")
                return
            self._is_recording = True

        t = threading.Thread(target=self._pipeline, daemon=True)
        t.start()

    def _pipeline(self) -> None:
        """録音 → 文字起こし → ペースト の一連の処理。"""
        self._stop_event.clear()
        self._set_state(TrayState.RECORDING)

        try:
            self.sound.play_start()

            def on_speech_change(is_speech: bool) -> None:
                if is_speech:
                    self._set_state(TrayState.SPEECH_DETECTED)
                else:
                    self._set_state(TrayState.RECORDING)

            audio = record(
                self.cfg.recording,
                self._stop_event,
                on_warning=lambda msg: self.tray.notify(msg),
                on_speech_change=on_speech_change,
            )
            self.sound.play_stop()

            if audio is None:
                logger.info("録音データなし（短すぎるか、エラー）。スキップします。")
                return

            if not self.transcriber.is_ready:
                logger.warning("モデルがまだロードされていません。スキップします。")
                self.tray.notify("モデルがロード中です。しばらくお待ちください。")
                return

            self._set_state(TrayState.TRANSCRIBING)
            text = self.transcriber.transcribe(
                audio,
                language=self._language,
                cfg=self.cfg.recognition,
            )

            if not text:
                logger.info("認識結果が空です。スキップします。")
                return

            paste(text, self.cfg.output, on_error=lambda msg: self.tray.notify(msg))

        except Exception:
            logger.exception("パイプライン処理中にエラーが発生しました")
            self.tray.notify("エラーが発生しました。ログを確認してください。")

        finally:
            self._set_state(TrayState.IDLE)
            with self._lock:
                self._is_recording = False

    # ── 終了 ──────────────────────────────────────────

    def _cleanup(self) -> None:
        """終了時のクリーンアップ処理。"""
        if self._shutdown:
            return
        self._shutdown = True
        logger.info("クリーンアップ処理を実行中...")
        self._stop_event.set()
        self._exit_event.set()
        self.overlay.stop()
        self.tray.stop()

    def _on_quit(self) -> None:
        logger.info("zen-whisper を終了します")
        self._cleanup()

    # ── 起動 ──────────────────────────────────────────

    def run(self) -> None:
        """アプリケーションを起動する。"""

        def on_tray_ready(icon) -> None:
            """トレイアイコン表示後のセットアップ。"""
            # macOS マイク権限チェック
            _check_mac_permissions(self.tray)

            # モデルロード（バックグラウンド）
            self._load_model_async()

            # ホットキー登録
            start_hotkey_listener(
                self.cfg.hotkey,
                on_toggle=self._on_toggle,
                on_switch_lang=self._on_switch_lang,
            )
            logger.info("ホットキーリスナーを起動しました")

        # トレイをバックグラウンドで実行（ノンブロッキング）
        self.tray.run_detached(setup=on_tray_ready)

        # メインスレッドで待機（Ctrl+C またはトレイ終了で抜ける）
        try:
            while not self._exit_event.is_set():
                self._exit_event.wait(timeout=1.0)
        except KeyboardInterrupt:
            logger.info("Ctrl+C で終了します")
        self._cleanup()


def main() -> None:
    """エントリポイント関数。"""
    app = App()
    app.run()


if __name__ == "__main__":
    main()

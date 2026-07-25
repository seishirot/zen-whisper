"""zen-whisper エントリポイント。全コンポーネントを統合し、システムトレイアプリとして動作する。"""

from __future__ import annotations

import os
import sys

# pythonw.exe (GUI モード) では sys.stdout/stderr が None になる。
# 一部ライブラリが sys.stderr.write() を呼ぶためクラッシュを防止する。
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
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

# src パッケージをインポート可能にする
_ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT_DIR))

from src.config import (
    ENGINE_QWEN3_ASR,
    ENGINE_REAZON_K2,
    ENGINE_WHISPER,
    POSTPROCESSOR_DICTIONARY,
    POSTPROCESSOR_OFF,
    AppConfig,
    load_config,
    save_config,
)
from src.hotkey import start_hotkey_listener
from src.overlay import OverlayIndicator
from src.paster import paste
from src.postprocessing import (
    DATA_DESTINATION_LOCAL,
    DATA_DESTINATION_REMOTE,
    PostprocessResult,
    load_postprocessors,
    process_transcript,
)
from src.platform import is_mac, terminate_process_tree
from src.profiles import Profile, load_profiles
from src.recorder import log_available_devices, preload_vad, record
from src.sounds import SoundPlayer
from src.transcriber import Transcriber
from src.tray import TrayApp, TrayState

logger = logging.getLogger("zen-whisper")


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _chmod_logs(self) -> None:
        for index in range(self.backupCount + 1):
            path = self.baseFilename if index == 0 else f"{self.baseFilename}.{index}"
            if os.path.exists(path):
                try:
                    os.chmod(path, 0o600)
                except OSError as exc:
                    os.write(
                        2,
                        f"zen-whisper: failed to chmod log file {path}: {exc}\n".encode(
                            "utf-8",
                            errors="replace",
                        ),
                    )

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self._chmod_logs()

    def doRollover(self) -> None:  # noqa: N802 - stdlib override name
        super().doRollover()
        self._chmod_logs()


def _setup_logging(cfg: AppConfig) -> None:
    """ロギングを設定する。"""
    log_path = _ROOT_DIR / cfg.logging.file
    log_handler = _PrivateRotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            log_handler,
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
        self.profiles = load_profiles()
        self.postprocessors = load_postprocessors()
        self._enhancement_lock = threading.RLock()
        self._enhancement_generation = 0
        self._active_postprocessor_process: subprocess.Popen[str] | None = None

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
            profiles=self.profiles,
            postprocessors=self.postprocessors,
            initial_profile=self.cfg.enhancement.profile,
            initial_postprocessor=self.cfg.enhancement.postprocessor,
            on_set_profile=self._on_set_profile,
            on_set_postprocessor=self._on_set_postprocessor,
        )

        self._language = self.cfg.recognition.language
        self._is_recording = False
        self._is_capturing = False
        self._submit_after_paste = False
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

    # ── プロファイル・後処理 ──────────────────────────

    @staticmethod
    def _terminate_postprocessor_process(
        process: subprocess.Popen[str],
    ) -> None:
        try:
            terminate_process_tree(process)
        except Exception as exc:
            logger.warning(
                "後処理プロセスの停止に失敗しました: type=%s",
                type(exc).__name__,
            )

    def _on_set_profile(self, profile_id: str) -> None:
        process = None
        with self._enhancement_lock:
            changed = self.cfg.enhancement.profile != profile_id
            self.cfg.enhancement.profile = profile_id
            if changed:
                self._enhancement_generation += 1
                process = self._active_postprocessor_process
                self._active_postprocessor_process = None
            postprocessor_id = self.cfg.enhancement.postprocessor
        if process is not None:
            self._terminate_postprocessor_process(process)
        if not self._on_save_config():
            self.tray.notify("プロファイル設定の保存に失敗しました。")
        profile = self.profiles.get(profile_id)
        notice = f"プロファイル: {profile.name if profile else 'オフ'}"
        if (
            profile is not None
            and self.cfg.recognition.engine == ENGINE_REAZON_K2
            and postprocessor_id == POSTPROCESSOR_OFF
        ):
            notice += (
                "\nReazon K2 は認識ヒント非対応です。"
                "補正するには辞書置換または CLI 後処理を選んでください。"
            )
        self.tray.notify(notice)

    def _postprocessor_notice(self, postprocessor_id: str) -> str:
        if postprocessor_id == POSTPROCESSOR_OFF:
            return "後処理: オフ"
        if postprocessor_id == POSTPROCESSOR_DICTIONARY:
            with self._enhancement_lock:
                profile_id = self.cfg.enhancement.profile
            if not profile_id:
                return "後処理: 辞書置換のみ（プロファイル未選択のため変更なし）"
            return "後処理: 辞書置換のみ"

        preset = self.postprocessors.get(postprocessor_id)
        if preset is None:
            return f"後処理プリセット '{postprocessor_id}' が見つかりません。"
        if preset.data_destination == DATA_DESTINATION_LOCAL:
            return f"後処理: {preset.display_name}（ローカル）"

        destination = (
            "外部送信"
            if preset.data_destination == DATA_DESTINATION_REMOTE
            else "送信先不明"
        )
        return (
            f"後処理: {preset.display_name}（{destination}）\n"
            "認識結果、選択プロフィールの文脈、辞書データが指定CLIへ渡されます。"
        )

    def _on_set_postprocessor(self, postprocessor_id: str) -> None:
        process = None
        with self._enhancement_lock:
            changed = self.cfg.enhancement.postprocessor != postprocessor_id
            self.cfg.enhancement.postprocessor = postprocessor_id
            if changed:
                self._enhancement_generation += 1
                process = self._active_postprocessor_process
                self._active_postprocessor_process = None
        if process is not None:
            self._terminate_postprocessor_process(process)
        if not self._on_save_config():
            self.tray.notify("後処理設定の保存に失敗しました。")
        self.tray.notify(self._postprocessor_notice(postprocessor_id))

    def _enhancement_selection(self) -> tuple[Profile | None, str, int]:
        """Snapshot the current profile/postprocessor pair."""
        with self._enhancement_lock:
            profile_id = self.cfg.enhancement.profile
            postprocessor_id = self.cfg.enhancement.postprocessor
            generation = self._enhancement_generation
        if not profile_id:
            return None, postprocessor_id, generation
        profile = self.profiles.get(profile_id)
        if profile is None:
            logger.warning("選択中のプロファイルが見つかりません: %s", profile_id)
        return profile, postprocessor_id, generation

    @contextmanager
    def _postprocessor_dispatch_guard(
        self,
        expected_generation: int,
        expected_postprocessor_id: str,
    ) -> Iterator[bool]:
        """Serialize the final selection check with process registration."""
        with self._enhancement_lock:
            yield (
                self._enhancement_generation == expected_generation
                and self.cfg.enhancement.postprocessor
                == expected_postprocessor_id
            )

    def _on_postprocessor_started(
        self,
        process: subprocess.Popen[str],
    ) -> None:
        with self._enhancement_lock:
            self._active_postprocessor_process = process

    def _on_postprocessor_finished(
        self,
        process: subprocess.Popen[str],
    ) -> None:
        with self._enhancement_lock:
            if self._active_postprocessor_process is process:
                self._active_postprocessor_process = None

    def _cancel_active_postprocessor(self) -> None:
        process = None
        with self._enhancement_lock:
            self._enhancement_generation += 1
            process = self._active_postprocessor_process
            self._active_postprocessor_process = None
        if process is not None:
            self._terminate_postprocessor_process(process)

    def _apply_postprocessing(
        self,
        text: str,
        profile: Profile | None,
        postprocessor_id: str,
        language: str,
        submit_after_paste: bool,
        expected_generation: int | None = None,
    ) -> tuple[str, bool]:
        guarded_callbacks = {}
        if expected_generation is not None:
            state_guard = lambda: self._postprocessor_dispatch_guard(
                expected_generation,
                postprocessor_id,
            )
            guarded_callbacks = {
                "start_guard": state_guard,
                "finish_guard": state_guard,
                "on_process_started": self._on_postprocessor_started,
                "on_process_finished": self._on_postprocessor_finished,
            }
        result: PostprocessResult = process_transcript(
            text,
            profile,
            postprocessor_id,
            self.postprocessors,
            language,
            **guarded_callbacks,
        )
        if result.succeeded:
            return result.text, submit_after_paste

        if submit_after_paste:
            self.tray.notify(
                f"後処理に失敗しました: {result.error}\n"
                "辞書置換までの結果を貼り付け、Enter送信はキャンセルしました。"
            )
        else:
            self.tray.notify(
                f"後処理に失敗しました: {result.error}\n"
                "辞書置換までの結果を貼り付けます。"
            )
        return result.text, False

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

    def _on_toggle(self, submit_after_paste: bool = False) -> None:
        with self._lock:
            if self._is_recording:
                if submit_after_paste and self._is_capturing and not self._stop_event.is_set():
                    self._submit_after_paste = True
                    logger.info("送信付きトグルキー: 録音を停止します")
                elif submit_after_paste:
                    logger.info("送信付きトグルキー: 録音停止後のため Enter 送信は行いません")
                else:
                    logger.info("トグルキー: 録音を停止します")
                self._stop_event.set()
                return
            if not self.transcriber.is_ready:
                self.tray.notify("モデルを準備中です。しばらくお待ちください。")
                return
            self._stop_event.clear()
            self._is_recording = True
            self._is_capturing = True
            self._submit_after_paste = False

        t = threading.Thread(target=self._pipeline, daemon=True)
        t.start()

    def _pipeline(self) -> None:
        """録音 → 文字起こし → ペースト の一連の処理。"""
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
            with self._lock:
                self._is_capturing = False
            self.sound.play_stop()

            if audio is None:
                logger.info("録音データなし（短すぎるか、エラー）。スキップします。")
                return

            if not self.transcriber.is_ready:
                logger.warning("モデルがまだロードされていません。スキップします。")
                self.tray.notify("モデルがロード中です。しばらくお待ちください。")
                return

            self._set_state(TrayState.TRANSCRIBING)
            recognition_profile, _, _ = self._enhancement_selection()
            hints = (
                recognition_profile.recognition_hints()
                if recognition_profile is not None
                else None
            )
            text = self.transcriber.transcribe(
                audio,
                language=self._language,
                cfg=self.cfg.recognition,
                hints=hints,
            )

            if not text:
                logger.info("認識結果が空です。スキップします。")
                return

            with self._lock:
                submit_after_paste = self._submit_after_paste

            # Profile/postprocessor selection may change while ASR is running.
            # Re-read immediately before optional CLI dispatch so switching a
            # remote preset off prevents this transcript from being sent.
            profile, postprocessor_id, enhancement_generation = (
                self._enhancement_selection()
            )
            if postprocessor_id != POSTPROCESSOR_OFF:
                self._set_state(TrayState.POSTPROCESSING)
                text, submit_after_paste = self._apply_postprocessing(
                    text,
                    profile,
                    postprocessor_id,
                    self._language,
                    submit_after_paste,
                    expected_generation=enhancement_generation,
                )

            paste(
                text,
                self.cfg.output,
                on_error=lambda msg: self.tray.notify(msg),
                submit_after_paste=submit_after_paste,
            )

        except Exception:
            logger.exception("パイプライン処理中にエラーが発生しました")
            self.tray.notify("エラーが発生しました。ログを確認してください。")

        finally:
            self._set_state(TrayState.IDLE)
            with self._lock:
                self._is_recording = False
                self._is_capturing = False
                self._submit_after_paste = False

    # ── 終了 ──────────────────────────────────────────

    def _cleanup(self) -> None:
        """終了時のクリーンアップ処理。"""
        if self._shutdown:
            return
        self._shutdown = True
        logger.info("クリーンアップ処理を実行中...")
        self._stop_event.set()
        self._exit_event.set()
        self._cancel_active_postprocessor()
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
                on_submit_toggle=lambda: self._on_toggle(submit_after_paste=True),
            )
            logger.info("ホットキーリスナーを起動しました")

            active_postprocessor = self.cfg.enhancement.postprocessor
            if active_postprocessor not in (
                POSTPROCESSOR_OFF,
                POSTPROCESSOR_DICTIONARY,
            ):
                self.tray.notify(self._postprocessor_notice(active_postprocessor))

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

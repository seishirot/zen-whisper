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
import copy
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
    config_file_fingerprint,
    load_config,
    qwen3_model_label,
    save_config,
)
from src.hotkey import start_hotkey_listener, validate_hotkey_config
from src.overlay import OverlayIndicator
from src.paster import paste
from src.postprocessing import (
    DATA_DESTINATION_LOCAL,
    DATA_DESTINATION_REMOTE,
    PostprocessorConfigError,
    PostprocessorPreset,
    PostprocessResult,
    load_local_postprocessor_ids,
    load_postprocessors,
    postprocessors_file_fingerprint,
    process_transcript,
    save_postprocessor,
)
from src.platform import is_mac, terminate_process_tree
from src.profiles import (
    Profile,
    ProfileError,
    load_profiles,
    profile_file_fingerprint,
    save_profile,
)
from src.recorder import log_available_devices, preload_vad, record
from src.settings import SettingsSnapshot, SettingsWindow
from src.sounds import SoundPlayer
from src.toml_storage import FileFingerprint, StaleFileError
from src.transcriber import (
    Transcriber,
    recognition_configuration_error,
)
from src.tray import TrayApp, TrayState

logger = logging.getLogger("zen-whisper")


_MODEL_LOAD_FAILED_MESSAGE = (
    "モデルのロードに失敗しているため実行できません。"
    "ZenWhisperを再起動してください。"
)


def _write_startup_warning(message: str) -> None:
    """Best-effort diagnostics that also work under pythonw.exe."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(message.rstrip() + "\n")
            sys.stderr.flush()
    except Exception:
        pass


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _chmod_logs(self) -> None:
        for index in range(self.backupCount + 1):
            path = self.baseFilename if index == 0 else f"{self.baseFilename}.{index}"
            if os.path.exists(path):
                try:
                    os.chmod(path, 0o600)
                except OSError as exc:
                    _write_startup_warning(
                        f"zen-whisper: failed to chmod log file {path}: {exc}",
                    )

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self._chmod_logs()

    def doRollover(self) -> None:  # noqa: N802 - stdlib override name
        super().doRollover()
        self._chmod_logs()


def _resolve_logging_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else _ROOT_DIR / path


def _logging_destination_error(value: str) -> str:
    """Prepare a requested log destination without truncating existing data."""
    path = _resolve_logging_path(value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            return f"ログファイルにディレクトリは指定できません: {path}"
        with path.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        return (
            f"ログファイルを作成できません: {path} "
            f"({type(exc).__name__})"
        )
    return ""


def _setup_logging(cfg: AppConfig) -> None:
    """ロギングを設定する。"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_path = _resolve_logging_path(cfg.logging.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.insert(
            0,
            _PrivateRotatingFileHandler(
                log_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
        )
    except (OSError, TypeError, ValueError) as exc:
        _write_startup_warning(
            (
                "zen-whisper: log file is unavailable; "
                f"continuing with stream logging ({type(exc).__name__})\n"
            ),
        )
    logging.basicConfig(
        level=(
            getattr(logging, cfg.logging.level.upper(), logging.INFO)
            if isinstance(cfg.logging.level, str)
            else logging.INFO
        ),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
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
        self._config_fingerprint = config_file_fingerprint()
        self._profile_fingerprints = {
            profile_id: profile_file_fingerprint(profile_id)
            for profile_id in self.profiles
        }
        self._postprocessors_fingerprint = (
            postprocessors_file_fingerprint()
        )
        self._config_lock = threading.RLock()
        self._settings_revision = 0
        self._enhancement_lock = threading.RLock()
        self._enhancement_generation = 0
        self._active_postprocessor_process: subprocess.Popen[str] | None = None
        self._model_load_lock = threading.Lock()
        self._model_worker_lock = threading.Lock()
        self._model_load_generation = 0
        self._model_loading = False
        self._model_load_error: str | None = None
        self._language = self.cfg.recognition.language
        self._is_recording = False
        self._is_capturing = False
        self._submit_after_paste = False
        self._stop_event = threading.Event()
        self._exit_event = threading.Event()
        self._lock = threading.Lock()
        self._shutdown = False

        logger.info("zen-whisper を起動します")
        log_available_devices(self.cfg.recording.sample_rate)

        self.transcriber = Transcriber()
        self.sound = SoundPlayer(self.cfg.feedback)
        self.overlay = OverlayIndicator(self.cfg.overlay, on_click=self._on_toggle)
        self.settings = SettingsWindow(
            snapshot_provider=self._settings_snapshot,
            on_save_config=self._on_settings_save_config,
            on_save_profile=self._on_settings_save_profile,
            on_save_postprocessor=self._on_settings_save_postprocessor,
        )
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
            on_set_sound_enabled=self._on_set_sound_enabled,
            profiles=self.profiles,
            postprocessors=self.postprocessors,
            initial_profile=self.cfg.enhancement.profile,
            initial_postprocessor=self.cfg.enhancement.postprocessor,
            on_set_profile=self._on_set_profile,
            on_set_postprocessor=self._on_set_postprocessor,
            on_open_settings=self._on_open_settings,
        )

        atexit.register(self._cleanup)

    def _set_state(self, state: TrayState) -> None:
        """トレイとオーバーレイの状態を同時に変更する。"""
        self.tray.set_state(state)
        self.overlay.set_state(state)

    # ── 設定保存 ──────────────────────────────────────

    def _on_save_config(self) -> bool:
        """現在の設定を config.toml に保存する。"""
        with self._config_lock:
            expected_fingerprint = getattr(
                self,
                "_config_fingerprint",
                None,
            )
            current_fingerprint = config_file_fingerprint()
            if expected_fingerprint is None:
                expected_fingerprint = current_fingerprint
            if current_fingerprint != expected_fingerprint:
                logger.warning(
                    "config.toml が外部変更されたため保存を中止しました"
                )
                return False
            succeeded = save_config(
                self.cfg,
                expected_fingerprint=expected_fingerprint,
            )
            if not succeeded:
                return False
            self._config_fingerprint = config_file_fingerprint()
            self._settings_revision += 1
            return True

    def _on_set_sound_enabled(self, enabled: bool) -> bool:
        with self._config_lock:
            previous = self.cfg.feedback.sound_enabled
            self.cfg.feedback.sound_enabled = enabled
            if not self._on_save_config():
                self.cfg.feedback.sound_enabled = previous
                return False
            return True

    # ── 設定画面 ──────────────────────────────────────

    def _on_open_settings(self) -> None:
        self.settings.show()

    def _settings_snapshot(self) -> SettingsSnapshot:
        with self._lock:
            with self._config_lock:
                with self._enhancement_lock:
                    cfg = copy.deepcopy(self.cfg)
                    profiles = copy.deepcopy(self.profiles)
                    postprocessors = copy.deepcopy(self.postprocessors)
                    revision = self._settings_revision
                    config_fingerprint = getattr(
                        self,
                        "_config_fingerprint",
                        None,
                    )
                    profile_fingerprints = copy.deepcopy(
                        getattr(self, "_profile_fingerprints", {})
                    )
                    postprocessors_fingerprint = getattr(
                        self,
                        "_postprocessors_fingerprint",
                        None,
                    )
            local_ids = load_local_postprocessor_ids()
        if config_fingerprint is None:
            config_fingerprint = config_file_fingerprint()
        if not profile_fingerprints:
            profile_fingerprints = {
                profile_id: profile_file_fingerprint(profile_id)
                for profile_id in profiles
            }
        if postprocessors_fingerprint is None:
            postprocessors_fingerprint = postprocessors_file_fingerprint()
        return SettingsSnapshot(
            config=cfg,
            profiles=profiles,
            postprocessors=postprocessors,
            local_postprocessor_ids=local_ids,
            revision=revision,
            config_fingerprint=config_fingerprint,
            profile_fingerprints=profile_fingerprints,
            postprocessors_fingerprint=postprocessors_fingerprint,
        )

    @staticmethod
    def _recognition_reload_key(cfg: AppConfig) -> tuple[object, ...]:
        recognition = cfg.recognition
        common = (
            recognition.engine,
            recognition.device,
        )
        if recognition.engine == ENGINE_REAZON_K2:
            return common + (
                recognition.reazon_language,
                recognition.reazon_precision,
                recognition.reazon_inference_threads,
            )
        if recognition.engine == ENGINE_QWEN3_ASR:
            return common + (
                recognition.qwen3_model,
                recognition.qwen3_max_new_tokens,
                recognition.qwen3_attn_implementation,
                recognition.qwen3_torch_compile,
            )
        whisper = common + (recognition.model_size,)
        if recognition.device == "cpu":
            return whisper + (recognition.cpu_threads,)
        if recognition.device == "cuda":
            return whisper + (recognition.compute_type,)
        return whisper

    @staticmethod
    def _custom_sound_error(cfg: AppConfig) -> str:
        if cfg.feedback.sound_type != "custom":
            return ""
        for label, value in (
            ("録音開始音", cfg.feedback.custom_start_sound),
            ("録音停止音", cfg.feedback.custom_stop_sound),
        ):
            path = Path(value)
            if not path.is_absolute():
                path = _ROOT_DIR / path
            if not path.is_file():
                return f"{label}のファイルが見つかりません: {path}"
        return ""

    def _on_settings_save_config(
        self,
        cfg: AppConfig,
        expected_revision: int | None = None,
        expected_fingerprint: FileFingerprint | None = None,
    ) -> tuple[bool, str]:
        warnings = cfg.validate() + validate_hotkey_config(cfg.hotkey)
        if warnings:
            return False, "\n".join(warnings)
        sound_error = self._custom_sound_error(cfg)
        if sound_error:
            return False, sound_error

        process = None
        with self._lock:
            if self._is_recording:
                return (
                    False,
                    "録音・文字起こし・校正が終わってから"
                    "設定を保存してください",
                )
            if self._shutdown:
                return False, "終了処理中のため設定を保存できません"

            with self._config_lock:
                if (
                    expected_revision is not None
                    and expected_revision != self._settings_revision
                ):
                    return (
                        False,
                        "設定画面を開いた後に別の操作で設定が変更されました。"
                        "再読込してから編集し直してください",
                    )
                if (
                    expected_fingerprint is not None
                    and config_file_fingerprint()
                    != expected_fingerprint
                ):
                    return (
                        False,
                        "config.toml が設定画面の外で変更されました。"
                        "外部ファイルを元に戻すか、ZenWhisperを再起動して"
                        "変更を読み込んでください",
                    )
                with self._enhancement_lock:
                    if cfg.enhancement.profile and (
                        cfg.enhancement.profile not in self.profiles
                    ):
                        return False, "選択したプロフィールが見つかりません"
                    if (
                        cfg.enhancement.postprocessor
                        not in (
                            POSTPROCESSOR_OFF,
                            POSTPROCESSOR_DICTIONARY,
                        )
                        and cfg.enhancement.postprocessor
                        not in self.postprocessors
                    ):
                        return (
                            False,
                            "選択したCLI後処理プリセットが見つかりません",
                        )

                    old_cfg = self.cfg
                    if old_cfg.logging != cfg.logging:
                        logging_error = _logging_destination_error(
                            cfg.logging.file
                        )
                        if logging_error:
                            return False, logging_error
                    reload_model = self._recognition_reload_key(
                        old_cfg
                    ) != self._recognition_reload_key(cfg)
                    if reload_model:
                        recognition_error = recognition_configuration_error(
                            cfg.recognition
                        )
                        if recognition_error:
                            return False, recognition_error
                    restart_items: list[str] = []
                    if old_cfg.hotkey != cfg.hotkey:
                        restart_items.append("ホットキー")
                    if old_cfg.logging != cfg.logging:
                        restart_items.append("ログ")
                    recreate_overlay = (
                        old_cfg.overlay.enabled != cfg.overlay.enabled
                    )
                    enhancement_changed = (
                        old_cfg.enhancement != cfg.enhancement
                    )

                    saved = (
                        save_config(
                            cfg,
                            expected_fingerprint=expected_fingerprint,
                        )
                        if expected_fingerprint is not None
                        else save_config(cfg)
                    )
                    if not saved:
                        return (
                            False,
                            "config.toml の保存に失敗しました。"
                            "既存ファイルが壊れていないかログを確認してください",
                        )
                    self.cfg = cfg
                    self._config_fingerprint = config_file_fingerprint()
                    self._settings_revision += 1
                    if enhancement_changed:
                        self._enhancement_generation += 1
                        process = self._active_postprocessor_process
                        self._active_postprocessor_process = None

            if process is not None:
                self._terminate_postprocessor_process(process)

            self._language = cfg.recognition.language
            self.sound = SoundPlayer(cfg.feedback)
            if recreate_overlay:
                self.overlay.stop()
                self.overlay = OverlayIndicator(
                    cfg.overlay,
                    on_click=self._on_toggle,
                )
            self.tray.apply_settings(
                cfg,
                self.profiles,
                self.postprocessors,
            )
            if reload_model:
                self._load_model_async(
                    notify_message=(
                        "設定変更によりモデルを再読み込みしています"
                    )
                )
            if (
                enhancement_changed
                and cfg.enhancement.postprocessor
                not in (POSTPROCESSOR_OFF, POSTPROCESSOR_DICTIONARY)
            ):
                self.tray.notify(
                    self._postprocessor_notice(
                        cfg.enhancement.postprocessor
                    )
                )

        message = "設定を保存しました"
        if restart_items:
            message += (
                "。"
                + "・".join(restart_items)
                + "の変更は再起動後に反映されます"
            )
        return True, message

    def _on_settings_save_profile(
        self,
        profile: Profile,
        expected_fingerprint: FileFingerprint | None = None,
    ) -> tuple[bool, str]:
        process = None
        with self._lock:
            if self._is_recording:
                return (
                    False,
                    "録音・文字起こし・校正が終わってから"
                    "プロフィールを保存してください",
                )
            if self._shutdown:
                return False, "終了処理中のため保存できません"
            try:
                with self._config_lock:
                    with self._enhancement_lock:
                        active_profile_id = self.cfg.enhancement.profile
                        runtime_active = self.profiles.get(active_profile_id)
                disk_profiles = load_profiles()
                if (
                    active_profile_id
                    and disk_profiles.get(active_profile_id)
                    != runtime_active
                ):
                    return (
                        False,
                        "使用中のプロフィールが設定画面の外で変更されました。"
                        "外部ファイルを元に戻すか、ZenWhisperを再起動して"
                        "変更を読み込んでください",
                    )
                if (
                    expected_fingerprint is not None
                    and profile_file_fingerprint(profile.profile_id)
                    != expected_fingerprint
                ):
                    return (
                        False,
                        "対象のプロフィールが設定画面の外で変更されました。"
                        "外部ファイルを元に戻すか、ZenWhisperを再起動して"
                        "変更を読み込んでください",
                    )
                if expected_fingerprint is None:
                    save_profile(profile)
                else:
                    save_profile(
                        profile,
                        expected_fingerprint=expected_fingerprint,
                    )
                profiles = load_profiles()
                expected_active = (
                    profile
                    if active_profile_id == profile.profile_id
                    else runtime_active
                )
                if (
                    active_profile_id
                    and profiles.get(active_profile_id)
                    != expected_active
                ):
                    return (
                        False,
                        "保存中に使用中のプロフィールが別の操作で"
                        "変更されたため、実行中の設定には反映しません",
                    )
            except StaleFileError:
                return (
                    False,
                    "対象のプロフィールが保存直前に変更されました。"
                    "ZenWhisperを再起動して内容を確認してください",
                )
            except (ProfileError, OSError) as exc:
                logger.warning(
                    "設定画面からプロフィールを保存できません: %s",
                    exc,
                )
                return False, str(exc)

            with self._config_lock:
                with self._enhancement_lock:
                    self.profiles = profiles
                    self._profile_fingerprints = {
                        profile_id: profile_file_fingerprint(profile_id)
                        for profile_id in profiles
                    }
                    if (
                        self.cfg.enhancement.profile
                        == profile.profile_id
                    ):
                        self._enhancement_generation += 1
                        process = self._active_postprocessor_process
                        self._active_postprocessor_process = None
            if process is not None:
                self._terminate_postprocessor_process(process)
            self.tray.apply_settings(
                self.cfg,
                self.profiles,
                self.postprocessors,
            )
        return True, f"プロフィール「{profile.name}」を保存しました"

    def _on_settings_save_postprocessor(
        self,
        preset: PostprocessorPreset,
        expected_fingerprint: FileFingerprint | None = None,
    ) -> tuple[bool, str]:
        process = None
        with self._lock:
            if self._is_recording:
                return (
                    False,
                    "録音・文字起こし・校正が終わってから"
                    "CLI後処理を保存してください",
                )
            if self._shutdown:
                return False, "終了処理中のため保存できません"
            try:
                with self._config_lock:
                    with self._enhancement_lock:
                        active_postprocessor_id = (
                            self.cfg.enhancement.postprocessor
                        )
                        runtime_active = self.postprocessors.get(
                            active_postprocessor_id
                        )
                disk_postprocessors = load_postprocessors()
                if (
                    active_postprocessor_id
                    not in (POSTPROCESSOR_OFF, POSTPROCESSOR_DICTIONARY)
                    and disk_postprocessors.get(active_postprocessor_id)
                    != runtime_active
                ):
                    return (
                        False,
                        "使用中のCLI後処理が設定画面の外で変更されました。"
                        "外部ファイルを元に戻すか、ZenWhisperを再起動して"
                        "送信先を確認してください",
                    )
                if (
                    expected_fingerprint is not None
                    and postprocessors_file_fingerprint()
                    != expected_fingerprint
                ):
                    return (
                        False,
                        "postprocessors.toml が設定画面の外で変更されました。"
                        "外部ファイルを元に戻すか、ZenWhisperを再起動して"
                        "送信先を確認してください",
                    )
                if expected_fingerprint is None:
                    save_postprocessor(preset)
                else:
                    save_postprocessor(
                        preset,
                        expected_fingerprint=expected_fingerprint,
                    )
                postprocessors = load_postprocessors()
                expected_active = (
                    preset
                    if active_postprocessor_id == preset.preset_id
                    else runtime_active
                )
                if (
                    active_postprocessor_id
                    not in (POSTPROCESSOR_OFF, POSTPROCESSOR_DICTIONARY)
                    and postprocessors.get(active_postprocessor_id)
                    != expected_active
                ):
                    return (
                        False,
                        "保存中に使用中のCLI後処理が別の操作で変更されたため、"
                        "実行中の設定には反映しません",
                    )
            except StaleFileError:
                return (
                    False,
                    "postprocessors.toml が保存直前に変更されました。"
                    "ZenWhisperを再起動して送信先を確認してください",
                )
            except (PostprocessorConfigError, OSError) as exc:
                logger.warning(
                    "設定画面からCLI後処理を保存できません: %s",
                    exc,
                )
                return False, str(exc)

            with self._config_lock:
                with self._enhancement_lock:
                    self.postprocessors = postprocessors
                    self._postprocessors_fingerprint = (
                        postprocessors_file_fingerprint()
                    )
                    active = (
                        self.cfg.enhancement.postprocessor
                        == preset.preset_id
                    )
                    if active:
                        self._enhancement_generation += 1
                        process = self._active_postprocessor_process
                        self._active_postprocessor_process = None
            if process is not None:
                self._terminate_postprocessor_process(process)
            self.tray.apply_settings(
                self.cfg,
                self.profiles,
                self.postprocessors,
            )
            if active:
                self.tray.notify(
                    self._postprocessor_notice(preset.preset_id)
                )
        return True, f"CLI後処理「{preset.display_name}」を保存しました"

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

    def _on_set_profile(self, profile_id: str) -> bool:
        process = None
        with self._config_lock:
            with self._enhancement_lock:
                previous_profile_id = self.cfg.enhancement.profile
                changed = previous_profile_id != profile_id
                self.cfg.enhancement.profile = profile_id
                saved = self._on_save_config()
                if not saved:
                    self.cfg.enhancement.profile = previous_profile_id
                elif changed:
                    self._enhancement_generation += 1
                    process = self._active_postprocessor_process
                    self._active_postprocessor_process = None
                postprocessor_id = self.cfg.enhancement.postprocessor
        if process is not None:
            self._terminate_postprocessor_process(process)
        if not saved:
            self.tray.notify("プロファイル設定の保存に失敗しました。")
            return False
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
        return True

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

    def _on_set_postprocessor(self, postprocessor_id: str) -> bool:
        process = None
        with self._config_lock:
            with self._enhancement_lock:
                previous_postprocessor_id = (
                    self.cfg.enhancement.postprocessor
                )
                changed = (
                    previous_postprocessor_id != postprocessor_id
                )
                self.cfg.enhancement.postprocessor = postprocessor_id
                saved = self._on_save_config()
                if not saved:
                    self.cfg.enhancement.postprocessor = (
                        previous_postprocessor_id
                    )
                elif changed:
                    self._enhancement_generation += 1
                    process = self._active_postprocessor_process
                    self._active_postprocessor_process = None
        if process is not None:
            self._terminate_postprocessor_process(process)
        if not saved:
            self.tray.notify("後処理設定の保存に失敗しました。")
            return False
        self.tray.notify(self._postprocessor_notice(postprocessor_id))
        return True

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
            if (
                submit_after_paste
                and postprocessor_id != POSTPROCESSOR_DICTIONARY
            ):
                self.tray.notify(
                    "CLI後処理結果を貼り付けます。"
                    "内容確認前の誤送信を防ぐため、Enter送信はキャンセルしました。"
                )
                return result.text, False
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

    @staticmethod
    def _unload_transcriber(transcriber: object) -> bool:
        unload = getattr(transcriber, "unload", None)
        if not callable(unload):
            return True
        try:
            unload()
            return True
        except Exception:
            logger.exception("旧ASRモデルの解放に失敗しました")
            return False

    def _load_model_async(self, notify_message: str | None = None) -> None:
        """バックグラウンドでモデルをロードする。完了/失敗時にトレイ通知。"""
        with self._config_lock:
            recognition_cfg = copy.deepcopy(self.cfg.recognition)
            with self._model_load_lock:
                self._model_load_generation += 1
                generation = self._model_load_generation
                self._model_loading = True
                self._model_load_error = None
        self._set_state(TrayState.LOADING)
        if notify_message:
            self.tray.notify(notify_message)

        def _is_current() -> bool:
            with self._model_load_lock:
                return (
                    not self._shutdown
                    and generation == self._model_load_generation
                )

        def _do_load() -> None:
            with self._model_worker_lock:
                if not _is_current():
                    logger.info(
                        "待機中の古いモデルロードを開始せず破棄しました: "
                        "generation=%d",
                        generation,
                    )
                    return
                candidate = None
                try:
                    candidate = Transcriber()
                    with self._model_load_lock:
                        if (
                            self._shutdown
                            or generation != self._model_load_generation
                        ):
                            return
                        previous = self.transcriber
                        self.transcriber = candidate

                    # Keep at most one heavyweight ASR backend resident. The
                    # published candidate is intentionally not ready until its
                    # model has loaded, and recording start is blocked meanwhile.
                    if not self._unload_transcriber(previous):
                        raise RuntimeError(
                            "旧ASRモデルを安全に解放できませんでした"
                        )
                    if not _is_current():
                        self._unload_transcriber(candidate)
                        return

                    # VAD モデルを事前ロード（初回録音時の遅延を防止）
                    preload_vad()

                    candidate.load_model(
                        recognition_cfg,
                        on_timeout=lambda msg: (
                            self.tray.notify(msg)
                            if _is_current()
                            else None
                        ),
                    )
                    accepted = _is_current()
                    if not accepted:
                        self._unload_transcriber(candidate)
                        logger.info(
                            "古いモデルロード結果を破棄しました: "
                            "generation=%d",
                            generation,
                        )
                        return
                    if not candidate.is_ready:
                        raise RuntimeError(
                            "モデルのロード完了後もASRバックエンドを利用できません"
                        )
                    self.tray.notify(
                        "モデルのロードが完了しました"
                        f"（{candidate.engine_label}）。使用可能です。"
                    )
                except Exception:
                    with self._model_load_lock:
                        current_failure = (
                            not self._shutdown
                            and generation == self._model_load_generation
                        )
                        failed_transcriber = (
                            candidate if candidate is not None else self.transcriber
                        )
                        if current_failure:
                            self._model_load_error = _MODEL_LOAD_FAILED_MESSAGE
                    if not current_failure:
                        if candidate is not None:
                            self._unload_transcriber(candidate)
                        logger.info(
                            "古いモデルロードの失敗を無視しました: "
                            "generation=%d",
                            generation,
                        )
                        return
                    self._unload_transcriber(failed_transcriber)
                    logger.exception("モデルのロードに失敗しました")
                    self.tray.notify(
                        "モデルのロードに失敗しました。"
                        "ログを確認してください。"
                    )
                finally:
                    with self._model_load_lock:
                        finish_current = (
                            not self._shutdown
                            and generation == self._model_load_generation
                        )
                        if finish_current:
                            self._model_loading = False
                    if finish_current:
                        self._set_state(TrayState.IDLE)

        worker = threading.Thread(target=_do_load, daemon=True)
        try:
            worker.start()
        except Exception:
            with self._model_load_lock:
                start_failure = (
                    not self._shutdown
                    and generation == self._model_load_generation
                )
                if start_failure:
                    self._model_loading = False
                    self._model_load_error = _MODEL_LOAD_FAILED_MESSAGE
                    failed_transcriber = self.transcriber
            if start_failure:
                self._unload_transcriber(failed_transcriber)
                self._set_state(TrayState.IDLE)
                self.tray.notify(
                    "モデル読込スレッドを開始できませんでした。"
                    "ログを確認してください。"
                )
            logger.exception("モデル読込スレッドを開始できませんでした")

    # ── エンジン切替 ────────────────────────────────────

    def _on_set_engine(
        self,
        engine: str,
        qwen3_model: str | None = None,
        device: str | None = None,
    ) -> bool:
        with self._lock:
            if self._is_recording:
                self.tray.notify(
                    "録音・文字起こし・校正が終わってから"
                    "エンジンを切り替えてください"
                )
                return False
            if self._shutdown:
                return False

            changed = False
            with self._config_lock:
                proposed = copy.deepcopy(self.cfg.recognition)
                if engine != proposed.engine:
                    proposed.engine = engine
                    changed = True
                if (
                    device is not None
                    and device != proposed.device
                ):
                    proposed.device = device
                    changed = True
                if (
                    qwen3_model
                    and qwen3_model != proposed.qwen3_model
                ):
                    proposed.qwen3_model = qwen3_model
                    changed = True
                if not changed:
                    return True
                recognition_error = recognition_configuration_error(proposed)
                if recognition_error:
                    self.tray.notify(recognition_error)
                    return False
                previous = self.cfg.recognition
                self.cfg.recognition = proposed
                saved = self._on_save_config()
            if not saved:
                with self._config_lock:
                    self.cfg.recognition = previous
                self.tray.notify(
                    "設定の保存に失敗しました。ログを確認してください。"
                )
                return False
            label = engine
            if engine == ENGINE_WHISPER and device:
                label = f"{engine} ({device})"
            if engine == ENGINE_QWEN3_ASR and qwen3_model:
                label = f"{engine} ({qwen3_model_label(qwen3_model)})"
            self._load_model_async(
                notify_message=f"エンジン切替中: {label}"
            )
        return True

    # ── マイク切替 ────────────────────────────────────

    def _on_set_microphone(self, microphone: str) -> bool:
        with self._config_lock:
            if microphone == self.cfg.recording.microphone:
                unchanged = True
                saved = True
            else:
                unchanged = False
                previous = self.cfg.recording.microphone
                self.cfg.recording.microphone = microphone
                saved = self._on_save_config()
                if not saved:
                    self.cfg.recording.microphone = previous
        if unchanged:
            self.tray.notify(f"マイク: {microphone or 'OS既定'}")
            return True
        if saved:
            logger.info("マイク設定を保存しました: %s", microphone or "OS既定")
            self.tray.notify(f"マイク: {microphone or 'OS既定'}")
        else:
            logger.warning("マイク設定の保存に失敗しました: %s", microphone or "OS既定")
            self.tray.notify(
                f"マイク: {microphone or 'OS既定'}（設定保存に失敗）"
            )
        return saved

    # ── 言語切替 ──────────────────────────────────────

    def _on_set_language(self, lang: str) -> bool:
        with self._config_lock:
            previous_language = self._language
            previous_config_language = self.cfg.recognition.language
            self._language = lang
            self.cfg.recognition.language = lang
            saved = self._on_save_config()
            if not saved:
                self._language = previous_language
                self.cfg.recognition.language = previous_config_language
        if not saved:
            self.tray.notify("言語設定の保存に失敗しました")
            return False
        logger.info("言語を %s に切替えました", lang)
        return True

    def _on_switch_lang(self) -> None:
        with self._config_lock:
            previous_language = self._language
            previous_config_language = self.cfg.recognition.language
            new_lang = "en" if self._language == "ja" else "ja"
            self._language = new_lang
            self.cfg.recognition.language = new_lang
            saved = self._on_save_config()
            if not saved:
                self._language = previous_language
                self.cfg.recognition.language = previous_config_language
                new_lang = previous_language
        self.tray.set_language(new_lang)
        if not saved:
            self.tray.notify("言語設定の保存に失敗しました")
            return
        self.tray.notify(f"言語: {'日本語' if new_lang == 'ja' else 'English'}")
        logger.info("ホットキーで言語を %s に切替えました", new_lang)

    # ── 録音トグル ────────────────────────────────────

    def _on_toggle(self, submit_after_paste: bool = False) -> None:
        with self._lock:
            if getattr(self, "_shutdown", False):
                logger.info("終了処理中のため録音トグルを無視します")
                return
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
            with self._model_load_lock:
                model_loading = self._model_loading
                model_load_error = self._model_load_error
            if model_loading:
                self.tray.notify(
                    "モデルを準備中です。しばらくお待ちください。"
                )
                return
            if model_load_error:
                self.tray.notify(model_load_error)
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
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._stop_event.set()
            self._exit_event.set()
        logger.info("クリーンアップ処理を実行中...")
        with self._model_load_lock:
            self._model_load_generation += 1
            self._model_loading = False
        self._cancel_active_postprocessor()
        self.settings.stop()
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


def main() -> int:
    """エントリポイント関数。"""
    # Direct development launches share the same single-instance/control path
    # as the installed GUI entry point, although importing this module is heavier.
    from src.launcher import main as launcher_main

    return launcher_main(app_factory=App)


if __name__ == "__main__":
    raise SystemExit(main())

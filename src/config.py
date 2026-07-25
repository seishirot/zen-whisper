"""設定読み込みモジュール。config.toml を読み込み、デフォルト値とマージする。"""

from __future__ import annotations

import logging
import math
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.toml_storage import (
    FileFingerprint,
    atomic_write_toml,
    file_fingerprint,
)

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT_DIR / "config.toml"

# エンジン名定数
ENGINE_AUTO = "auto"
ENGINE_WHISPER = "whisper"
ENGINE_REAZON_K2 = "reazon-k2"
ENGINE_QWEN3_ASR = "qwen3-asr"
VALID_ENGINES = (ENGINE_WHISPER, ENGINE_REAZON_K2, ENGINE_QWEN3_ASR)
VALID_DEVICES = ("cuda", "cpu", "mlx")
ASR_SAMPLE_RATE = 16000
POSTPROCESSOR_OFF = "off"
POSTPROCESSOR_DICTIONARY = "dictionary"

# Qwen3-ASR モデル名定数（トレイメニューでのサイズ切替に使用）
QWEN3_MODEL_LARGE = "Qwen/Qwen3-ASR-1.7B"  # 高精度・既定
QWEN3_MODEL_SMALL = "Qwen/Qwen3-ASR-0.6B"  # 高速・やや低精度


def _default_recognition_device() -> str:
    return "mlx" if sys.platform == "darwin" else "cuda"


@dataclass
class HotkeyConfig:
    toggle: str | list[str] = "shift+space"
    submit_toggle: str | list[str] = ""
    switch_lang: str = "shift+alt+space"


@dataclass
class RecognitionConfig:
    language: str = "ja"
    engine: str = ENGINE_WHISPER
    model_size: str = "large-v3-turbo"
    compute_type: str = "float16"
    beam_size: int = 5
    cpu_threads: int = 4
    model_load_timeout_sec: int = 300
    device: str = field(default_factory=_default_recognition_device)
    reazon_language: str = "ja"
    reazon_precision: str = "fp32"
    reazon_chunk_sec: float = 25.0
    reazon_trailing_silence_sec: float = 0.5
    qwen3_model: str = QWEN3_MODEL_LARGE  # Qwen3-ASR 使用時のモデル名（既定: 1.7B 高精度）
    qwen3_max_new_tokens: int = 128  # Qwen3-ASR 生成トークン上限（短文入力なら 128 で十分）
    # アテンション実装: "auto"（FA2 があれば使用、無ければ sdpa）/ "sdpa" / "flash_attention_2" / "eager"
    qwen3_attn_implementation: str = "auto"
    # torch.compile() による高速化。triton 必須（Windows 非対応）のため既定では無効。
    qwen3_torch_compile: bool = False
    # ハルシネーション抑制パラメータ
    no_speech_threshold: float = 0.6
    condition_on_previous_text: bool = False
    hallucination_silence_threshold: float | None = 2.0


@dataclass
class RecordingConfig:
    microphone: str = ""
    sample_rate: int = ASR_SAMPLE_RATE
    vad_silence_threshold_sec: float = 10.0
    min_recording_sec: float = 0.5
    min_audio_rms: float = 0.001
    min_audio_peak: float = 0.01
    max_recording_sec: float = 300.0
    max_recording_warning_pct: int = 80


@dataclass
class OutputConfig:
    restore_clipboard: bool = True
    paste_delay_ms: int = 100


@dataclass
class EnhancementConfig:
    profile: str = ""
    postprocessor: str = POSTPROCESSOR_OFF


@dataclass
class FeedbackConfig:
    sound_enabled: bool = True
    sound_type: str = "tone"  # "tone" (生成音) or "custom" (カスタムファイル)
    volume: float = 0.5  # 0.0〜1.0
    custom_start_sound: str = "assets/start.flac"
    custom_stop_sound: str = "assets/stop.flac"


@dataclass
class OverlayConfig:
    enabled: bool = True
    position: str = "bottom-center"
    size: int = 48


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "zen-whisper.log"


@dataclass
class AppConfig:
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    enhancement: EnhancementConfig = field(default_factory=EnhancementConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> list[str]:
        """設定値をバリデーションし、警告メッセージのリストを返す。"""
        def is_number(value: object) -> bool:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            try:
                return math.isfinite(value)
            except OverflowError:
                return False

        warnings: list[str] = []
        if (
            not is_number(self.recording.max_recording_sec)
            or self.recording.max_recording_sec <= 0
        ):
            warnings.append("max_recording_sec は正の値である必要があります")
        if self.recording.sample_rate != ASR_SAMPLE_RATE:
            warnings.append(
                f"sample_rate は ASR/VAD の処理レートとして {ASR_SAMPLE_RATE}Hz 固定です"
            )
        if (
            not is_number(self.recording.min_audio_rms)
            or self.recording.min_audio_rms < 0
        ):
            warnings.append("min_audio_rms は 0 以上である必要があります")
        if (
            not is_number(self.recording.min_audio_peak)
            or self.recording.min_audio_peak < 0
        ):
            warnings.append("min_audio_peak は 0 以上である必要があります")
        if (
            not is_number(self.recording.vad_silence_threshold_sec)
            or self.recording.vad_silence_threshold_sec < 0
        ):
            warnings.append(
                "vad_silence_threshold_sec は 0 以上である必要があります"
            )
        if (
            not is_number(self.recording.min_recording_sec)
            or self.recording.min_recording_sec < 0
        ):
            warnings.append("min_recording_sec は 0 以上である必要があります")
        if (
            is_number(self.recording.min_recording_sec)
            and is_number(self.recording.max_recording_sec)
            and self.recording.min_recording_sec
            > self.recording.max_recording_sec
        ):
            warnings.append(
                "min_recording_sec は max_recording_sec 以下である必要があります"
            )
        if self.recognition.language not in ("ja", "en"):
            warnings.append(f"language '{self.recognition.language}' は未検証です（ja/en 推奨）")
        if (
            not is_number(self.recognition.beam_size)
            or self.recognition.beam_size <= 0
        ):
            warnings.append("beam_size は正の値である必要があります")
        if (
            not is_number(self.recognition.cpu_threads)
            or self.recognition.cpu_threads <= 0
        ):
            warnings.append("cpu_threads は正の値である必要があります")
        if (
            not is_number(self.recognition.model_load_timeout_sec)
            or self.recognition.model_load_timeout_sec <= 0
        ):
            warnings.append("model_load_timeout_sec は正の値である必要があります")
        if (
            not isinstance(self.recognition.model_size, str)
            or not self.recognition.model_size.strip()
        ):
            warnings.append("model_size は空にできません")
        if (
            not isinstance(self.recognition.compute_type, str)
            or not self.recognition.compute_type.strip()
        ):
            warnings.append("compute_type は空にできません")
        if self.recognition.device not in VALID_DEVICES:
            warnings.append(
                f"device '{self.recognition.device}' は無効です"
                f"（有効値: {', '.join(VALID_DEVICES)}）"
            )
        if self.recognition.reazon_language not in ("ja", "ja-en"):
            warnings.append("reazon_language は 'ja' または 'ja-en' を指定してください")
        if self.recognition.reazon_precision not in ("fp32", "int8", "int8-fp32"):
            warnings.append(
                "reazon_precision は 'fp32', 'int8', 'int8-fp32' のいずれかを指定してください"
            )
        if (
            not is_number(self.recognition.reazon_chunk_sec)
            or self.recognition.reazon_chunk_sec <= 0
        ):
            warnings.append("reazon_chunk_sec は正の値である必要があります")
        if (
            not is_number(
                self.recognition.reazon_trailing_silence_sec
            )
            or self.recognition.reazon_trailing_silence_sec < 0
        ):
            warnings.append("reazon_trailing_silence_sec は 0 以上である必要があります")
        if (
            not is_number(self.recognition.qwen3_max_new_tokens)
            or self.recognition.qwen3_max_new_tokens <= 0
        ):
            warnings.append(
                "qwen3_max_new_tokens は正の値である必要があります"
            )
        if self.recognition.qwen3_attn_implementation not in (
            "auto",
            "sdpa",
            "flash_attention_2",
            "eager",
        ):
            warnings.append(
                "qwen3_attn_implementation は auto, sdpa, "
                "flash_attention_2, eager のいずれかを指定してください"
            )
        if (
            not isinstance(self.recognition.qwen3_model, str)
            or not self.recognition.qwen3_model.strip()
        ):
            warnings.append("qwen3_model は空にできません")
        if (
            not is_number(self.recognition.no_speech_threshold)
            or not (
                0.0
                <= self.recognition.no_speech_threshold
                <= 1.0
            )
        ):
            warnings.append(
                "no_speech_threshold は 0〜1 の範囲で指定してください"
            )
        if (
            self.recognition.hallucination_silence_threshold is not None
            and (
                not is_number(
                    self.recognition.hallucination_silence_threshold
                )
                or self.recognition.hallucination_silence_threshold < 0
            )
        ):
            warnings.append(
                "hallucination_silence_threshold は 0 以上で指定してください"
            )
        if (
            not is_number(self.recording.max_recording_warning_pct)
            or not (
                0
                < self.recording.max_recording_warning_pct
                <= 100
            )
        ):
            warnings.append("max_recording_warning_pct は 1〜100 の範囲である必要があります")
        if self.recognition.engine not in VALID_ENGINES:
            warnings.append(
                f"engine '{self.recognition.engine}' は無効です"
                f"（有効値: {', '.join(VALID_ENGINES)}）"
            )
        if not isinstance(self.enhancement.profile, str):
            warnings.append("enhancement.profile は文字列で指定してください")
        if (
            not isinstance(self.enhancement.postprocessor, str)
            or not self.enhancement.postprocessor
        ):
            warnings.append("enhancement.postprocessor は空でない文字列で指定してください")
        if not self.hotkey.toggle:
            warnings.append("hotkey.toggle は空にできません")
        if not self.hotkey.switch_lang:
            warnings.append("hotkey.switch_lang は空にできません")
        if (
            not is_number(self.output.paste_delay_ms)
            or self.output.paste_delay_ms < 0
        ):
            warnings.append("paste_delay_ms は 0 以上である必要があります")
        if self.feedback.sound_type not in ("tone", "custom"):
            warnings.append("sound_type は tone または custom を指定してください")
        if (
            not is_number(self.feedback.volume)
            or not 0.0 <= self.feedback.volume <= 1.0
        ):
            warnings.append("volume は 0〜1 の範囲で指定してください")
        if self.feedback.sound_type == "custom" and (
            not self.feedback.custom_start_sound
            or not self.feedback.custom_stop_sound
        ):
            warnings.append(
                "custom サウンドでは開始音と停止音のパスが必要です"
            )
        if (
            not isinstance(self.logging.level, str)
            or self.logging.level.upper() not in (
                "DEBUG",
                "INFO",
                "WARNING",
                "ERROR",
            )
        ):
            warnings.append(
                "logging.level は DEBUG, INFO, WARNING, ERROR "
                "のいずれかを指定してください"
            )
        if (
            not isinstance(self.logging.file, str)
            or not self.logging.file.strip()
        ):
            warnings.append("logging.file は空にできません")
        return warnings


def _merge_section(dc: object, data: dict) -> None:
    """dataclass インスタンスに辞書の値を上書きマージする。"""
    for key, value in data.items():
        if hasattr(dc, key):
            setattr(dc, key, value)


def _normalize_legacy_auto(cfg: AppConfig) -> None:
    """Normalize old auto settings to explicit defaults."""
    if cfg.recognition.engine == ENGINE_AUTO:
        logger.warning("engine='auto' は非推奨です。engine='whisper' として扱います")
        cfg.recognition.engine = ENGINE_WHISPER
    if cfg.recognition.device == "auto":
        device = _default_recognition_device()
        logger.warning("device='auto' は非推奨です。device='%s' として扱います", device)
        cfg.recognition.device = device


def _normalize_recording_sample_rate(cfg: AppConfig) -> None:
    """Keep the app-internal audio contract aligned with ASR backends."""
    if cfg.recording.sample_rate != ASR_SAMPLE_RATE:
        logger.warning(
            "recording.sample_rate=%s は現在サポートされません。%sHz として扱います",
            cfg.recording.sample_rate,
            ASR_SAMPLE_RATE,
        )
        cfg.recording.sample_rate = ASR_SAMPLE_RATE


def _normalize_optional_recognition_values(cfg: AppConfig) -> None:
    """Decode TOML-safe sentinels used for optional recognition values."""
    value = cfg.recognition.hallucination_silence_threshold
    if isinstance(value, str) and value.strip().lower() == "off":
        cfg.recognition.hallucination_silence_threshold = None


def _normalize_enhancement(cfg: AppConfig) -> None:
    """Keep malformed enhancement selections from breaking the tray at startup."""
    if not isinstance(cfg.enhancement.profile, str):
        logger.warning("enhancement.profile の型が不正なためオフとして扱います")
        cfg.enhancement.profile = ""
    if (
        not isinstance(cfg.enhancement.postprocessor, str)
        or not cfg.enhancement.postprocessor
    ):
        logger.warning("enhancement.postprocessor が不正なためオフとして扱います")
        cfg.enhancement.postprocessor = POSTPROCESSOR_OFF


def load_config(path: Path | None = None) -> AppConfig:
    """TOML 設定ファイルを読み込み AppConfig を返す。ファイルが無ければデフォルト値。"""
    cfg = AppConfig()
    config_path = path or _CONFIG_PATH

    if not config_path.exists():
        logger.info("設定ファイルが見つかりません。デフォルト値を使用します: %s", config_path)
        return cfg

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        logger.exception("設定ファイルの読み込みに失敗しました: %s", config_path)
        return cfg

    section_map = {
        "hotkey": cfg.hotkey,
        "recognition": cfg.recognition,
        "recording": cfg.recording,
        "output": cfg.output,
        "enhancement": cfg.enhancement,
        "feedback": cfg.feedback,
        "overlay": cfg.overlay,
        "logging": cfg.logging,
    }
    for section_name, dc_instance in section_map.items():
        if section_name in data:
            section_data = data[section_name]
            if not isinstance(section_data, dict):
                logger.warning(
                    "設定セクション [%s] がテーブルでないため無視します",
                    section_name,
                )
                continue
            _merge_section(dc_instance, section_data)

    _normalize_legacy_auto(cfg)
    _normalize_recording_sample_rate(cfg)
    _normalize_optional_recognition_values(cfg)
    _normalize_enhancement(cfg)

    # バリデーション
    warnings = cfg.validate()
    for w in warnings:
        logger.warning("設定バリデーション: %s", w)

    logger.info("設定ファイルを読み込みました: %s", config_path)
    return cfg


def config_file_fingerprint(path: Path | None = None) -> FileFingerprint:
    """Return the identity of the config file used by the settings UI."""
    return file_fingerprint(path or _CONFIG_PATH)


def save_config(
    cfg: AppConfig,
    path: Path | None = None,
    *,
    expected_fingerprint: FileFingerprint | None = None,
) -> bool:
    """現在の AppConfig を TOML ファイルに書き出す。"""
    config_path = path or _CONFIG_PATH
    try:
        existing: dict[str, object] = {}
        if config_path.is_file():
            try:
                with config_path.open("rb") as file:
                    existing = tomllib.load(file)
            except Exception:
                logger.exception(
                    "既存の設定ファイルが壊れているため上書きしません: %s",
                    config_path,
                )
                return False
        managed = asdict(cfg)
        recognition = managed.get("recognition")
        if (
            isinstance(recognition, dict)
            and recognition.get("hallucination_silence_threshold") is None
        ):
            # TOML has no null value. Keep the UI's blank/disabled state
            # round-trippable with an explicit string sentinel.
            recognition["hallucination_silence_threshold"] = "off"
        merged = dict(existing)
        for section_name, section_data in managed.items():
            existing_section = merged.get(section_name)
            if (
                isinstance(existing_section, dict)
                and isinstance(section_data, dict)
            ):
                merged[section_name] = {
                    **existing_section,
                    **section_data,
                }
            else:
                merged[section_name] = section_data
        atomic_write_toml(
            merged,
            config_path,
            expected_fingerprint=expected_fingerprint,
        )
        logger.info("設定ファイルを保存しました: %s", config_path)
        return True
    except Exception:
        logger.exception("設定ファイルの保存に失敗しました: %s", config_path)
        return False

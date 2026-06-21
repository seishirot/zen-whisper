"""設定読み込みモジュール。config.toml を読み込み、デフォルト値とマージする。"""

from __future__ import annotations

import logging
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

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
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> list[str]:
        """設定値をバリデーションし、警告メッセージのリストを返す。"""
        warnings: list[str] = []
        if self.recording.max_recording_sec <= 0:
            warnings.append("max_recording_sec は正の値である必要があります")
        if self.recording.sample_rate != ASR_SAMPLE_RATE:
            warnings.append(
                f"sample_rate は ASR/VAD の処理レートとして {ASR_SAMPLE_RATE}Hz 固定です"
            )
        if self.recording.min_audio_rms < 0:
            warnings.append("min_audio_rms は 0 以上である必要があります")
        if self.recording.min_audio_peak < 0:
            warnings.append("min_audio_peak は 0 以上である必要があります")
        if self.recognition.language not in ("ja", "en"):
            warnings.append(f"language '{self.recognition.language}' は未検証です（ja/en 推奨）")
        if self.recognition.beam_size <= 0:
            warnings.append("beam_size は正の値である必要があります")
        if self.recognition.cpu_threads <= 0:
            warnings.append("cpu_threads は正の値である必要があります")
        if self.recognition.model_load_timeout_sec <= 0:
            warnings.append("model_load_timeout_sec は正の値である必要があります")
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
        if self.recognition.reazon_chunk_sec <= 0:
            warnings.append("reazon_chunk_sec は正の値である必要があります")
        if self.recognition.reazon_trailing_silence_sec < 0:
            warnings.append("reazon_trailing_silence_sec は 0 以上である必要があります")
        if not 0 < self.recording.max_recording_warning_pct <= 100:
            warnings.append("max_recording_warning_pct は 1〜100 の範囲である必要があります")
        if self.recognition.engine not in VALID_ENGINES:
            warnings.append(
                f"engine '{self.recognition.engine}' は無効です"
                f"（有効値: {', '.join(VALID_ENGINES)}）"
            )
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
        "feedback": cfg.feedback,
        "overlay": cfg.overlay,
        "logging": cfg.logging,
    }
    for section_name, dc_instance in section_map.items():
        if section_name in data:
            _merge_section(dc_instance, data[section_name])

    _normalize_legacy_auto(cfg)
    _normalize_recording_sample_rate(cfg)

    # バリデーション
    warnings = cfg.validate()
    for w in warnings:
        logger.warning("設定バリデーション: %s", w)

    logger.info("設定ファイルを読み込みました: %s", config_path)
    return cfg


def save_config(cfg: AppConfig, path: Path | None = None) -> bool:
    """現在の AppConfig を TOML ファイルに書き出す。"""
    config_path = path or _CONFIG_PATH
    try:
        with open(config_path, "wb") as f:
            tomli_w.dump(asdict(cfg), f)
        logger.info("設定ファイルを保存しました: %s", config_path)
        return True
    except Exception:
        logger.exception("設定ファイルの保存に失敗しました: %s", config_path)
        return False

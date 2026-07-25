"""Whisper-family ASR backends: faster-whisper and mlx-whisper."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import numpy as np

from src.asr.base import RecognitionHints, load_with_timeout
from src.config import ASR_SAMPLE_RATE, RecognitionConfig

logger = logging.getLogger(__name__)

_MLX_REPO_MAP: dict[str, str] = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base",
    "small": "mlx-community/whisper-small",
    "medium": "mlx-community/whisper-medium",
    "large": "mlx-community/whisper-large-v3",
    "large-v2": "mlx-community/whisper-large-v2",
    "large-v3": "mlx-community/whisper-large-v3",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}
_MAX_WHISPER_HOTWORDS_CHARS = 240


def _whisper_hotwords(hints: RecognitionHints | None) -> str:
    """Keep profile vocabulary within Whisper's limited prompt budget."""
    if hints is None:
        return ""

    selected: list[str] = []
    current_length = 0
    for word in hints.hotwords:
        added_length = len(word) + (2 if selected else 0)
        if current_length + added_length > _MAX_WHISPER_HOTWORDS_CHARS:
            logger.info(
                "Whisper のプロファイル語彙を上限で打ち切りました: chars=%d",
                current_length,
            )
            break
        selected.append(word)
        current_length += added_length
    return ", ".join(selected)


def _to_mlx_repo(model_size: str) -> str:
    """Convert faster-whisper model size names to mlx-whisper repositories."""
    repo = _MLX_REPO_MAP.get(model_size)
    if repo is None:
        if "/" in model_size:
            return model_size
        logger.warning(
            "MLX リポジトリマッピングが見つかりません: %s → デフォルト (large-v3-turbo) を使用",
            model_size,
        )
        return "mlx-community/whisper-large-v3-turbo"
    return repo


def _cuda_available() -> bool:
    """Best-effort CUDA availability check for CTranslate2/faster-whisper."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        pass

    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def is_cuda_available() -> bool:
    """Return whether faster-whisper can likely use CUDA."""
    return _cuda_available()


def _resolve_device(cfg: RecognitionConfig) -> str:
    """Resolve cfg.device. Legacy auto prefers MLX on macOS, CUDA if available, then CPU."""
    device = cfg.device.lower()
    if device == "auto":
        if sys.platform == "darwin":
            return "mlx"
        return "cuda" if _cuda_available() else "cpu"
    return device


class MlxWhisperBackend:
    """mlx-whisper backend used on Apple Silicon."""

    name = "mlx"

    def __init__(self) -> None:
        self._mlx_model_repo = ""

    @property
    def is_ready(self) -> bool:
        return bool(self._mlx_model_repo)

    def load(
        self,
        cfg: RecognitionConfig,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        repo = _to_mlx_repo(cfg.model_size)
        logger.info("MLX-whisper を初期化: repo=%s", repo)

        def _warmup() -> bool:
            import mlx_whisper

            dummy_audio = np.zeros(ASR_SAMPLE_RATE, dtype=np.float32)
            mlx_whisper.transcribe(
                dummy_audio,
                path_or_hf_repo=repo,
                language="en",
            )
            return True

        load_with_timeout(
            _warmup,
            cfg.model_load_timeout_sec,
            "MLX",
            on_timeout,
        )

        self._mlx_model_repo = repo
        logger.info("MLX-whisper モデルのロードが完了しました")

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
        hints: RecognitionHints | None = None,
    ) -> str:
        import mlx_whisper

        kwargs: dict[str, object] = {
            "path_or_hf_repo": self._mlx_model_repo,
            "language": language,
            "beam_size": cfg.beam_size,
            "no_speech_threshold": cfg.no_speech_threshold,
            "condition_on_previous_text": (
                cfg.condition_on_previous_text
            ),
        }
        if cfg.hallucination_silence_threshold is not None:
            kwargs["hallucination_silence_threshold"] = (
                cfg.hallucination_silence_threshold
            )
            # mlx-whisper only applies hallucination silence handling
            # when word timestamps are enabled.
            kwargs["word_timestamps"] = True
        hotwords = _whisper_hotwords(hints)
        if hotwords:
            kwargs["initial_prompt"] = f"重要語彙: {hotwords}"
        result = mlx_whisper.transcribe(audio, **kwargs)
        return result["text"].strip()


class FasterWhisperBackend:
    """faster-whisper backend for CUDA and CPU CTranslate2 inference."""

    name = "faster-whisper"

    def __init__(self, device: str) -> None:
        self._device = device
        self._model = None

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def load(
        self,
        cfg: RecognitionConfig,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        compute_type = cfg.compute_type
        kwargs: dict[str, object] = {
            "device": self._device,
            "compute_type": compute_type,
        }
        if self._device == "cpu":
            kwargs["compute_type"] = "int8"
            kwargs["cpu_threads"] = cfg.cpu_threads
            kwargs["num_workers"] = 1

        def factory() -> object:
            from faster_whisper import WhisperModel

            return WhisperModel(cfg.model_size, **kwargs)

        model = load_with_timeout(
            factory,
            cfg.model_load_timeout_sec,
            "faster-whisper",
            on_timeout,
        )
        if model is not None:
            self._model = model
            logger.info(
                "faster-whisper モデルのロードが完了しました (device=%s, compute=%s)",
                self._device,
                kwargs["compute_type"],
            )

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
        hints: RecognitionHints | None = None,
    ) -> str:
        if self._model is None:
            logger.error("モデルがロードされていません")
            return ""

        transcribe_kwargs: dict[str, object] = {
            "language": language,
            "beam_size": cfg.beam_size,
            "vad_filter": True,
            "no_speech_threshold": cfg.no_speech_threshold,
            "condition_on_previous_text": cfg.condition_on_previous_text,
        }
        if cfg.hallucination_silence_threshold is not None:
            transcribe_kwargs["hallucination_silence_threshold"] = (
                cfg.hallucination_silence_threshold
            )
        hotwords = _whisper_hotwords(hints)
        if hotwords:
            transcribe_kwargs["hotwords"] = hotwords

        segments, _info = self._model.transcribe(audio, **transcribe_kwargs)
        return "".join(seg.text for seg in segments).strip()

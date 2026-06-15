"""Whisper-family ASR backends: faster-whisper and mlx-whisper."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable

import numpy as np

from src.asr.base import load_with_timeout
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

        timeout_sec = cfg.model_load_timeout_sec
        error: list[Exception] = []
        done = threading.Event()

        def _warmup() -> None:
            try:
                import mlx_whisper

                dummy_audio = np.zeros(ASR_SAMPLE_RATE, dtype=np.float32)
                mlx_whisper.transcribe(
                    dummy_audio,
                    path_or_hf_repo=repo,
                    language="en",
                )
                done.set()
            except Exception as e:
                error.append(e)
                done.set()

        t = threading.Thread(target=_warmup, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)

        if not done.is_set():
            msg = f"MLX モデルのロードが {timeout_sec}秒 でタイムアウトしました"
            logger.error(msg)
            if on_timeout:
                on_timeout(msg)
            return

        if error:
            raise error[0]

        self._mlx_model_repo = repo
        logger.info("MLX-whisper モデルのロードが完了しました")

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
    ) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._mlx_model_repo,
            language=language,
        )
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

        segments, _info = self._model.transcribe(audio, **transcribe_kwargs)
        return "".join(seg.text for seg in segments).strip()

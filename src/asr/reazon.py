"""ReazonSpeech K2 backend."""

from __future__ import annotations

import importlib.util
import logging
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from src.asr.base import load_with_timeout
from src.config import ASR_SAMPLE_RATE, RecognitionConfig

logger = logging.getLogger(__name__)

_reazon_k2_available: bool | None = None


def is_reazon_k2_available() -> bool:
    """Return whether ReazonSpeech K2 is importable."""
    global _reazon_k2_available
    if _reazon_k2_available is None:
        try:
            _reazon_k2_available = (
                importlib.util.find_spec("reazonspeech") is not None
                and importlib.util.find_spec("reazonspeech.k2.asr") is not None
            )
        except (ImportError, ModuleNotFoundError, ValueError):
            _reazon_k2_available = False
    return _reazon_k2_available


def _iter_reazon_chunks(
    audio: np.ndarray,
    cfg: RecognitionConfig,
    sample_rate: int = ASR_SAMPLE_RATE,
) -> Iterator[np.ndarray]:
    """Yield Reazon-safe chunks with trailing silence appended."""
    if len(audio) == 0:
        return

    chunk_samples = max(1, int(cfg.reazon_chunk_sec * sample_rate))
    silence_samples = max(0, int(cfg.reazon_trailing_silence_sec * sample_rate))
    silence = np.zeros(silence_samples, dtype=np.float32)

    for start in range(0, len(audio), chunk_samples):
        chunk = audio[start : start + chunk_samples].astype(np.float32, copy=False)
        if silence_samples:
            chunk = np.concatenate([chunk, silence])
        yield chunk


class ReazonK2Backend:
    """CPU backend using ReazonSpeech K2."""

    name = "reazon-k2"

    def __init__(self) -> None:
        self._model = None
        self._audio_from_path = None
        self._transcribe = None

    @property
    def is_ready(self) -> bool:
        return (
            self._model is not None
            and self._audio_from_path is not None
            and self._transcribe is not None
        )

    def load(
        self,
        cfg: RecognitionConfig,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        from reazonspeech.k2.asr import audio_from_path, load_model, transcribe

        model = load_with_timeout(
            lambda: self._load_reazon_model(load_model, cfg),
            cfg.model_load_timeout_sec,
            "ReazonSpeech K2",
            on_timeout,
        )
        if model is None:
            return
        self._model = model
        self._audio_from_path = audio_from_path
        self._transcribe = transcribe
        logger.info(
            "ReazonSpeech K2 モデルのロードが完了しました (precision=%s, language=%s)",
            cfg.reazon_precision,
            cfg.reazon_language,
        )

    def _load_reazon_model(self, load_model: Callable[..., Any], cfg: RecognitionConfig):
        try:
            return load_model(
                device="cpu",
                precision=cfg.reazon_precision,
                language=cfg.reazon_language,
            )
        except Exception:
            if cfg.reazon_language == "ja":
                raise
            logger.warning(
                "ReazonSpeech K2 の language=%s ロードに失敗したため ja にフォールバックします",
                cfg.reazon_language,
                exc_info=True,
            )
            return load_model(
                device="cpu",
                precision=cfg.reazon_precision,
                language="ja",
            )

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
    ) -> str:
        if not self.is_ready:
            logger.error("モデルがロードされていません")
            return ""

        parts: list[str] = []
        with tempfile.TemporaryDirectory(prefix="zen_whisper_reazon_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            for index, chunk in enumerate(_iter_reazon_chunks(audio, cfg)):
                chunk_path = tmp_path / f"chunk_{index:03d}.wav"
                sf.write(chunk_path, chunk, ASR_SAMPLE_RATE, subtype="PCM_16")
                speech = self._audio_from_path(str(chunk_path))
                result = self._transcribe(self._model, speech)
                text = getattr(result, "text", str(result)).strip()
                if text:
                    parts.append(text)

        return " ".join(parts).strip()

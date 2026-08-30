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

from src.asr.base import RecognitionHints, load_with_timeout
from src.config import ASR_SAMPLE_RATE, RecognitionConfig
from src.model_provenance import download_verified_snapshot, model_source

logger = logging.getLogger(__name__)

_reazon_k2_available: bool | None = None


def _reazon_model_files(precision: str) -> dict[str, str]:
    base = "epoch-99-avg-1"
    if precision == "fp32":
        return {
            "tokens": "tokens.txt",
            "encoder": f"encoder-{base}.onnx",
            "decoder": f"decoder-{base}.onnx",
            "joiner": f"joiner-{base}.onnx",
        }
    if precision == "int8":
        return {
            "tokens": "tokens.txt",
            "encoder": f"encoder-{base}.int8.onnx",
            "decoder": f"decoder-{base}.int8.onnx",
            "joiner": f"joiner-{base}.int8.onnx",
        }
    if precision == "int8-fp32":
        return {
            "tokens": "tokens.txt",
            "encoder": f"encoder-{base}.int8.onnx",
            "decoder": f"decoder-{base}.onnx",
            "joiner": f"joiner-{base}.int8.onnx",
        }
    raise ValueError(f"Unknown precision: {precision}")


def _load_pinned_reazon_model(
    *,
    device: str,
    precision: str,
    language: str,
    num_threads: int = 1,
    local_files_only: bool = False,
    on_model_resolved: Callable[[Path, dict[str, Path]], None] | None = None,
) -> object:
    """Load the public Reazon K2 model from its reviewed immutable snapshot."""
    if language != "ja":
        raise ValueError(f"No approved Reazon snapshot for language: {language}")
    if num_threads <= 0:
        raise ValueError("num_threads must be positive")
    source = model_source("reazon_k2", language)
    files = _reazon_model_files(precision)
    download_kwargs: dict[str, object] = {
        "required_files": tuple(files.values()),
    }
    if local_files_only:
        download_kwargs["local_files_only"] = True
    snapshot = Path(download_verified_snapshot(source, **download_kwargs))
    resolved_files = {name: snapshot / filename for name, filename in files.items()}
    if on_model_resolved is not None:
        on_model_resolved(snapshot, resolved_files)

    import sherpa_onnx

    logger.info(
        "ReazonSpeech K2 実効モデル設定: revision=%s, precision=%s, "
        "num_threads=%d, files=%s",
        source.revision,
        precision,
        num_threads,
        ",".join(path.name for path in resolved_files.values()),
    )
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        tokens=str(resolved_files["tokens"]),
        encoder=str(resolved_files["encoder"]),
        decoder=str(resolved_files["decoder"]),
        joiner=str(resolved_files["joiner"]),
        num_threads=num_threads,
        sample_rate=ASR_SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        provider=device,
    )


def is_reazon_k2_available() -> bool:
    """Return whether ReazonSpeech K2 is importable."""
    global _reazon_k2_available
    if _reazon_k2_available is None:
        try:
            _reazon_k2_available = (
                importlib.util.find_spec("reazonspeech") is not None
                and importlib.util.find_spec("reazonspeech.k2.asr") is not None
            )
        except Exception as exc:
            logger.warning(
                "Reazon K2 の利用可否を確認できません: type=%s",
                type(exc).__name__,
            )
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
        from reazonspeech.k2.asr import audio_from_path, transcribe

        model = load_with_timeout(
            lambda: self._load_reazon_model(_load_pinned_reazon_model, cfg),
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
            "ReazonSpeech K2 モデルのロードが完了しました "
            "(precision=%s, language=%s, threads=%d)",
            cfg.reazon_precision,
            cfg.reazon_language,
            cfg.reazon_inference_threads,
        )

    def _load_reazon_model(self, load_model: Callable[..., Any], cfg: RecognitionConfig):
        return load_model(
            device="cpu",
            precision=cfg.reazon_precision,
            language=cfg.reazon_language,
            num_threads=cfg.reazon_inference_threads,
        )

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
        hints: RecognitionHints | None = None,
    ) -> str:
        if not self.is_ready:
            logger.error("モデルがロードされていません")
            return ""

        chunk_samples = max(1, int(cfg.reazon_chunk_sec * ASR_SAMPLE_RATE))
        chunk_count = (len(audio) + chunk_samples - 1) // chunk_samples
        logger.info(
            "ReazonSpeech K2 転写条件: precision=%s, configured_threads=%d, "
            "chunk_sec=%.3f, trailing_silence_sec=%.3f, chunks=%d, "
            "stream_per_chunk=true, join=space",
            cfg.reazon_precision,
            cfg.reazon_inference_threads,
            cfg.reazon_chunk_sec,
            cfg.reazon_trailing_silence_sec,
            chunk_count,
        )
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

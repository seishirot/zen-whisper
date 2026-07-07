"""ASR adapters for the macOS backend.

The MLX dependencies are intentionally imported lazily so protocol and boundary
tests can run on machines without MLX or downloaded models.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np


class AdapterError(RuntimeError):
    """Raised for engine/model load or transcription failures."""


class ASRAdapter(Protocol):
    engine_id: str

    def preload(self, model_id: str, language: str) -> None:
        """Load or warm up model resources."""

    def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
        """Transcribe a WAV file and return plain text."""


@dataclass
class DummyAdapter:
    engine_id: str = "dummy"
    text: str = "dummy transcript"

    def preload(self, model_id: str, language: str) -> None:
        return None

    def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
        if not audio_path.exists():
            raise AdapterError("Audio file does not exist")
        return self.text.strip()


class MlxWhisperAdapter:
    engine_id = "mlx-whisper"

    def __init__(self) -> None:
        self._loaded_model: str | None = None

    def preload(self, model_id: str, language: str) -> None:
        try:
            import mlx_whisper

            mlx_whisper.transcribe(
                np.zeros(16000, dtype=np.float32),
                path_or_hf_repo=model_id,
                **_language_kwargs(language),
            )
        except Exception as exc:  # pragma: no cover - depends on optional MLX/model IO
            raise AdapterError("MLX Whisper unavailable") from exc
        self._loaded_model = model_id

    def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
        if self._loaded_model != model_id:
            self.preload(model_id, language)
        try:
            audio = _load_wav_float32_mono_16k(audio_path)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("Could not read WAV audio") from exc
        try:
            import mlx_whisper

            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=model_id,
                **_language_kwargs(language),
            )
        except Exception as exc:  # pragma: no cover - depends on optional MLX/model IO
            raise AdapterError("MLX Whisper transcribe failed") from exc
        return _extract_text(result)


class MlxQwen3AsrAdapter:
    engine_id = "mlx-qwen3-asr"

    def __init__(self) -> None:
        self._loaded_model_id: str | None = None
        self._model: object | None = None

    def preload(self, model_id: str, language: str) -> None:
        model = self._ensure_model(model_id)
        if not callable(getattr(model, "generate", None)):
            raise AdapterError("mlx-audio Qwen3-ASR model has no generate() method")

    def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
        model = self._ensure_model(model_id)
        generate = getattr(model, "generate", None)
        if not callable(generate):
            raise AdapterError("mlx-audio Qwen3-ASR model has no generate() method")
        try:
            result = generate(str(audio_path), **_qwen_language_kwargs(language))
        except Exception as exc:  # pragma: no cover - depends on optional MLX/model IO
            raise AdapterError("Qwen3-ASR transcribe failed") from exc
        return _extract_text(result)

    def _ensure_model(self, model_id: str) -> object:
        if self._model is not None and self._loaded_model_id == model_id:
            return self._model
        try:
            from mlx_audio.stt import load
        except Exception as exc:  # pragma: no cover - depends on optional MLX install
            raise AdapterError("mlx-audio is required for Qwen3-ASR") from exc
        try:
            self._model = load(model_id)
        except Exception as exc:  # pragma: no cover - depends on optional MLX/model IO
            raise AdapterError("Qwen3-ASR unavailable") from exc
        self._loaded_model_id = model_id
        return self._model


def make_adapter(engine_id: str) -> ASRAdapter:
    if engine_id == "mlx-whisper":
        return MlxWhisperAdapter()
    if engine_id == "mlx-qwen3-asr":
        return MlxQwen3AsrAdapter()
    if engine_id == "dummy":
        return DummyAdapter()
    raise AdapterError(f"Unsupported engine: {engine_id}")


def _extract_text(result: object) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        value = result.get("text")
        if isinstance(value, str):
            return value.strip()
    if isinstance(result, list):
        return "".join(_extract_text(item) for item in result).strip()
    value = getattr(result, "text", None)
    if isinstance(value, str):
        return value.strip()
    raise AdapterError(f"Could not extract text from result type: {type(result)!r}")


def _language_kwargs(language: str) -> dict[str, str]:
    if language == "auto":
        return {}
    return {"language": language}


def _qwen_language_kwargs(language: str) -> dict[str, str | None]:
    if language == "auto":
        return {"language": None}
    return {"language": language}


def _load_wav_float32_mono_16k(audio_path: Path) -> np.ndarray:
    import soundfile as sf

    audio, samplerate = sf.read(audio_path, dtype="float32", always_2d=False)
    if samplerate != 16000:
        raise AdapterError(f"Expected 16 kHz WAV, got {samplerate} Hz")
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if audio.ndim != 1:
        raise AdapterError("Expected mono WAV audio")
    return np.asarray(audio, dtype=np.float32)

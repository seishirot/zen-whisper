"""ReazonSpeech K2 backend helpers."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from src.asr.reazon import ReazonK2Backend, _iter_reazon_chunks
from src.config import RecognitionConfig


def test_iter_reazon_chunks_adds_trailing_silence() -> None:
    cfg = RecognitionConfig(reazon_chunk_sec=2.0, reazon_trailing_silence_sec=0.5)
    audio = np.ones(5 * 16000, dtype=np.float32)

    chunks = list(_iter_reazon_chunks(audio, cfg))

    assert [len(chunk) for chunk in chunks] == [
        int(2.5 * 16000),
        int(2.5 * 16000),
        int(1.5 * 16000),
    ]
    assert np.all(chunks[0][: 2 * 16000] == 1.0)
    assert np.all(chunks[0][2 * 16000 :] == 0.0)


def test_reazon_backend_transcribes_chunks_in_order() -> None:
    cfg = RecognitionConfig(reazon_chunk_sec=1.0, reazon_trailing_silence_sec=0.0)
    audio = np.ones(int(2.2 * 16000), dtype=np.float32)
    backend = ReazonK2Backend()
    backend._model = object()
    backend._audio_from_path = lambda path: path

    def fake_transcribe(model, speech):
        index = speech.rsplit("_", 1)[-1].split(".", 1)[0]
        return SimpleNamespace(text=f"chunk-{index}")

    backend._transcribe = fake_transcribe

    assert backend.transcribe(audio, "ja", cfg) == "chunk-000 chunk-001 chunk-002"


def test_reazon_load_falls_back_to_ja_when_language_unavailable() -> None:
    cfg = RecognitionConfig(reazon_language="ja-en")
    backend = ReazonK2Backend()
    calls: list[str] = []

    def fake_load_model(*, device: str, precision: str, language: str):
        calls.append(language)
        if language == "ja-en":
            raise RuntimeError("missing model")
        return object()

    model = backend._load_reazon_model(fake_load_model, cfg)

    assert model is not None
    assert calls == ["ja-en", "ja"]

"""Profile hints passed to supported ASR backends."""

from __future__ import annotations

import sys
import types

import numpy as np

from src.asr.base import RecognitionHints
from src.asr.whisper import FasterWhisperBackend, MlxWhisperBackend
from src.config import RecognitionConfig


def test_faster_whisper_receives_profile_hotwords():
    captured = {}

    class FakeModel:
        def transcribe(self, audio, **kwargs):
            captured.update(kwargs)
            return [types.SimpleNamespace(text=" result ")], None

    backend = FasterWhisperBackend("cpu")
    backend._model = FakeModel()

    text = backend.transcribe(
        np.zeros(1600, dtype=np.float32),
        "ja",
        RecognitionConfig(),
        RecognitionHints(context="full context", hotwords=("ZenWhisper", "ゼンウィスパー")),
    )

    assert text == "result"
    assert captured["hotwords"] == "ZenWhisper, ゼンウィスパー"
    assert "initial_prompt" not in captured


def test_mlx_whisper_receives_profile_terms_as_initial_prompt(monkeypatch):
    captured = {}

    def fake_transcribe(audio, **kwargs):
        captured.update(kwargs)
        return {"text": " result "}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )
    backend = MlxWhisperBackend()
    backend._mlx_model_path = "test-repo"

    text = backend.transcribe(
        np.zeros(1600, dtype=np.float32),
        "ja",
        RecognitionConfig(),
        RecognitionHints(hotwords=("ZenWhisper",)),
    )

    assert text == "result"
    assert captured["initial_prompt"] == "重要語彙: ZenWhisper"


def test_mlx_whisper_receives_structured_whisper_settings(monkeypatch):
    captured = {}

    def fake_transcribe(audio, **kwargs):
        captured.update(kwargs)
        return {"text": " result "}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )
    backend = MlxWhisperBackend()
    backend._mlx_model_path = "test-repo"
    cfg = RecognitionConfig(
        beam_size=3,
        no_speech_threshold=0.75,
        condition_on_previous_text=True,
        hallucination_silence_threshold=1.25,
    )

    text = backend.transcribe(
        np.zeros(1600, dtype=np.float32),
        "ja",
        cfg,
    )

    assert text == "result"
    assert captured == {
        "path_or_hf_repo": "test-repo",
        "language": "ja",
        "beam_size": 3,
        "no_speech_threshold": 0.75,
        "condition_on_previous_text": True,
        "hallucination_silence_threshold": 1.25,
        "word_timestamps": True,
    }


def test_faster_whisper_loads_only_the_verified_local_snapshot(monkeypatch):
    constructor_calls: list[tuple[str, dict[str, object]]] = []
    model = object()

    def fake_whisper_model(path: str, **kwargs: object) -> object:
        constructor_calls.append((path, kwargs))
        return model

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=fake_whisper_model),
    )
    monkeypatch.setattr(
        "src.asr.whisper.download_verified_snapshot",
        lambda _source: "/verified/faster-whisper",
    )
    backend = FasterWhisperBackend("cpu")

    backend.load(RecognitionConfig(model_size="large-v3-turbo"))

    assert backend._model is model
    assert constructor_calls == [
        (
            "/verified/faster-whisper",
            {
                "local_files_only": True,
                "device": "cpu",
                "compute_type": "int8",
                "cpu_threads": 4,
                "num_workers": 1,
            },
        )
    ]


def test_mlx_whisper_loads_only_the_verified_local_snapshot(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_transcribe(audio: object, **kwargs: object) -> dict[str, str]:
        calls.append(dict(kwargs))
        return {"text": "warm"}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )
    monkeypatch.setattr(
        "src.asr.whisper.download_verified_snapshot",
        lambda _source: "/verified/mlx-whisper",
    )
    backend = MlxWhisperBackend()

    backend.load(RecognitionConfig(model_size="large-v3-turbo"))

    assert backend._mlx_model_path == "/verified/mlx-whisper"
    assert calls == [
        {
            "path_or_hf_repo": "/verified/mlx-whisper",
            "language": "en",
        }
    ]

"""Profile hints passed to supported ASR backends."""

from __future__ import annotations

import sys
import types

import numpy as np

from src.asr.base import RecognitionHints
from src.asr.qwen import Qwen3Backend
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
    backend._mlx_model_repo = "test-repo"

    text = backend.transcribe(
        np.zeros(1600, dtype=np.float32),
        "ja",
        RecognitionConfig(),
        RecognitionHints(hotwords=("ZenWhisper",)),
    )

    assert text == "result"
    assert captured["initial_prompt"] == "重要語彙: ZenWhisper"


def test_qwen_receives_full_profile_context():
    captured = {}

    class FakeModel:
        def transcribe(self, **kwargs):
            captured.update(kwargs)
            return [types.SimpleNamespace(text=" result ")]

    backend = Qwen3Backend()
    backend._model = FakeModel()

    text = backend.transcribe(
        np.zeros(1600, dtype=np.float32),
        "ja",
        RecognitionConfig(),
        RecognitionHints(context="project context", hotwords=("ZenWhisper",)),
    )

    assert text == "result"
    assert captured["context"] == "project context"
    assert captured["language"] == "Japanese"

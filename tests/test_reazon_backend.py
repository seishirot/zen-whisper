"""ReazonSpeech K2 backend helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.asr.reazon import (
    ReazonK2Backend,
    _iter_reazon_chunks,
    _load_pinned_reazon_model,
)
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


def test_reazon_load_does_not_silently_fallback_for_unsupported_language() -> None:
    cfg = RecognitionConfig(reazon_language="ja-en")
    backend = ReazonK2Backend()
    calls: list[str] = []

    def fake_load_model(*, device: str, precision: str, language: str):
        calls.append(language)
        raise ValueError("unsupported language")

    with pytest.raises(ValueError, match="unsupported language"):
        backend._load_reazon_model(fake_load_model, cfg)

    assert calls == ["ja-en"]


def test_pinned_reazon_loader_rejects_unapproved_bilingual_snapshot() -> None:
    with pytest.raises(ValueError, match="No approved Reazon snapshot"):
        _load_pinned_reazon_model(
            device="cpu",
            precision="int8-fp32",
            language="ja-en",
        )


@pytest.mark.parametrize(
    ("precision", "expected_files"),
    [
        (
            "fp32",
            {
                "tokens.txt",
                "encoder-epoch-99-avg-1.onnx",
                "decoder-epoch-99-avg-1.onnx",
                "joiner-epoch-99-avg-1.onnx",
            },
        ),
        (
            "int8",
            {
                "tokens.txt",
                "encoder-epoch-99-avg-1.int8.onnx",
                "decoder-epoch-99-avg-1.int8.onnx",
                "joiner-epoch-99-avg-1.int8.onnx",
            },
        ),
        (
            "int8-fp32",
            {
                "tokens.txt",
                "encoder-epoch-99-avg-1.int8.onnx",
                "decoder-epoch-99-avg-1.onnx",
                "joiner-epoch-99-avg-1.int8.onnx",
            },
        ),
    ],
)
def test_pinned_reazon_loader_passes_verified_local_files(
    monkeypatch,
    tmp_path: Path,
    precision: str,
    expected_files: set[str],
) -> None:
    download_calls: list[tuple[object, tuple[str, ...]]] = []
    recognizer = object()
    factory_calls: list[dict[str, object]] = []

    def fake_download(source, *, required_files: tuple[str, ...]) -> str:
        download_calls.append((source, required_files))
        return str(tmp_path)

    def fake_from_transducer(**kwargs):
        factory_calls.append(kwargs)
        return recognizer

    monkeypatch.setattr("src.asr.reazon.download_verified_snapshot", fake_download)
    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        SimpleNamespace(
            OfflineRecognizer=SimpleNamespace(from_transducer=fake_from_transducer)
        ),
    )

    assert (
        _load_pinned_reazon_model(
            device="cpu",
            precision=precision,
            language="ja",
        )
        is recognizer
    )
    assert len(download_calls) == 1
    assert set(download_calls[0][1]) == expected_files
    expected_paths = {name: str(tmp_path / name) for name in expected_files}
    assert factory_calls == [
        {
            "tokens": expected_paths["tokens.txt"],
            "encoder": next(
                path for name, path in expected_paths.items() if name.startswith("encoder-")
            ),
            "decoder": next(
                path for name, path in expected_paths.items() if name.startswith("decoder-")
            ),
            "joiner": next(
                path for name, path in expected_paths.items() if name.startswith("joiner-")
            ),
            "num_threads": 1,
            "sample_rate": 16000,
            "feature_dim": 80,
            "decoding_method": "greedy_search",
            "provider": "cpu",
        }
    ]


def test_reazon_model_files_rejects_unknown_precision() -> None:
    with pytest.raises(ValueError, match="Unknown precision"):
        _load_pinned_reazon_model(device="cpu", precision="unknown", language="ja")

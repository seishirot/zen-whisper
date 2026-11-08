"""Haas-ja ONNX backend helpers (no model download)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.asr import haas
from src.asr.haas import (
    HaasBackend,
    _decode_bytes,
    _Decoding,
    _Detokenizer,
    _tdt_greedy_decode,
    haas_model_files,
    split_at_quiet_points,
)
from src.config import AppConfig, RecognitionConfig
from src.model_provenance import model_source


def test_model_files_select_one_encoder_and_are_pinned() -> None:
    source = model_source("haas_onnx", "ja")
    assert source.revision == "e46a4b107ffba60336eb3de9745da6f628cfae35"
    for precision in ("int8", "int4-onecomp"):
        files = haas_model_files(precision)
        assert f"{precision}/encoder.onnx.data" in files
        assert {"LICENSE", "NOTICE", "common/tokens.json"} <= set(files)
        assert set(files) <= set(source.files)
    assert "int4-onecomp/encoder.onnx" not in haas_model_files("int8")


def test_model_files_reject_unknown_precision() -> None:
    with pytest.raises(ValueError, match="Unknown Haas precision"):
        haas_model_files("fp32")


def test_loader_requests_only_selected_files(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []
    created: list[tuple[Path, str, int]] = []

    def fake_download(source, *, required_files, local_files_only=False):
        calls.append((required_files, local_files_only))
        return str(tmp_path)

    class FakeModel:
        def __init__(self, snapshot, precision, threads):
            created.append((snapshot, precision, threads))

    monkeypatch.setattr(haas, "download_verified_snapshot", fake_download)
    monkeypatch.setattr(haas, "_HaasModel", FakeModel)

    haas._load_pinned_haas_model(precision="int4-onecomp", num_threads=3)

    assert calls == [(haas_model_files("int4-onecomp"), False)]
    assert created == [(tmp_path, "int4-onecomp", 3)]


def test_loader_rejects_non_positive_threads() -> None:
    with pytest.raises(ValueError, match="num_threads"):
        haas._load_pinned_haas_model(precision="int8", num_threads=0)


def test_backend_passes_configured_precision_and_threads() -> None:
    cfg = RecognitionConfig(haas_precision="int4-onecomp", haas_inference_threads=6)
    calls: list[dict[str, object]] = []

    def fake_load(**kwargs):
        calls.append(kwargs)
        return object()

    HaasBackend._load_haas_model(fake_load, cfg)
    assert calls == [{"precision": "int4-onecomp", "num_threads": 6}]


class _RecordingModel:
    def __init__(self) -> None:
        self.lengths: list[int] = []

    def transcribe(self, audio: np.ndarray) -> str:
        self.lengths.append(len(audio))
        return f"<{len(self.lengths)}>"


def test_backend_returns_empty_for_empty_or_silent_input() -> None:
    backend = HaasBackend()
    model = _RecordingModel()
    backend._model = model
    cfg = RecognitionConfig()

    assert backend.transcribe(np.zeros(0, np.float32), "ja", cfg) == ""
    assert backend.transcribe(np.zeros(16000, np.float32), "ja", cfg) == ""
    assert backend.transcribe(np.ones(100, np.float32), "ja", cfg) == ""
    assert model.lengths == []


def test_backend_chunks_long_audio_and_joins_without_separator() -> None:
    backend = HaasBackend()
    model = _RecordingModel()
    backend._model = model
    cfg = RecognitionConfig(haas_chunk_sec=10.0)
    audio = np.full(25 * 16000, 0.1, dtype=np.float32)

    assert backend.transcribe(audio, "ja", cfg) == "<1><2><3>"
    assert sum(model.lengths) == len(audio)


def test_unloaded_backend_returns_empty() -> None:
    assert HaasBackend().transcribe(np.ones(16000, np.float32), "ja", RecognitionConfig()) == ""


def test_split_at_quiet_points_prefers_silence_near_target() -> None:
    rate = 16000
    audio = np.full(40 * rate, 0.5, dtype=np.float32)
    audio[int(29.0 * rate) : int(29.3 * rate)] = 0.0

    bounds = split_at_quiet_points(audio, chunk_seconds=28.0)

    assert bounds[0][0] == 0 and bounds[-1][1] == len(audio)
    assert 29.0 * rate <= bounds[0][1] <= 29.3 * rate
    assert all(left[1] == right[0] for left, right in zip(bounds, bounds[1:]))


def test_split_keeps_short_audio_whole() -> None:
    audio = np.ones(5 * 16000, dtype=np.float32)
    assert split_at_quiet_points(audio, chunk_seconds=28.0) == [(0, len(audio))]


def test_detokenizer_handles_prefix_and_byte_fallback() -> None:
    pieces = ("▁今日", "は", "<0xE3>", "<0x81>", "<0x82>", "<unk>")
    detokenizer = _Detokenizer(pieces, unk_id=5, add_dummy_prefix=True)

    assert detokenizer.decode([0, 1, 2, 3, 4]) == "今日はあ"
    assert _decode_bytes(bytearray(b"\xe3\x81")) == "��"


def test_tdt_greedy_decode_emits_tokens_and_skips_durations() -> None:
    cfg = _Decoding(blank_id=2, durations=(0, 1, 2), max_symbols=10, num_tokens=2)
    # frame -> (token, duration index)
    plan = {0: (0, 2), 2: (1, 1), 3: (2, 1)}

    def joint_at(t, pred):
        token, duration = plan.get(t, (2, 1))
        logits = np.zeros(cfg.num_tokens + 1 + len(cfg.durations), np.float32)
        logits[token] = 1.0
        logits[cfg.num_tokens + 1 + duration] = 1.0
        return logits

    def predict(token, state):
        return np.zeros(1), state

    assert _tdt_greedy_decode(4, joint_at, predict, (np.zeros(1), None), cfg) == [0, 1]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("haas_precision", "fp32", "haas_precision"),
        ("haas_inference_threads", 0, "haas_inference_threads"),
        ("haas_chunk_sec", 90.0, "haas_chunk_sec"),
        ("haas_chunk_sec", 0.5, "haas_chunk_sec"),
    ],
)
def test_config_validation_rejects_invalid_haas_values(field, value, message) -> None:
    cfg = AppConfig()
    setattr(cfg.recognition, field, value)
    assert any(message in warning for warning in cfg.validate())


def test_default_haas_config_is_valid() -> None:
    assert not [w for w in AppConfig().validate() if "haas" in w]

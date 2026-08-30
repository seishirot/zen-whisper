"""Tests for the retained private ASR corpus preparation."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

import tools.prepare_private_asr_corpus as private_corpus


def test_detect_utterances_splits_only_long_pauses() -> None:
    rate = private_corpus.SAMPLE_RATE
    tone = np.full(int(0.4 * rate), 0.1, dtype=np.float32)
    short_pause = np.zeros(int(0.2 * rate), dtype=np.float32)
    long_pause = np.zeros(int(2.2 * rate), dtype=np.float32)
    audio = np.concatenate([tone, short_pause, tone, long_pause, tone])

    spans, diagnostics = private_corpus.detect_utterances(audio)

    assert len(spans) == 2
    assert diagnostics["threshold"] > 0


def test_prepare_writes_hash_pinned_manifest_without_transcripts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    rate = private_corpus.SAMPLE_RATE
    tone = np.full(int(0.4 * rate), 0.1, dtype=np.float32)
    gap = np.zeros(int(2.2 * rate), dtype=np.float32)
    source = tmp_path / "source.wav"
    sf.write(source, np.concatenate([tone, gap, tone]), rate, subtype="PCM_16")
    monkeypatch.setattr(private_corpus, "PROMPTS", ("最初です。", "次です。"))

    manifest_path = private_corpus.prepare(
        source,
        tmp_path / "corpus",
        overwrite=False,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["privacy"]["training_use"] is False
    assert manifest["privacy"]["raw_transcripts_retained"] is False
    assert manifest["selection"]["prompt_count"] == 2
    assert len(manifest["items"]) == 2
    assert all("wav_sha256" in item for item in manifest["items"])

"""Pure tests for deterministic public corpus preparation."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from tools.prepare_public_asr_corpus import (
    PreparedClip,
    SourceRow,
    _best_crossing_start,
    _compose_to_length,
    choose_rows,
    deterministic_rank,
    feature_tags,
    normalize_reference,
)


def _row(index: int, bin_name: str) -> SourceRow:
    duration = {"short": 2.0, "medium": 4.0, "long": 8.5}[bin_name]
    text = f"評価文{index}"
    encoded = f"audio-{index}".encode()
    return SourceRow(
        row_index=index,
        transcription=text,
        encoded_audio=encoded,
        source_audio_sha256=hashlib.sha256(encoded).hexdigest(),
        original_sample_rate=16_000,
        original_duration_sec=duration,
        duration_bin=bin_name,
        feature_tags=feature_tags(text),
        rank=deterministic_rank(
            revision="a" * 40,
            split="test",
            row_index=index,
            transcription=text,
            seed=7,
        ),
    )


def test_choose_rows_is_deterministic_and_duration_balanced() -> None:
    rows = [
        _row(index, name)
        for index, name in enumerate((["short", "medium", "long"] * 12))
    ]

    first = choose_rows(rows, 30)
    second = choose_rows(reversed(rows), 30)

    assert [row.row_index for row in first] == [
        row.row_index for row in second
    ]
    assert {
        name: sum(row.duration_bin == name for row in first)
        for name in ("short", "medium", "long")
    } == {"short": 10, "medium": 10, "long": 10}


def test_reference_normalization_and_feature_tags() -> None:
    assert normalize_reference(" ＡＳＲ九号。 ") == "ASR九号。"
    assert set(feature_tags("ASR九号カメラ")) == {
        "number",
        "latin",
        "katakana",
        "kanji",
    }


def test_compose_to_length_never_truncates_a_clip() -> None:
    clips = [
        PreparedClip("a", np.ones(16_000, np.float32), "甲", "test:1"),
        PreparedClip("b", np.ones(24_000, np.float32), "乙", "test:2"),
    ]

    audio, reference, recipe = _compose_to_length(
        clips,
        4 * 16_000,
        start_index=0,
        gap_samples=1600,
    )

    assert len(audio) == 4 * 16_000
    assert reference
    assert all(part["end_sample"] <= len(audio) for part in recipe)


def test_crossing_alignment_requires_energy_at_both_boundaries() -> None:
    audio = np.ones(10 * 16_000, np.float32)

    start, energy = _best_crossing_start(audio)

    assert start < 25 * 16_000
    assert energy["25s"] == pytest.approx(1.0)
    assert energy["30s"] == pytest.approx(1.0)

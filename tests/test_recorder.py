"""src.recorder の録音後処理テスト。"""

from __future__ import annotations

import numpy as np

from src.recorder import (
    _audio_level_stats,
    _finalize_recording_audio,
    _is_audio_too_quiet,
    _mono_from_stream_data,
    _resample_chunk,
    _stream_block_samples,
)


def test_finalize_recording_trims_long_trailing_silence() -> None:
    sr = 16000
    audio = np.arange(sr * 12, dtype=np.float32)

    result = _finalize_recording_audio(
        audio,
        sample_rate=sr,
        min_recording_sec=0.5,
        last_speech_sample=sr * 2,
    )

    assert result is not None
    expected_len = int(sr * 2.5)
    assert len(result) == expected_len
    np.testing.assert_array_equal(result, audio[:expected_len])


def test_finalize_recording_keeps_short_trailing_silence() -> None:
    sr = 16000
    audio = np.arange(int(sr * 2.3), dtype=np.float32)

    result = _finalize_recording_audio(
        audio,
        sample_rate=sr,
        min_recording_sec=0.5,
        last_speech_sample=sr * 2,
    )

    assert result is not None
    assert len(result) == len(audio)
    np.testing.assert_array_equal(result, audio)


def test_finalize_recording_trims_leading_and_trailing_silence() -> None:
    sr = 16000
    audio = np.arange(sr * 10, dtype=np.float32)

    result = _finalize_recording_audio(
        audio,
        sample_rate=sr,
        min_recording_sec=0.5,
        first_speech_sample=sr * 2,
        last_speech_sample=sr * 5,
    )

    assert result is not None
    expected_start = sr * 2 - int(round(sr * 0.3))
    expected_end = sr * 5 + int(round(sr * 0.5))
    assert len(result) == expected_end - expected_start
    np.testing.assert_array_equal(result, audio[expected_start:expected_end])


def test_finalize_recording_clamps_leading_trim_to_start() -> None:
    sr = 16000
    audio = np.arange(sr * 2, dtype=np.float32)

    result = _finalize_recording_audio(
        audio,
        sample_rate=sr,
        min_recording_sec=0.5,
        first_speech_sample=int(sr * 0.1),
        last_speech_sample=sr,
    )

    assert result is not None
    expected_end = int(sr * 1.5)
    assert len(result) == expected_end
    np.testing.assert_array_equal(result, audio[:expected_end])


def test_finalize_recording_returns_none_without_detected_speech() -> None:
    sr = 16000
    audio = np.zeros(sr * 2, dtype=np.float32)

    result = _finalize_recording_audio(
        audio,
        sample_rate=sr,
        min_recording_sec=0.5,
        last_speech_sample=None,
    )

    assert result is None


def test_finalize_recording_returns_none_when_trimmed_audio_is_too_short() -> None:
    sr = 16000
    audio = np.arange(sr * 2, dtype=np.float32)

    result = _finalize_recording_audio(
        audio,
        sample_rate=sr,
        min_recording_sec=1.0,
        last_speech_sample=int(sr * 0.1),
    )

    assert result is None


def test_finalize_recording_returns_none_when_leading_trimmed_audio_is_too_short() -> None:
    sr = 16000
    audio = np.arange(sr * 2, dtype=np.float32)

    result = _finalize_recording_audio(
        audio,
        sample_rate=sr,
        min_recording_sec=1.0,
        first_speech_sample=sr,
        last_speech_sample=int(sr * 1.1),
    )

    assert result is None


def test_stream_block_samples_matches_vad_chunk_duration() -> None:
    assert _stream_block_samples(48000, 16000) == 1536


def test_resample_chunk_converts_to_target_rate() -> None:
    source_rate = 48000
    target_rate = 16000
    chunk = np.ones(source_rate // 10, dtype=np.float32)

    result = _resample_chunk(chunk, source_rate, target_rate)

    assert result.dtype == np.float32
    assert len(result) == target_rate // 10


def test_audio_level_gate_rejects_near_silence() -> None:
    audio = np.zeros(16000, dtype=np.float32)

    rms, peak = _audio_level_stats(audio)

    assert rms == 0.0
    assert peak == 0.0
    assert _is_audio_too_quiet(audio, min_rms=0.001, min_peak=0.01)


def test_audio_level_gate_keeps_audible_audio() -> None:
    audio = np.full(16000, 0.02, dtype=np.float32)

    rms, peak = _audio_level_stats(audio)

    assert rms > 0.001
    assert peak > 0.01
    assert not _is_audio_too_quiet(audio, min_rms=0.001, min_peak=0.01)


def test_mono_from_stream_data_downmixes_channels() -> None:
    left = np.zeros(512, dtype=np.float32)
    right = np.full(512, 0.25, dtype=np.float32)
    data = np.column_stack([left, right])

    result = _mono_from_stream_data(data)

    np.testing.assert_array_equal(result, np.full(512, 0.125, dtype=np.float32))

"""src.sounds のテスト。"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from src.sounds import _generate_tone_array, _generate_wav_bytes


class TestGenerateWavBytes:
    """WAV バイト生成のテスト。"""

    def test_returns_bytes(self):
        result = _generate_wav_bytes(440, 100)
        assert isinstance(result, bytes)

    def test_wav_header(self):
        result = _generate_wav_bytes(440, 100)
        # RIFF ヘッダ
        assert result[:4] == b"RIFF"

    def test_different_frequencies(self):
        low = _generate_wav_bytes(220, 100)
        high = _generate_wav_bytes(880, 100)
        # 違う周波数 → 違うデータ
        assert low != high

    def test_different_durations(self):
        short = _generate_wav_bytes(440, 50)
        long = _generate_wav_bytes(440, 200)
        # 長い方がデータサイズが大きい
        assert len(long) > len(short)


class TestGenerateToneArray:
    """float32 トーン配列生成のテスト。"""

    def test_returns_float32_array(self):
        result = _generate_tone_array(440, 100)
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32

    def test_correct_length(self):
        sr = 44100
        duration_ms = 100
        result = _generate_tone_array(440, duration_ms, sample_rate=sr)
        expected_samples = int(sr * duration_ms / 1000)
        assert len(result) == expected_samples

    def test_amplitude_within_range(self):
        result = _generate_tone_array(440, 100)
        # volume 0.5 で生成しているので ±0.5 程度
        assert np.max(np.abs(result)) <= 1.0

    def test_not_all_zeros(self):
        result = _generate_tone_array(440, 100)
        assert np.max(np.abs(result)) > 0.0


class TestSoundPlayerImport:
    """SoundPlayer のインポートテスト。"""

    def test_import(self):
        from src.sounds import SoundPlayer
        assert SoundPlayer is not None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_winsound_available(self):
        import winsound
        assert hasattr(winsound, "PlaySound")

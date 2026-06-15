"""音声フィードバックモジュール。録音開始/停止時にトーンを再生する。

sound_type が "tone" の場合は生成した正弦波、"custom" の場合は指定ファイル（FLAC/WAV等）を再生する。
"""

from __future__ import annotations

import io
import logging
import sys
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from src.config import FeedbackConfig

# winsound は Windows のみ利用可能。Mac では sounddevice で代替する。
if sys.platform == "win32":
    import winsound

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parent.parent


# ── 生成トーン ─────────────────────────────────────────


def _generate_wav_bytes(freq_hz: float, duration_ms: int, sample_rate: int = 44100) -> bytes:
    """指定周波数・長さの正弦波WAVデータをメモリ上で生成する。"""
    n_samples = int(sample_rate * duration_ms / 1000)
    t = np.linspace(0, duration_ms / 1000, n_samples, endpoint=False)
    # フェードイン/アウト（10ms）でクリックノイズを防止
    fade_samples = int(sample_rate * 0.01)
    wave_data = np.sin(2 * np.pi * freq_hz * t)
    if n_samples > fade_samples * 2:
        fade_in = np.linspace(0, 1, fade_samples)
        fade_out = np.linspace(1, 0, fade_samples)
        wave_data[:fade_samples] *= fade_in
        wave_data[-fade_samples:] *= fade_out

    # 16bit PCM に変換
    pcm = (wave_data * 32767 * 0.5).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())

    return buf.getvalue()


def _generate_tone_array(freq_hz: float, duration_ms: int, sample_rate: int = 44100) -> np.ndarray:
    """指定周波数・長さの正弦波を float32 numpy 配列として生成する。"""
    n_samples = int(sample_rate * duration_ms / 1000)
    t = np.linspace(0, duration_ms / 1000, n_samples, endpoint=False)
    fade_samples = int(sample_rate * 0.01)
    wave_data = np.sin(2 * np.pi * freq_hz * t).astype(np.float32)
    if n_samples > fade_samples * 2:
        fade_in = np.linspace(0, 1, fade_samples, dtype=np.float32)
        fade_out = np.linspace(1, 0, fade_samples, dtype=np.float32)
        wave_data[:fade_samples] *= fade_in
        wave_data[-fade_samples:] *= fade_out
    wave_data *= 0.5  # ボリューム調整
    return wave_data


_start_tone: bytes | None = None
_stop_tone: bytes | None = None
_start_tone_array: np.ndarray | None = None
_stop_tone_array: np.ndarray | None = None
_TONE_SAMPLE_RATE = 44100


def _ensure_tones() -> None:
    """トーンデータを遅延初期化する。"""
    global _start_tone, _stop_tone, _start_tone_array, _stop_tone_array
    if _start_tone is None:
        _start_tone = _generate_wav_bytes(880, 150)  # 高めの短いトーン
        _stop_tone = _generate_wav_bytes(440, 200)    # 低めのトーン
    if _start_tone_array is None:
        _start_tone_array = _generate_tone_array(880, 150)
        _stop_tone_array = _generate_tone_array(440, 200)


def _play_tone_sd(tone_array: np.ndarray) -> None:
    """sounddevice でトーンをブロッキング再生する。"""
    sd.play(tone_array, _TONE_SAMPLE_RATE)
    sd.wait()


# ── カスタムサウンド再生 ──────────────────────────────────


def _play_file_async(path: Path, volume: float = 1.0) -> None:
    """soundfile + sounddevice でオーディオファイルを非ブロッキング再生する（fire-and-forget）。"""
    import soundfile as sf

    data, samplerate = sf.read(path, dtype="float32")
    if volume != 1.0:
        data = data * volume
    sd.play(data, samplerate)
    # wait() しない → 再生中でも呼び出し元はすぐ返る


# ── 公開API ──────────────────────────────────────────


class SoundPlayer:
    """設定に基づき開始/停止音を再生するプレイヤー。"""

    def __init__(self, cfg: FeedbackConfig) -> None:
        self._cfg = cfg
        self._volume = max(0.0, min(1.0, cfg.volume))
        self._custom_start: Path | None = None
        self._custom_stop: Path | None = None

        if cfg.sound_type == "custom":
            self._custom_start = _ROOT_DIR / cfg.custom_start_sound
            self._custom_stop = _ROOT_DIR / cfg.custom_stop_sound
            # 起動時にファイル存在チェック
            for p in (self._custom_start, self._custom_stop):
                if not p.exists():
                    logger.warning("カスタムサウンドファイルが見つかりません: %s（toneにフォールバック）", p)
                    self._cfg.sound_type = "tone"
                    break

    def _play_tone(self, tone_type: str) -> None:
        """トーンを再生する。Windows は winsound、Mac は sounddevice を使用。"""
        _ensure_tones()
        if sys.platform == "win32":
            wav_data = _start_tone if tone_type == "start" else _stop_tone
            winsound.PlaySound(wav_data, winsound.SND_MEMORY)
        else:
            tone_array = _start_tone_array if tone_type == "start" else _stop_tone_array
            _play_tone_sd(tone_array)

    def play_start(self) -> None:
        """録音開始音をブロッキングで再生する。"""
        if not self._cfg.sound_enabled:
            return
        try:
            if self._cfg.sound_type == "custom" and self._custom_start is not None:
                _play_file_async(self._custom_start, self._volume)
            else:
                self._play_tone("start")
        except Exception:
            logger.debug("開始音の再生に失敗しました", exc_info=True)

    def play_stop(self) -> None:
        """録音停止音をブロッキングで再生する。"""
        if not self._cfg.sound_enabled:
            return
        try:
            if self._cfg.sound_type == "custom" and self._custom_stop is not None:
                _play_file_async(self._custom_stop, self._volume)
            else:
                self._play_tone("stop")
        except Exception:
            logger.debug("停止音の再生に失敗しました", exc_info=True)

"""録音 + VAD モジュール。sounddevice で録音し、silero-vad で無音検知する。"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from threading import Event, Lock

import numpy as np
import sounddevice as sd
import torch
from scipy.signal import resample_poly

from src.audio_devices import list_input_devices, resolve_input_device
from src.config import RecordingConfig

logger = logging.getLogger(__name__)

# silero-vad のチャンクサイズ（512 samples @ 16kHz = 32ms）
_VAD_CHUNK_SAMPLES = 512
_LEADING_SILENCE_PAD_SEC = 0.3
_TRAILING_SILENCE_PAD_SEC = 0.5

# VAD モデルキャッシュ（初回ロード後は再利用）
_vad_cache_lock = Lock()
_vad_model = None
_vad_utils = None


def _get_vad_model_and_utils():
    """VAD モデルとユーティリティをキャッシュ付きで取得する。"""
    global _vad_model, _vad_utils
    with _vad_cache_lock:
        if _vad_model is None:
            logger.info("silero-vad モデルをロード中...")
            _vad_model, _vad_utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
            )
            logger.info("silero-vad モデルのロードが完了しました")
        return _vad_model, _vad_utils


def preload_vad() -> None:
    """VAD モデルを事前にロードする。アプリ起動時に呼び出すことで、初回録音時の遅延を防ぐ。"""
    _get_vad_model_and_utils()


def _resolve_device(mic: str, sample_rate: int) -> int | None:
    """Backward-compatible helper returning the stream device id."""
    return resolve_input_device(mic, sample_rate).stream_device


def log_available_devices(sample_rate: int | None = None) -> None:
    """利用可能なオーディオデバイスをログに出力する。"""
    logger.info("=== 利用可能なオーディオデバイス ===")
    for device in list_input_devices(sample_rate):
        supported = ""
        if device.supports_sample_rate is not None:
            supported = f", {sample_rate}Hz: {'OK' if device.supports_sample_rate else 'NG'}"
        default_mark = " *default" if device.is_default else ""
        logger.info(
            "  [%d] %s [%s] (入力ch: %d, stream_ch: %d, default_sr: %.0f%s)%s",
            device.index,
            device.name,
            device.hostapi,
            device.max_input_channels,
            device.stream_channels,
            device.default_samplerate,
            supported,
            default_mark,
        )


def _recording_end_sample(
    audio_len: int,
    sample_rate: int,
    last_speech_sample: int | None,
    trailing_pad_sec: float = _TRAILING_SILENCE_PAD_SEC,
) -> int | None:
    """ASR に渡す録音末尾位置を返す。音声未検出なら None。"""
    if last_speech_sample is None:
        return None

    clipped_speech_sample = max(0, min(audio_len, int(last_speech_sample)))
    pad_samples = max(0, int(round(trailing_pad_sec * sample_rate)))
    return min(audio_len, clipped_speech_sample + pad_samples)


def _recording_start_sample(
    audio_len: int,
    sample_rate: int,
    first_speech_sample: int | None,
    leading_pad_sec: float = _LEADING_SILENCE_PAD_SEC,
) -> int | None:
    """ASR に渡す録音開始位置を返す。音声未検出なら None。"""
    if first_speech_sample is None:
        return None

    clipped_speech_sample = max(0, min(audio_len, int(first_speech_sample)))
    pad_samples = max(0, int(round(leading_pad_sec * sample_rate)))
    return max(0, clipped_speech_sample - pad_samples)


def _recording_bounds(
    audio_len: int,
    sample_rate: int,
    first_speech_sample: int | None,
    last_speech_sample: int | None,
    leading_pad_sec: float = _LEADING_SILENCE_PAD_SEC,
    trailing_pad_sec: float = _TRAILING_SILENCE_PAD_SEC,
) -> tuple[int, int] | None:
    """ASR に渡す録音区間を返す。音声未検出なら None。"""
    end_sample = _recording_end_sample(
        audio_len,
        sample_rate,
        last_speech_sample,
        trailing_pad_sec,
    )
    if end_sample is None:
        return None

    start_sample = _recording_start_sample(
        audio_len,
        sample_rate,
        first_speech_sample,
        leading_pad_sec,
    )
    if start_sample is None:
        start_sample = 0
    return min(start_sample, end_sample), end_sample


def _finalize_recording_audio(
    audio: np.ndarray,
    sample_rate: int,
    min_recording_sec: float,
    last_speech_sample: int | None,
    first_speech_sample: int | None = None,
    leading_pad_sec: float = _LEADING_SILENCE_PAD_SEC,
    trailing_pad_sec: float = _TRAILING_SILENCE_PAD_SEC,
) -> np.ndarray | None:
    """録音音声から先頭・末尾無音を除き、ASR に渡す最終音声を返す。"""
    bounds = _recording_bounds(
        len(audio),
        sample_rate,
        first_speech_sample,
        last_speech_sample,
        leading_pad_sec,
        trailing_pad_sec,
    )
    if bounds is None:
        return None

    start_sample, end_sample = bounds
    finalized = audio[start_sample:end_sample]
    duration = len(finalized) / sample_rate
    if duration < min_recording_sec:
        return None
    return finalized


def _speech_event_sample(value: object, sample_rate: int, fallback: int) -> int:
    """VADIterator の秒単位イベント値をサンプル位置へ変換する。"""
    try:
        return int(round(float(value) * sample_rate))
    except (TypeError, ValueError):
        return fallback


def _stream_block_samples(stream_sample_rate: int, target_sample_rate: int) -> int:
    """Return stream frames that correspond to one VAD chunk after resampling."""
    return max(1, int(round(_VAD_CHUNK_SAMPLES * stream_sample_rate / target_sample_rate)))


def _resample_chunk(chunk: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Convert a mono float32 chunk to target sample rate."""
    if source_rate == target_rate:
        return chunk.astype(np.float32, copy=False)
    divisor = math.gcd(source_rate, target_rate)
    converted = resample_poly(chunk, target_rate // divisor, source_rate // divisor)
    return converted.astype(np.float32, copy=False)


def _append_for_vad(buffer: np.ndarray, chunk: np.ndarray) -> np.ndarray:
    if buffer.size == 0:
        return chunk
    return np.concatenate([buffer, chunk])


def _mono_from_stream_data(data: np.ndarray) -> np.ndarray:
    """Downmix input channels to stable mono audio."""
    if data.ndim == 1 or data.shape[1] == 1:
        return data[:, 0] if data.ndim == 2 else data
    return np.mean(data, axis=1, dtype=np.float32)


def _audio_level_stats(audio: np.ndarray) -> tuple[float, float]:
    """Return RMS and peak levels for normalized float audio."""
    if len(audio) == 0:
        return 0.0, 0.0
    audio64 = audio.astype(np.float64, copy=False)
    rms = float(np.sqrt(np.mean(np.square(audio64))))
    peak = float(np.max(np.abs(audio)))
    return rms, peak


def _is_audio_too_quiet(audio: np.ndarray, min_rms: float, min_peak: float) -> bool:
    rms, peak = _audio_level_stats(audio)
    return rms < min_rms and peak < min_peak


def record(
    cfg: RecordingConfig,
    stop_event: Event,
    on_warning: Callable[[str], None] | None = None,
    on_speech_change: Callable[[bool], None] | None = None,
) -> np.ndarray | None:
    """
    録音を実行し、音声データ (float32, 16kHz, mono) を返す。

    停止条件:
    - stop_event が set された（トグルキー再押下）
    - VAD が vad_silence_threshold_sec 以上の無音を検知
    - 最大録音時間 max_recording_sec を超過

    最小録音長 min_recording_sec 未満の場合は None を返す。
    on_warning が指定されている場合、録音時間が上限の warning_pct% を超えた時にコールバックを呼ぶ。
    on_speech_change が指定されている場合、VADの音声検知状態が変化した時にコールバックを呼ぶ（True=音声開始、False=音声終了）。
    """
    resolved_device = resolve_input_device(cfg.microphone, cfg.sample_rate)
    if resolved_device.fallback_used:
        logger.warning(
            "マイク設定をOS既定入力へfallbackします: configured=%s, reason=%s, "
            "default_index=%s, default_name=%s, default_hostapi=%s",
            resolved_device.configured,
            resolved_device.fallback_reason,
            resolved_device.actual_index,
            resolved_device.name,
            resolved_device.hostapi,
        )
        if on_warning is not None and resolved_device.warning_message is not None:
            on_warning(resolved_device.warning_message)

    # silero-vad モデルのロード（キャッシュ済み）
    vad_model, vad_utils = _get_vad_model_and_utils()
    # VADIterator の取得
    # silero-vad v5+ の API: (get_speech_timestamps, _, read_audio, VADIterator, _)
    VADIterator = vad_utils[3]
    vad_iter = VADIterator(
        vad_model,
        threshold=0.5,
        sampling_rate=cfg.sample_rate,
        min_silence_duration_ms=500,
        speech_pad_ms=30,
    )

    audio_chunks: list[np.ndarray] = []
    samples_read = 0
    first_speech_sample: int | None = None
    last_speech_sample: int | None = None
    last_speech_time = time.monotonic()
    in_speech = False  # VADIterator の音声区間内かどうか
    start_time = time.monotonic()
    warning_fired = False
    warning_threshold_sec = cfg.max_recording_sec * cfg.max_recording_warning_pct / 100.0
    vad_buffer = np.empty(0, dtype=np.float32)
    vad_processed_samples = 0
    stream_blocksize = _stream_block_samples(resolved_device.stream_sample_rate, cfg.sample_rate)

    logger.info(
        "録音を開始します (configured=%s, actual_device=%s, name=%s, hostapi=%s, "
        "fallback_used=%s, stream_sr=%d, target_sr=%d, channels=%d)",
        resolved_device.configured or "OS既定",
        resolved_device.actual_index,
        resolved_device.name,
        resolved_device.hostapi,
        resolved_device.fallback_used,
        resolved_device.stream_sample_rate,
        cfg.sample_rate,
        resolved_device.stream_channels,
    )

    try:
        with sd.InputStream(
            samplerate=resolved_device.stream_sample_rate,
            channels=resolved_device.stream_channels,
            dtype="float32",
            device=resolved_device.stream_device,
            blocksize=stream_blocksize,
        ) as stream:
            while not stop_event.is_set():
                data, overflowed = stream.read(stream_blocksize)
                if overflowed:
                    logger.debug("オーディオバッファオーバーフロー")

                stream_chunk = _mono_from_stream_data(data)
                chunk = _resample_chunk(
                    stream_chunk,
                    resolved_device.stream_sample_rate,
                    cfg.sample_rate,
                )
                chunk_end_sample = samples_read + len(chunk)
                samples_read = chunk_end_sample
                audio_chunks.append(chunk.copy())

                # VAD で音声区間を検知
                vad_buffer = _append_for_vad(vad_buffer, chunk)
                while len(vad_buffer) >= _VAD_CHUNK_SAMPLES:
                    vad_chunk = vad_buffer[:_VAD_CHUNK_SAMPLES]
                    vad_buffer = vad_buffer[_VAD_CHUNK_SAMPLES:]
                    vad_processed_samples += len(vad_chunk)

                    vad_tensor = torch.from_numpy(vad_chunk)
                    speech_dict = vad_iter(vad_tensor, return_seconds=True)
                    if speech_dict:
                        if "start" in speech_dict:
                            in_speech = True
                            speech_start_sample = _speech_event_sample(
                                speech_dict["start"],
                                cfg.sample_rate,
                                vad_processed_samples,
                            )
                            if first_speech_sample is None:
                                first_speech_sample = speech_start_sample
                            last_speech_sample = vad_processed_samples
                            if on_speech_change is not None:
                                on_speech_change(True)
                        if "end" in speech_dict:
                            in_speech = False
                            last_speech_sample = _speech_event_sample(
                                speech_dict["end"],
                                cfg.sample_rate,
                                vad_processed_samples,
                            )
                            if on_speech_change is not None:
                                on_speech_change(False)
                        last_speech_time = time.monotonic()
                    elif in_speech:
                        # 音声区間の途中 → 喋り続けているので無音タイマーをリセット
                        last_speech_sample = vad_processed_samples
                        last_speech_time = time.monotonic()

                # 無音タイムアウト
                elapsed_silence = time.monotonic() - last_speech_time
                if elapsed_silence >= cfg.vad_silence_threshold_sec:
                    logger.info("無音 %.1f秒を検知。録音を自動停止します。", elapsed_silence)
                    break

                # 録音時間警告
                elapsed_total = time.monotonic() - start_time
                if not warning_fired and on_warning and elapsed_total >= warning_threshold_sec:
                    remaining = cfg.max_recording_sec - elapsed_total
                    on_warning(f"録音時間の上限まで残り約{remaining:.0f}秒です")
                    warning_fired = True

                # 最大録音時間
                if elapsed_total >= cfg.max_recording_sec:
                    logger.info("最大録音時間 %.0f秒に到達。録音を停止します。", cfg.max_recording_sec)
                    break

    except Exception:
        logger.exception("録音中にエラーが発生しました")
        return None
    finally:
        vad_iter.reset_states()

    if not audio_chunks:
        return None

    raw_audio = np.concatenate(audio_chunks)
    raw_duration = len(raw_audio) / cfg.sample_rate

    bounds = _recording_bounds(
        len(raw_audio),
        cfg.sample_rate,
        first_speech_sample,
        last_speech_sample,
    )
    if bounds is None:
        logger.info("音声が検出されませんでした。録音を破棄します。")
        return None

    start_sample, end_sample = bounds
    leading_trim_sec = start_sample / cfg.sample_rate
    trailing_trim_sec = max(0.0, raw_duration - end_sample / cfg.sample_rate)

    audio = _finalize_recording_audio(
        raw_audio,
        cfg.sample_rate,
        cfg.min_recording_sec,
        last_speech_sample,
        first_speech_sample,
    )
    asr_duration = max(0, end_sample - start_sample) / cfg.sample_rate
    if audio is None:
        logger.info(
            "録音時間 %.2f秒, ASR入力 %.2f秒 < 最小 %.2f秒。破棄します。"
            " 先頭トリム=%.2f秒, 末尾トリム=%.2f秒",
            raw_duration,
            asr_duration,
            cfg.min_recording_sec,
            leading_trim_sec,
            trailing_trim_sec,
        )
        return None

    rms, peak = _audio_level_stats(audio)
    if _is_audio_too_quiet(audio, cfg.min_audio_rms, cfg.min_audio_peak):
        logger.info(
            "録音レベルが低すぎます。ASRに渡さず破棄します: rms=%.6f, peak=%.6f, "
            "min_rms=%.6f, min_peak=%.6f, ASR入力=%.2f秒, 先頭トリム=%.2f秒, "
            "末尾トリム=%.2f秒",
            rms,
            peak,
            cfg.min_audio_rms,
            cfg.min_audio_peak,
            len(audio) / cfg.sample_rate,
            leading_trim_sec,
            trailing_trim_sec,
        )
        return None

    logger.info(
        "録音完了: 実時間=%.2f秒, ASR入力=%.2f秒, 先頭トリム=%.2f秒, "
        "末尾トリム=%.2f秒, rms=%.6f, peak=%.6f",
        raw_duration,
        len(audio) / cfg.sample_rate,
        leading_trim_sec,
        trailing_trim_sec,
        rms,
        peak,
    )
    return audio

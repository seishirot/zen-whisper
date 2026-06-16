"""録音 + VAD モジュール。sounddevice で録音し、sherpa-onnx VAD で無音検知する。"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from src.audio_devices import list_input_devices, resolve_input_device
from src.config import ASR_SAMPLE_RATE, RecordingConfig

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parent.parent
_VAD_MODEL_PATH = _ROOT_DIR / "assets" / "vad" / "silero_vad_16k_op15.onnx"

# Silero VAD config. sherpa-onnx reports the actual runtime window via window_size().
_VAD_CONFIG_WINDOW_SAMPLES = 512
_VAD_THRESHOLD = 0.5
_VAD_MIN_SILENCE_DURATION_SEC = 0.5
_VAD_MIN_SPEECH_DURATION_SEC = 0.25
_LEADING_SILENCE_PAD_SEC = 0.3
_TRAILING_SILENCE_PAD_SEC = 0.5

# VAD モデルキャッシュ（初回ロード後は再利用）
_vad_cache_lock = Lock()
_vad_model = None
_vad_sample_rate: int | None = None


@dataclass(frozen=True)
class _VadUpdate:
    in_speech: bool
    first_speech_sample: int | None
    last_speech_sample: int | None
    started: bool
    ended: bool
    active: bool


def _get_vad_model(sample_rate: int):
    """sherpa-onnx VAD モデルをキャッシュ付きで取得する。"""
    global _vad_model, _vad_sample_rate
    with _vad_cache_lock:
        if _vad_model is None or _vad_sample_rate != sample_rate:
            import sherpa_onnx

            if not _VAD_MODEL_PATH.exists():
                raise FileNotFoundError(f"VAD model file not found: {_VAD_MODEL_PATH}")

            logger.info("sherpa-onnx VAD モデルをロード中: %s", _VAD_MODEL_PATH)
            config = sherpa_onnx.VadModelConfig(
                silero_vad=sherpa_onnx.SileroVadModelConfig(
                    model=str(_VAD_MODEL_PATH),
                    threshold=_VAD_THRESHOLD,
                    min_silence_duration=_VAD_MIN_SILENCE_DURATION_SEC,
                    min_speech_duration=_VAD_MIN_SPEECH_DURATION_SEC,
                    window_size=_VAD_CONFIG_WINDOW_SAMPLES,
                ),
                sample_rate=sample_rate,
                num_threads=1,
                provider="cpu",
            )
            if not config.validate():
                raise ValueError(f"Invalid sherpa-onnx VAD config: {config}")
            _vad_model = sherpa_onnx.VadModel.create(config)
            _vad_sample_rate = sample_rate
            logger.info(
                "sherpa-onnx VAD モデルのロードが完了しました (window=%d)",
                _vad_model.window_size(),
            )
        return _vad_model


def preload_vad() -> None:
    """VAD モデルを事前にロードする。アプリ起動時に呼び出すことで、初回録音時の遅延を防ぐ。"""
    _get_vad_model(ASR_SAMPLE_RATE)


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


def _stream_block_samples(
    stream_sample_rate: int,
    target_sample_rate: int,
    target_samples: int = _VAD_CONFIG_WINDOW_SAMPLES,
) -> int:
    """Return stream frames that correspond to one VAD chunk after resampling."""
    return max(1, int(round(target_samples * stream_sample_rate / target_sample_rate)))


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


def _update_vad_state(
    *,
    is_speech: bool,
    in_speech: bool,
    first_speech_sample: int | None,
    last_speech_sample: int | None,
    processed_samples: int,
    window_size: int,
) -> _VadUpdate:
    started = is_speech and not in_speech
    ended = not is_speech and in_speech
    next_first = first_speech_sample
    next_last = last_speech_sample

    if started:
        next_first = (
            max(0, processed_samples - window_size)
            if next_first is None
            else next_first
        )
        next_last = processed_samples
    elif is_speech:
        next_last = processed_samples
    elif ended:
        next_last = processed_samples

    return _VadUpdate(
        in_speech=is_speech,
        first_speech_sample=next_first,
        last_speech_sample=next_last,
        started=started,
        ended=ended,
        active=is_speech or ended,
    )


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

    vad_model = _get_vad_model(cfg.sample_rate)
    vad_window_size = vad_model.window_size()

    audio_chunks: list[np.ndarray] = []
    samples_read = 0
    first_speech_sample: int | None = None
    last_speech_sample: int | None = None
    last_speech_time = time.monotonic()
    in_speech = False  # sherpa-onnx VAD の音声区間内かどうか
    start_time = time.monotonic()
    warning_fired = False
    warning_threshold_sec = cfg.max_recording_sec * cfg.max_recording_warning_pct / 100.0
    vad_buffer = np.empty(0, dtype=np.float32)
    vad_processed_samples = 0
    stream_blocksize = _stream_block_samples(
        resolved_device.stream_sample_rate,
        cfg.sample_rate,
        vad_window_size,
    )

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
                while len(vad_buffer) >= vad_window_size:
                    vad_chunk = vad_buffer[:vad_window_size]
                    vad_buffer = vad_buffer[vad_window_size:]
                    vad_processed_samples += len(vad_chunk)

                    update = _update_vad_state(
                        is_speech=bool(
                            vad_model.is_speech(
                                vad_chunk.astype(np.float32, copy=False)
                            )
                        ),
                        in_speech=in_speech,
                        first_speech_sample=first_speech_sample,
                        last_speech_sample=last_speech_sample,
                        processed_samples=vad_processed_samples,
                        window_size=vad_window_size,
                    )
                    in_speech = update.in_speech
                    first_speech_sample = update.first_speech_sample
                    last_speech_sample = update.last_speech_sample
                    if update.started and on_speech_change is not None:
                        on_speech_change(True)
                    if update.ended and on_speech_change is not None:
                        on_speech_change(False)
                    if update.active:
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
        vad_model.reset()

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

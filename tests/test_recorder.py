"""src.recorder の録音後処理テスト。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from threading import Event

import numpy as np

import src.recorder as recorder_module
from src.audio_devices import ResolvedInputDevice
from src.config import RecordingConfig
from src.recorder import (
    _VAD_MODEL_PATH,
    _audio_level_stats,
    _finalize_recording_audio,
    _get_vad_model,
    _is_audio_too_quiet,
    _mono_from_stream_data,
    _resample_chunk,
    _stream_block_samples,
    _update_vad_state,
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


def test_stream_block_samples_uses_runtime_vad_window() -> None:
    assert _stream_block_samples(48000, 16000, 576) == 1728


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


def test_import_recorder_does_not_import_torch() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import src.recorder; print('torch' in sys.modules)",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


def test_sherpa_onnx_vad_model_loads_from_bundled_asset() -> None:
    assert _VAD_MODEL_PATH.exists()

    vad = _get_vad_model(16000)
    samples = np.zeros(vad.window_size(), dtype=np.float32)

    assert vad.window_size() > 0
    assert vad.is_speech(samples) is False
    vad.reset()


def test_vad_state_tracks_speech_transitions() -> None:
    window = 576
    state = _update_vad_state(
        is_speech=False,
        in_speech=False,
        first_speech_sample=None,
        last_speech_sample=None,
        processed_samples=window,
        window_size=window,
    )
    assert not state.started
    assert state.first_speech_sample is None
    assert state.last_speech_sample is None

    state = _update_vad_state(
        is_speech=True,
        in_speech=state.in_speech,
        first_speech_sample=state.first_speech_sample,
        last_speech_sample=state.last_speech_sample,
        processed_samples=window * 2,
        window_size=window,
    )
    assert state.started
    assert state.in_speech
    assert state.first_speech_sample == window
    assert state.last_speech_sample == window * 2

    state = _update_vad_state(
        is_speech=True,
        in_speech=state.in_speech,
        first_speech_sample=state.first_speech_sample,
        last_speech_sample=state.last_speech_sample,
        processed_samples=window * 3,
        window_size=window,
    )
    assert not state.started
    assert state.in_speech
    assert state.first_speech_sample == window
    assert state.last_speech_sample == window * 3

    state = _update_vad_state(
        is_speech=False,
        in_speech=state.in_speech,
        first_speech_sample=state.first_speech_sample,
        last_speech_sample=state.last_speech_sample,
        processed_samples=window * 4,
        window_size=window,
    )
    assert state.ended
    assert not state.in_speech
    assert state.first_speech_sample == window
    assert state.last_speech_sample == window * 4


def test_record_wires_sherpa_vad_speech_change_callbacks(monkeypatch) -> None:
    class FakeVad:
        def __init__(self) -> None:
            self.decisions = [False, True, False]
            self.windows: list[np.ndarray] = []
            self.reset_called = False

        def window_size(self) -> int:
            return 4

        def is_speech(self, samples: np.ndarray) -> bool:
            self.windows.append(samples.copy())
            if not self.decisions:
                raise AssertionError("Unexpected extra VAD window")
            return self.decisions.pop(0)

        def reset(self) -> None:
            self.reset_called = True

    stop_event = Event()
    chunks = [
        np.full((4, 1), 0.02, dtype=np.float32),
        np.full((4, 1), 0.02, dtype=np.float32),
        np.full((4, 1), 0.02, dtype=np.float32),
    ]
    fake_vad = FakeVad()

    class FakeInputStream:
        def __init__(self, **kwargs) -> None:
            assert kwargs["samplerate"] == 16000
            assert kwargs["channels"] == 1
            assert kwargs["dtype"] == "float32"
            assert kwargs["blocksize"] == fake_vad.window_size()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self, frames: int):
            assert frames == fake_vad.window_size()
            chunk = chunks.pop(0)
            if not chunks:
                stop_event.set()
            return chunk, False

    monkeypatch.setattr(recorder_module, "_get_vad_model", lambda sample_rate: fake_vad)
    monkeypatch.setattr(
        recorder_module,
        "resolve_input_device",
        lambda mic, sample_rate: ResolvedInputDevice(
            configured=mic,
            stream_device=None,
            actual_index=None,
            name="Fake microphone",
            hostapi="Fake API",
            stream_sample_rate=sample_rate,
            stream_channels=1,
        ),
    )
    monkeypatch.setattr(recorder_module.sd, "InputStream", FakeInputStream)

    events: list[bool] = []
    audio = recorder_module.record(
        RecordingConfig(
            sample_rate=16000,
            vad_silence_threshold_sec=999.0,
            min_recording_sec=0.0,
            min_audio_rms=0.0,
            min_audio_peak=0.0,
            max_recording_sec=999.0,
        ),
        stop_event,
        on_speech_change=events.append,
    )

    assert events == [True, False]
    assert fake_vad.reset_called
    assert len(fake_vad.windows) == 3
    assert audio is not None
    np.testing.assert_array_equal(audio, np.full(12, 0.02, dtype=np.float32))

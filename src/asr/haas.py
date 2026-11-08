# SPDX-License-Identifier: Apache-2.0 AND MIT
# Portions adapted from haas_cpu (Apache-2.0, modified); see THIRD_PARTY_NOTICES.md.
"""Haas-ja-v1.0 CPU backend using the pinned OneCompression ONNX bundle.

The feature extraction, TDT greedy decoding, detokenization and quiet-point
splitting below are adapted from the ``haas_cpu`` package distributed with
kizuna-intelligence/Haas-ja-v1.0-OneCompression-ONNX at commit
e46a4b107ffba60336eb3de9745da6f628cfae35 (Apache-2.0; original model
third-intelligence/Haas-ja-v1.0, Apache-2.0). Changes: integration with the
ZenWhisper model manifest, explicit UTF-8 file reads, no automatic download
from inside inference, no resampling, and empty-input handling. The upstream
LICENSE and NOTICE files are downloaded and verified with the model.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from src.asr.base import RecognitionHints, load_with_timeout
from src.config import ASR_SAMPLE_RATE, HAAS_MAX_CHUNK_SEC, RecognitionConfig
from src.model_provenance import download_verified_snapshot, model_source

logger = logging.getLogger(__name__)

HAAS_PRECISIONS = ("int8", "int4-onecomp")
_COMMON_FILES = (
    "LICENSE",
    "NOTICE",
    "common/decoding.json",
    "common/frontend.json",
    "common/joint.onnx",
    "common/prediction.onnx",
    "common/tokens.json",
)
_MAX_ENCODER_FRAMES = 8000  # upstream encoder export limit (about 80 s)
_MIN_SAMPLES = 320  # the frontend needs at least two 10 ms hops

_haas_available: bool | None = None


def haas_model_files(precision: str) -> tuple[str, ...]:
    """Return the manifest files needed for one encoder precision."""
    if precision not in HAAS_PRECISIONS:
        raise ValueError(f"Unknown Haas precision: {precision}")
    return _COMMON_FILES + (f"{precision}/encoder.onnx", f"{precision}/encoder.onnx.data")


def is_haas_available() -> bool:
    """Return whether the ONNX Runtime needed by Haas is importable."""
    global _haas_available
    if _haas_available is None:
        try:
            _haas_available = importlib.util.find_spec("onnxruntime") is not None
        except Exception as exc:
            logger.warning("Haas の利用可否を確認できません: type=%s", type(exc).__name__)
            _haas_available = False
    return _haas_available


# --- inference mathematics (adapted from haas_cpu) -------------------------


@dataclass(frozen=True)
class _Frontend:
    n_fft: int
    win_length: int
    hop_length: int
    preemph: float
    log_zero_guard: float
    pad_to: int
    window: np.ndarray
    mel_filter: np.ndarray
    std_eps: float = 1e-5


def _load_frontend(path: Path) -> _Frontend:
    c = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "normalize": "per_feature",
        "log_zero_guard_type": "add",
        "mag_power": 2.0,
        "exact_pad": False,
        "frame_splicing": 1,
        "pad_value": 0,
    }
    for name, value in expected.items():
        if c[name] != value:
            raise ValueError(f"Unsupported Haas frontend: {name}={c[name]!r}")
    if (c["sample_rate"], c["n_fft"], c["win_length"], c["hop_length"], c["pad_to"]) != (
        ASR_SAMPLE_RATE, 512, 400, 160, 16,
    ):
        raise ValueError("Unsupported Haas frontend geometry")
    window = np.zeros(c["n_fft"], dtype=np.float64)
    start = (c["n_fft"] - c["win_length"]) // 2
    window[start : start + c["win_length"]] = np.asarray(c["window"], dtype=np.float32)
    return _Frontend(
        n_fft=c["n_fft"],
        win_length=c["win_length"],
        hop_length=c["hop_length"],
        preemph=c["preemph"],
        log_zero_guard=c["log_zero_guard_value"],
        pad_to=c["pad_to"],
        window=window,
        mel_filter=np.asarray(c["mel_filter"], dtype=np.float32),
    )


def _log_mel(frontend: _Frontend, audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic NeMo-compatible log-mel features (no dither)."""
    c = frontend
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1 or not np.isfinite(audio).all():
        raise ValueError("音声は有限の値を持つ1次元配列で指定してください")
    valid = len(audio) // c.hop_length
    if valid < 2:
        raise ValueError("音声が短すぎます（20 ms 未満）")
    x = np.concatenate((audio[:1], audio[1:] - np.float32(c.preemph) * audio[:-1]))
    xp = np.pad(x, (c.n_fft // 2,) * 2)
    frames = np.lib.stride_tricks.sliding_window_view(xp, c.n_fft)[:: c.hop_length]
    spectrum = np.fft.rfft(frames.astype(np.float64) * c.window, n=c.n_fft)
    # Keep the upstream expression so float rounding matches the verified port.
    power = (np.sqrt(spectrum.real**2 + spectrum.imag**2) ** 2).astype(np.float32)
    mel = np.log(c.mel_filter @ power.T + np.float32(c.log_zero_guard))
    mean = mel[:, :valid].mean(1, keepdims=True)
    std = np.sqrt(np.sum((mel[:, :valid] - mean) ** 2, axis=1, keepdims=True) / (valid - 1))
    mel = (mel - mean) / (std + np.float32(c.std_eps))
    mel[:, valid:] = 0
    mel = np.pad(mel, ((0, 0), (0, (-mel.shape[1]) % c.pad_to)))
    return np.ascontiguousarray(mel[None], dtype=np.float32), np.array([valid], dtype=np.int64)


@dataclass(frozen=True)
class _Decoding:
    blank_id: int
    durations: tuple[int, ...]
    max_symbols: int
    num_tokens: int


def _tdt_greedy_decode(
    num_frames: int,
    joint_at: Callable[[int, np.ndarray], np.ndarray],
    predict: Callable[[int, tuple], tuple],
    start: tuple,
    cfg: _Decoding,
) -> list[int]:
    """Batch-one NeMo TDT label-looping greedy decoding."""
    pred, state = start
    t, last_ts, lasts = 0, -1, 0
    out: list[int] = []
    while t < num_frames:
        while True:
            t_label = t
            logits = joint_at(t, pred)
            token = int(np.argmax(logits[: cfg.num_tokens + 1]))
            duration = cfg.durations[int(np.argmax(logits[cfg.num_tokens + 1 :]))]
            if token == cfg.blank_id and duration == 0:
                duration = 1
            t += duration
            if token != cfg.blank_id or t >= num_frames:
                break
        if token == cfg.blank_id:
            break
        out.append(token)
        lasts = lasts + 1 if last_ts == t_label else 1
        last_ts = t_label
        pred, state = predict(token, state)
        if t < num_frames and lasts >= cfg.max_symbols and last_ts == t:
            t += 1
    return out


def _decode_bytes(data: bytearray) -> str:
    result: list[str] = []
    i = 0
    while i < len(data):
        first = data[i]
        size = (
            1 if first < 0x80 else 2 if 0xC2 <= first <= 0xDF
            else 3 if 0xE0 <= first <= 0xEF else 4 if 0xF0 <= first <= 0xF4 else 0
        )
        if size and i + size <= len(data):
            try:
                result.append(data[i : i + size].decode("utf-8", errors="strict"))
                i += size
                continue
            except UnicodeDecodeError:
                pass
        result.append("�")
        i += 1
    return "".join(result)


class _Detokenizer:
    """SentencePiece BPE byte-fallback detokenizer."""

    def __init__(self, pieces: tuple[str, ...], unk_id: int, add_dummy_prefix: bool) -> None:
        self.pieces = pieces
        self.unk_id = unk_id
        self.add_dummy_prefix = add_dummy_prefix

    def decode(self, ids: list[int]) -> str:
        parts: list[str] = []
        pending = bytearray()
        for token in ids:
            piece = self.pieces[token]
            if len(piece) == 6 and piece.startswith("<0x") and piece.endswith(">"):
                pending.append(int(piece[3:5], 16))
                continue
            if pending:
                parts.append(_decode_bytes(pending))
                pending.clear()
            if token == self.unk_id:
                parts.append(" ⁇ ")
            else:
                if not parts and self.add_dummy_prefix and piece.startswith("▁"):
                    piece = piece[1:]
                parts.append(piece.replace("▁", " "))
        if pending:
            parts.append(_decode_bytes(pending))
        return "".join(parts)


def split_at_quiet_points(
    audio: np.ndarray,
    *,
    chunk_seconds: float,
    sample_rate: int = ASR_SAMPLE_RATE,
    search_seconds: float = 2.0,
    window_seconds: float = 0.1,
) -> list[tuple[int, int]]:
    """Split long audio near the quietest 100 ms window around each boundary."""
    step = int(chunk_seconds * sample_rate)
    if step <= 0:
        raise ValueError("chunk_seconds must be positive")
    search = int(search_seconds * sample_rate)
    window = int(window_seconds * sample_rate)
    bounds: list[tuple[int, int]] = []
    start = 0
    while audio.size - start > step:
        target = start + step
        low = max(start + step // 2, target - search)
        high = min(audio.size - window, target + search)
        cut = target
        if high > low:
            segment = audio[low : high + window].astype(np.float64)
            energy = np.cumsum(np.concatenate([[0.0], segment**2]))
            cut = low + int(np.argmin(energy[window:] - energy[:-window])) + window // 2
        bounds.append((start, cut))
        start = cut
    if start < audio.size:
        bounds.append((start, audio.size))
    return bounds


# --- model loading ---------------------------------------------------------


class _HaasModel:
    """Loaded ONNX sessions plus the decoding tables."""

    def __init__(self, snapshot: Path, precision: str, threads: int) -> None:
        import onnxruntime as ort

        def session(path: Path, intra_threads: int):
            options = ort.SessionOptions()
            options.intra_op_num_threads = intra_threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            created = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
            if created.get_providers()[0] != "CPUExecutionProvider":
                raise RuntimeError("Haas ONNX session did not use CPUExecutionProvider")
            return created

        common = snapshot / "common"
        self.frontend = _load_frontend(common / "frontend.json")
        tokens = json.loads((common / "tokens.json").read_text(encoding="utf-8"))
        if not tokens["byte_fallback"] or len(tokens["pieces"]) != 3072:
            raise ValueError("Unsupported Haas token table")
        self.detokenizer = _Detokenizer(
            tuple(tokens["pieces"]), tokens["unk_id"], tokens["add_dummy_prefix"],
        )
        decoding = json.loads((common / "decoding.json").read_text(encoding="utf-8"))
        self.decoding = _Decoding(
            decoding["blank_id"], tuple(decoding["durations"]),
            decoding["max_symbols"], decoding["num_tokens"],
        )
        self.encoder = session(snapshot / precision / "encoder.onnx", threads)
        self.prediction = session(common / "prediction.onnx", 1)
        self.joint = session(common / "joint.onnx", 1)
        empty = (
            np.zeros((2, 1, 5, 64, 64), np.float32),
            np.zeros((2, 1, 3, 320, 4), np.float32),
        )
        self.start = self._predict(self.decoding.blank_id, empty)

    def _predict(self, token: int, state: tuple) -> tuple:
        pred, kda, conv = self.prediction.run(
            ["pred_proj", "kda_state_out", "conv_state_out"],
            {"token": np.array([token], dtype=np.int64), "kda_state": state[0], "conv_state": state[1]},
        )
        return pred, (kda, conv)

    def transcribe(self, audio: np.ndarray) -> str:
        mel, length = _log_mel(self.frontend, audio)
        if mel.shape[2] > _MAX_ENCODER_FRAMES:
            raise ValueError("Haas エンコーダ入力は8000フレーム以下にしてください")
        encoded, encoded_length = self.encoder.run(
            ["enc_proj", "enc_length"], {"mel": mel, "mel_length": length},
        )
        frames = int(encoded_length[0])
        encoded = np.ascontiguousarray(encoded[0, :frames])

        def joint_at(t: int, pred: np.ndarray) -> np.ndarray:
            return self.joint.run(["logits"], {"enc_frame": encoded[t : t + 1], "pred_proj": pred})[0][0]

        ids = _tdt_greedy_decode(frames, joint_at, self._predict, self.start, self.decoding)
        return self.detokenizer.decode(ids).strip()


def _load_pinned_haas_model(
    *,
    precision: str,
    num_threads: int,
    local_files_only: bool = False,
) -> _HaasModel:
    """Download/verify the reviewed snapshot and create CPU ONNX sessions."""
    if num_threads <= 0:
        raise ValueError("num_threads must be positive")
    files = haas_model_files(precision)
    source = model_source("haas_onnx", "ja")
    kwargs: dict[str, object] = {"required_files": files}
    if local_files_only:
        kwargs["local_files_only"] = True
    snapshot = Path(download_verified_snapshot(source, **kwargs))
    logger.info(
        "Haas-ja 実効モデル設定: revision=%s, precision=%s, num_threads=%d",
        source.revision,
        precision,
        num_threads,
    )
    return _HaasModel(snapshot, precision, num_threads)


def _effective_chunk_sec(value: object) -> float:
    """Clamp an invalid config value instead of hanging or exceeding the encoder limit."""
    default = RecognitionConfig().haas_chunk_sec
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not np.isfinite(value):
        logger.warning("haas_chunk_sec が不正なため既定値 %.1f 秒を使用します", default)
        return default
    clamped = min(max(float(value), 1.0), HAAS_MAX_CHUNK_SEC)
    if clamped != value:
        logger.warning("haas_chunk_sec=%s を %.1f 秒に補正します (範囲: 1〜%g)", value, clamped, HAAS_MAX_CHUNK_SEC)
    return clamped


class HaasBackend:
    """CPU backend for Haas-ja-v1.0 (FastConformer-TDT, ONNX Runtime)."""

    name = "haas-ja"

    def __init__(self) -> None:
        self._model: _HaasModel | None = None

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def load(
        self,
        cfg: RecognitionConfig,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        model = load_with_timeout(
            lambda: self._load_haas_model(_load_pinned_haas_model, cfg),
            cfg.model_load_timeout_sec,
            "Haas-ja",
            on_timeout,
        )
        if model is None:
            return
        self._model = model
        logger.info(
            "Haas-ja モデルのロードが完了しました (precision=%s, threads=%d)",
            cfg.haas_precision,
            cfg.haas_inference_threads,
        )

    @staticmethod
    def _load_haas_model(load_model: Callable[..., _HaasModel], cfg: RecognitionConfig) -> _HaasModel:
        return load_model(precision=cfg.haas_precision, num_threads=cfg.haas_inference_threads)

    def unload(self) -> None:
        self._model = None

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
        hints: RecognitionHints | None = None,
    ) -> str:
        model = self._model
        if model is None:
            logger.error("モデルがロードされていません")
            return ""
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size < _MIN_SAMPLES or not np.any(audio):
            return ""
        chunk_sec = _effective_chunk_sec(cfg.haas_chunk_sec)
        bounds = split_at_quiet_points(audio, chunk_seconds=chunk_sec)
        logger.info(
            "Haas-ja 転写条件: precision=%s, threads=%d, chunk_sec=%.1f, chunks=%d, join=none",
            cfg.haas_precision,
            cfg.haas_inference_threads,
            chunk_sec,
            len(bounds),
        )
        parts = [
            model.transcribe(audio[start:end])
            for start, end in bounds
            if end - start >= _MIN_SAMPLES
        ]
        return "".join(parts).strip()

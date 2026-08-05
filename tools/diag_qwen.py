"""Diagnose Qwen3-ASR native model placement and inference timing.

Run from the repository root:
    mise exec -- uv run --locked --extra qwen3-cuda python tools\\diag_qwen.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import soundfile as sf
import torch

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from src.asr.qwen import Qwen3Backend  # noqa: E402
from src.config import (  # noqa: E402
    ENGINE_QWEN3_ASR,
    QWEN3_MODEL_LARGE,
    RecognitionConfig,
)

WAV = _HERE / "samples" / "bench_sample_ja.wav"


def main() -> None:
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is not available")
    print(
        f"torch {torch.__version__} cuda=True "
        f"device={torch.cuda.get_device_name(0)}"
    )

    audio, sample_rate = sf.read(WAV, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        raise SystemExit(f"expected 16k, got {sample_rate}")
    audio = audio[: 16000 * 5]
    print(f"audio (clipped): {len(audio) / 16000:.1f}s")

    cfg = RecognitionConfig(
        engine=ENGINE_QWEN3_ASR,
        device="cuda",
        qwen3_model=QWEN3_MODEL_LARGE,
        qwen3_max_new_tokens=64,
        qwen3_attn_implementation="sdpa",
    )
    backend = Qwen3Backend()

    started = time.perf_counter()
    backend.load(cfg)
    print(f"load {time.perf_counter() - started:.1f}s")

    model = backend._model
    processor = backend._processor
    if model is None or processor is None:
        raise SystemExit("backend did not finish loading")
    parameter = next(model.parameters())
    print(f"model param device={parameter.device} dtype={parameter.dtype}")
    print(f"processor={type(processor).__name__}")

    for run in range(1, 5):
        torch.cuda.synchronize()
        started = time.perf_counter()
        text = backend.transcribe(audio, "ja", cfg)
        torch.cuda.synchronize()
        print(
            f"[run{run}] {time.perf_counter() - started:.2f}s "
            f"chars={len(text)}"
        )

    print(
        f"GPU mem allocated: "
        f"{torch.cuda.memory_allocated() / (1024**3):.2f} GiB"
    )
    backend.unload()


if __name__ == "__main__":
    main()

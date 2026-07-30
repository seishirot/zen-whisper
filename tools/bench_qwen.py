"""Qwen3-ASR native Transformers backend benchmark.

Run from the repository root:
    mise exec -- uv run --extra qwen3-cuda python tools\\bench_qwen.py [sdpa|eager|compile|fa2|all]
"""

from __future__ import annotations

import gc
import sys
import time
import traceback
from pathlib import Path

import numpy as np
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
N_RUNS = 5


def load_audio(path: Path) -> np.ndarray:
    audio, sample_rate = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        raise SystemExit(f"expected 16k, got {sample_rate}")
    return audio


def build_backend(attn: str, compile_enabled: bool) -> tuple[Qwen3Backend, RecognitionConfig]:
    cfg = RecognitionConfig(
        engine=ENGINE_QWEN3_ASR,
        device="cuda",
        qwen3_model=QWEN3_MODEL_LARGE,
        qwen3_max_new_tokens=256,
        qwen3_attn_implementation=attn,
        qwen3_torch_compile=compile_enabled,
    )
    backend = Qwen3Backend()
    backend.load(cfg)
    return backend, cfg


def bench(
    label: str,
    attn: str,
    compile_enabled: bool,
    audio: np.ndarray,
) -> None:
    print(f"\n{'=' * 70}\n[{label}]\n{'=' * 70}")
    duration = len(audio) / 16000
    backend = None
    try:
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        load_started = time.perf_counter()
        backend, cfg = build_backend(attn, compile_enabled)
        torch.cuda.synchronize()
        print(f"  load: {time.perf_counter() - load_started:.1f}s")

        torch.cuda.synchronize()
        warmup_started = time.perf_counter()
        backend.transcribe(audio, "ja", cfg)
        torch.cuda.synchronize()
        warmup_sec = time.perf_counter() - warmup_started
        print(f"  warmup: {warmup_sec:.2f}s  RTF={warmup_sec / duration:.3f}")

        times: list[float] = []
        text = ""
        for _ in range(N_RUNS):
            torch.cuda.synchronize()
            started = time.perf_counter()
            text = backend.transcribe(audio, "ja", cfg)
            torch.cuda.synchronize()
            times.append(time.perf_counter() - started)

        values = np.asarray(times)
        print(
            f"  steady ({N_RUNS} runs): median={np.median(values):.2f}s  "
            f"P95={np.percentile(values, 95):.2f}s  "
            f"RTF(median)={np.median(values) / duration:.3f}"
        )
        print(
            f"  audio={duration:.1f}s  chars={len(text)}  "
            f"peak_vram={torch.cuda.max_memory_allocated() / (1024**2):.0f} MiB"
        )
    except Exception:
        print("  !!! FAILED:")
        traceback.print_exc()
    finally:
        if backend is not None:
            backend.unload()
        del backend
        gc.collect()
        torch.cuda.empty_cache()


def main() -> None:
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is not available")
    print(
        f"torch {torch.__version__}  cuda=True  "
        f"device={torch.cuda.get_device_name(0)}"
    )
    audio = load_audio(WAV)
    print(f"audio: {WAV.name}  {len(audio) / 16000:.1f}s")

    selected = sys.argv[1] if len(sys.argv) > 1 else "all"
    builders = {
        "sdpa": ("Transformers native / SDPA", "sdpa", False),
        "eager": ("Transformers native / eager", "eager", False),
        "compile": ("Transformers native / SDPA + compile", "sdpa", True),
        "fa2": (
            "Transformers native / FlashAttention2",
            "flash_attention_2",
            False,
        ),
    }
    order = ["sdpa", "compile"] if selected == "all" else selected.split(",")
    for key in order:
        if key not in builders:
            raise SystemExit(f"unknown mode: {key}")
        label, attn, compile_enabled = builders[key]
        bench(label, attn, compile_enabled, audio)


if __name__ == "__main__":
    main()

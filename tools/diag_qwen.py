"""Qwen3-ASR 診断: モデルが GPU/bf16 に正しく載っているか、どこが遅いかを切り分ける。

使い方（リポジトリルートから）:
    .venv\\Scripts\\python.exe tools\\diag_qwen.py
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

_HERE = Path(__file__).resolve().parent

MODEL = "Qwen/Qwen3-ASR-1.7B"
WAV = _HERE / "samples" / "bench_sample_ja.wav"


def load_audio(path: Path) -> np.ndarray:
    a, sr = sf.read(path, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    assert sr == 16000, sr
    return a


def main() -> None:
    print(f"torch {torch.__version__} cuda={torch.cuda.is_available()} {torch.cuda.get_device_name(0)}")
    # 5秒に切り詰めて高速に回す
    audio = load_audio(WAV)[: 16000 * 5]
    dur = len(audio) / 16000
    print(f"audio (clipped): {dur:.1f}s")

    from qwen_asr import Qwen3ASRModel

    t0 = time.perf_counter()
    m = Qwen3ASRModel.from_pretrained(
        MODEL, dtype=torch.bfloat16, device_map="cuda",
        attn_implementation="sdpa", max_new_tokens=64,
    )
    print(f"load {time.perf_counter()-t0:.1f}s")

    # モデルが本当に GPU/bf16 か
    inner = m.model
    p = next(inner.parameters())
    print(f"model param device={p.device} dtype={p.dtype}")
    print(f"wrapper backend={m.backend} device={m.device} dtype={m.dtype}")

    # 1回目（warmup）
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    r = m.transcribe(audio=(audio, 16000), language="Japanese")
    torch.cuda.synchronize()
    print(f"\n[run1] {time.perf_counter()-t0:.2f}s  chars={len(r[0].text)}")

    # 2回目以降
    for i in range(3):
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        r = m.transcribe(audio=(audio, 16000), language="Japanese")
        torch.cuda.synchronize()
        print(f"[run{i+2}] {time.perf_counter()-t0:.2f}s  chars={len(r[0].text)}")

    print(f"\nGPU mem allocated: {torch.cuda.memory_allocated()/1e9:.2f} GB")


if __name__ == "__main__":
    main()

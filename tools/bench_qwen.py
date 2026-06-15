"""Qwen3-ASR 推論速度ベンチマーク（Transformers バックエンド・複数設定比較）。

使い方（リポジトリルートから）:
    .venv\\Scripts\\python.exe tools\\bench_qwen.py [sdpa|eager|compile|fa2|all]
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

_HERE = Path(__file__).resolve().parent

MODEL = "Qwen/Qwen3-ASR-1.7B"
WAV = _HERE / "samples" / "bench_sample_ja.wav"
LANG = "Japanese"
N_RUNS = 5


def load_audio(path: Path) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        raise SystemExit(f"expected 16k, got {sr}")
    return audio


def bench(label: str, build_fn, audio: np.ndarray) -> None:
    print(f"\n{'='*70}\n[{label}]\n{'='*70}")
    dur = len(audio) / 16000
    model = None
    try:
        t_load0 = time.perf_counter()
        model = build_fn()
        print(f"  load: {time.perf_counter() - t_load0:.1f}s")

        # warmup（コンパイル/カーネル初回コストを除外）
        t_w0 = time.perf_counter()
        _ = model.transcribe(audio=(audio, 16000), language=LANG)
        warm_s = time.perf_counter() - t_w0
        print(f"  warmup(1st call): {warm_s:.2f}s  RTF={warm_s/dur:.3f}")

        times = []
        text = ""
        for _ in range(N_RUNS):
            t0 = time.perf_counter()
            res = model.transcribe(audio=(audio, 16000), language=LANG)
            times.append(time.perf_counter() - t0)
            text = res[0].text
        arr = np.array(times)
        print(f"  steady ({N_RUNS} runs): mean={arr.mean():.2f}s  min={arr.min():.2f}s  "
              f"RTF(mean)={arr.mean()/dur:.3f}  RTF(min)={arr.min()/dur:.3f}")
        print(f"  audio={dur:.1f}s  chars={len(text)}")
        print(f"  text: {text[:80]}")
    except Exception:
        print("  !!! FAILED:")
        traceback.print_exc()
    finally:
        try:
            del model
        except Exception:
            pass
        import gc

        gc.collect()
        torch.cuda.empty_cache()


def build_sdpa_nocompile():
    from qwen_asr import Qwen3ASRModel

    return Qwen3ASRModel.from_pretrained(
        MODEL, dtype=torch.bfloat16, device_map="cuda",
        attn_implementation="sdpa", max_new_tokens=128,
    )


def build_eager_nocompile():
    from qwen_asr import Qwen3ASRModel

    return Qwen3ASRModel.from_pretrained(
        MODEL, dtype=torch.bfloat16, device_map="cuda",
        attn_implementation="eager", max_new_tokens=128,
    )


def build_current_compile():
    """SDPA + torch.compile(reduce-overhead)。triton 必須（Windows 非対応）。"""
    from qwen_asr import Qwen3ASRModel

    m = Qwen3ASRModel.from_pretrained(
        MODEL, dtype=torch.bfloat16, device_map="cuda", max_new_tokens=128,
    )
    m.model = torch.compile(m.model, mode="reduce-overhead")
    return m


def build_fa2():
    from qwen_asr import Qwen3ASRModel

    return Qwen3ASRModel.from_pretrained(
        MODEL, dtype=torch.bfloat16, device_map="cuda",
        attn_implementation="flash_attention_2", max_new_tokens=128,
    )


def main() -> None:
    print(f"torch {torch.__version__}  cuda={torch.cuda.is_available()}  "
          f"dev={torch.cuda.get_device_name(0)}")
    audio = load_audio(WAV)
    print(f"audio: {WAV.name}  {len(audio)/16000:.1f}s")

    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    builders = {
        "sdpa": ("Transformers / SDPA / no-compile", build_sdpa_nocompile),
        "eager": ("Transformers / eager / no-compile", build_eager_nocompile),
        "compile": ("SDPA + torch.compile(reduce-overhead)", build_current_compile),
        "fa2": ("Transformers / FlashAttention2", build_fa2),
    }
    order = ["sdpa", "compile"] if which == "all" else which.split(",")
    for key in order:
        label, fn = builders[key]
        bench(label, fn, audio)


if __name__ == "__main__":
    main()

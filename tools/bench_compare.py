"""エンジン横断ベンチ: faster-whisper large-v3-turbo vs Qwen3-ASR 1.7B / 0.6B。

速度 (RTF) と精度 (CER: 文字誤り率) を同一音声で比較する。
精度は SAPI 合成音声の入力テキストを正解 (reference) として計測する。

使い方（リポジトリルートから）:
    .venv\\Scripts\\python.exe tools\\bench_compare.py
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
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

WAV = _HERE / "samples" / "bench_sample_ja.wav"
# bench_sample_ja.wav を合成した際の入力テキスト（精度計測の正解）
REFERENCE = (
    "音声認識の処理速度を計測しています。完全ローカルで動作する文字起こしツールは、"
    "音声をクラウドに送信せずにテキスト化できるため、プライバシーを保護しながら"
    "オフラインでも利用できます。今日はとても良い天気なので、散歩に出かけるのが楽しみです。"
)
N_RUNS = 3


def load_audio(path: Path) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        raise SystemExit(f"expected 16k, got {sr}")
    return audio


def _normalize(s: str) -> str:
    """CER 計測用の正規化: 空白と代表的な句読点を除去。"""
    for ch in " \t\n　、。，．,.":
        s = s.replace(ch, "")
    return s


def cer(hyp: str, ref: str) -> float:
    """文字誤り率 (Levenshtein 距離 / 参照長)。"""
    a, b = _normalize(ref), _normalize(hyp)
    if not a:
        return 0.0
    # Levenshtein DP
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1] / len(a)


def _cleanup(model) -> None:
    try:
        del model
    except Exception:
        pass
    import gc

    gc.collect()
    torch.cuda.empty_cache()


def bench_qwen(label: str, model_name: str, audio: np.ndarray) -> None:
    print(f"\n{'='*70}\n[{label}]  ({model_name})\n{'='*70}")
    dur = len(audio) / 16000
    model = None
    try:
        from qwen_asr import Qwen3ASRModel

        t0 = time.perf_counter()
        model = Qwen3ASRModel.from_pretrained(
            model_name, dtype=torch.bfloat16, device_map="cuda",
            attn_implementation="sdpa", max_new_tokens=128,
        )
        print(f"  load: {time.perf_counter()-t0:.1f}s")

        _ = model.transcribe(audio=(audio, 16000), language="Japanese")  # warmup
        times, text = [], ""
        for _ in range(N_RUNS):
            t0 = time.perf_counter()
            res = model.transcribe(audio=(audio, 16000), language="Japanese")
            times.append(time.perf_counter() - t0)
            text = res[0].text
        arr = np.array(times)
        print(f"  RTF(mean)={arr.mean()/dur:.3f}  RTF(min)={arr.min()/dur:.3f}  "
              f"proc(min)={arr.min():.2f}s  audio={dur:.1f}s")
        print(f"  CER={cer(text, REFERENCE):.1%}  chars={len(text)}")
        print(f"  text: {text}")
    except Exception:
        print("  !!! FAILED:")
        traceback.print_exc()
    finally:
        _cleanup(model)


def bench_faster_whisper(audio: np.ndarray) -> None:
    print(f"\n{'='*70}\n[faster-whisper large-v3-turbo]  (float16, beam=5, vad_filter)\n{'='*70}")
    dur = len(audio) / 16000
    model = None
    try:
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
        print(f"  load: {time.perf_counter()-t0:.1f}s")

        def run() -> str:
            segs, _ = model.transcribe(
                audio, language="ja", beam_size=5, vad_filter=True,
                no_speech_threshold=0.6, condition_on_previous_text=False,
            )
            return "".join(s.text for s in segs).strip()

        _ = run()  # warmup
        times, text = [], ""
        for _ in range(N_RUNS):
            t0 = time.perf_counter()
            text = run()
            times.append(time.perf_counter() - t0)
        arr = np.array(times)
        print(f"  RTF(mean)={arr.mean()/dur:.3f}  RTF(min)={arr.min()/dur:.3f}  "
              f"proc(min)={arr.min():.2f}s  audio={dur:.1f}s")
        print(f"  CER={cer(text, REFERENCE):.1%}  chars={len(text)}")
        print(f"  text: {text}")
    except Exception:
        print("  !!! FAILED:")
        traceback.print_exc()
    finally:
        _cleanup(model)


def main() -> None:
    print(f"torch {torch.__version__}  cuda={torch.cuda.is_available()}  "
          f"dev={torch.cuda.get_device_name(0)}")
    audio = load_audio(WAV)
    print(f"audio: {WAV.name}  {len(audio)/16000:.1f}s")
    print(f"reference ({len(_normalize(REFERENCE))} chars): {REFERENCE}")

    bench_faster_whisper(audio)
    bench_qwen("Qwen3-ASR 1.7B", "Qwen/Qwen3-ASR-1.7B", audio)
    bench_qwen("Qwen3-ASR 0.6B", "Qwen/Qwen3-ASR-0.6B", audio)


if __name__ == "__main__":
    main()

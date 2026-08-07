"""統合確認: 実際の Transcriber + RecognitionConfig 経由で Qwen をロード・推論する。

確認ポイント:
  - 末尾が -hf の公式checkpointをnative Transformers APIで使用する
  - attn_implementation が config 経由で渡り、attn=sdpa で動く
  - torch_compile=True でも triton 無し環境（Windows）で安全に無効化され、クラッシュしない

使い方（リポジトリルートから）:
    mise exec -- uv run --locked --extra qwen3-cuda python tools\\verify_qwen_integration.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import soundfile as sf

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from src.config import QWEN3_MODEL_LARGE, RecognitionConfig  # noqa: E402
from src.transcriber import Transcriber  # noqa: E402

WAV = _HERE / "samples" / "bench_sample_ja.wav"

audio, sr = sf.read(WAV, dtype="float32")
if audio.ndim > 1:
    audio = audio.mean(axis=1)
assert sr == 16000

cfg = RecognitionConfig()
cfg.engine = "qwen3-asr"
cfg.device = "cuda"
cfg.qwen3_model = QWEN3_MODEL_LARGE
cfg.qwen3_max_new_tokens = 64
cfg.qwen3_attn_implementation = "sdpa"
cfg.qwen3_torch_compile = True  # わざと True。triton 無しで安全に無効化されることを確認

t = Transcriber()
t.load_model(cfg)
print("is_ready:", t.is_ready)
print("model:", cfg.qwen3_model)
text = t.transcribe(audio[: 16000 * 5], "ja", cfg)
print("RESULT_CHARS:", len(text))
print("OK" if t.is_ready and text else "FAIL")

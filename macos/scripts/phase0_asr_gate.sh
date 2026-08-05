#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"
BACKEND_DIR="$REPO_ROOT/macos/backend"
SAMPLE="${1:-$REPO_ROOT/tools/samples/bench_sample_ja.wav}"
GATE_STAMP="$REPO_ROOT/macos/build/qwen3_asr_gate.ok"

[[ -f "$SAMPLE" ]] || {
  echo "sample wav not found: $SAMPLE" >&2
  exit 1
}

mise exec -- uv sync --locked --project "$BACKEND_DIR" --extra mlx --extra dev
mise exec -- uv run \
  --locked \
  --project "$BACKEND_DIR" \
  --extra mlx \
  --extra dev \
  env PYTHONSAFEPATH=1 python -P - "$SAMPLE" <<'PY'
from pathlib import Path
import sys

audio = Path(sys.argv[1])

from zen_whisper_mac_backend.adapters import _pinned_model_snapshot

import mlx_whisper

whisper_model = "mlx-community/whisper-large-v3-turbo"
whisper_path = _pinned_model_snapshot("mlx-whisper", whisper_model)
whisper_result = mlx_whisper.transcribe(
    str(audio),
    path_or_hf_repo=whisper_path,
    language="ja",
)
whisper_text = whisper_result["text"].strip() if isinstance(whisper_result, dict) else str(whisper_result).strip()
assert whisper_text, "mlx-whisper returned empty text"
print("mlx-whisper ok: chars=", len(whisper_text))

from mlx_audio.stt import load

qwen_model = "mlx-community/Qwen3-ASR-0.6B-8bit"
qwen_path = _pinned_model_snapshot("mlx-qwen3-asr", qwen_model)
model = load(qwen_path)
qwen_result = model.generate(str(audio), language="Japanese")
qwen_text = getattr(qwen_result, "text", None)
if qwen_text is None and isinstance(qwen_result, dict):
    qwen_text = qwen_result.get("text")
if qwen_text is None:
    qwen_text = str(qwen_result)
qwen_text = qwen_text.strip()
assert qwen_text, "mlx-audio Qwen3-ASR returned empty text"
print("mlx-audio qwen ok: chars=", len(qwen_text))
PY

mkdir -p "$(dirname "$GATE_STAMP")"
{
  echo "qwen3_asr_phase0=ok"
  date -u +"created_at=%Y-%m-%dT%H:%M:%SZ"
} > "$GATE_STAMP"

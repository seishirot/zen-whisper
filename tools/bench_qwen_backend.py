"""Compare legacy and Transformers-native Qwen3-ASR backends.

Run this script from separate environments because ``qwen-asr==0.0.6`` pins
Transformers 4.57.6 while the native backend requires Transformers 5.13+.
Keep JSON under the ignored ``tools/bench_outputs`` directory. Real audio and
transcripts are private local artifacts and must not be committed.

Examples:
    python tools/bench_qwen_backend.py --backend legacy --model-size 0.6B
    python tools/bench_qwen_backend.py --backend native --model-size 0.6B
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
_DEFAULT_AUDIO = _HERE / "samples" / "bench_sample_ja.wav"
_MODEL_IDS = {
    ("legacy", "0.6B"): "Qwen/Qwen3-ASR-0.6B",
    ("legacy", "1.7B"): "Qwen/Qwen3-ASR-1.7B",
    ("native", "0.6B"): "Qwen/Qwen3-ASR-0.6B-hf",
    ("native", "1.7B"): "Qwen/Qwen3-ASR-1.7B-hf",
}


def _verified_model_path(
    backend: str,
    model_id: str,
    *,
    local_files_only: bool,
) -> str:
    from src.model_provenance import download_verified_snapshot, model_source

    group = "qwen3_legacy" if backend == "legacy" else "qwen3_hf"
    return download_verified_snapshot(
        model_source(group, model_id),
        local_files_only=local_files_only,
    )


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _load_audio(path: Path) -> tuple[np.ndarray, float, str]:
    audio, sample_rate = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        raise ValueError(f"{path}: expected 16 kHz audio, got {sample_rate}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return np.asarray(audio, dtype=np.float32), len(audio) / sample_rate, digest


def _percentile(values: list[float], percentile: float) -> float:
    if len(values) == 1:
        return values[0]
    return float(np.percentile(np.asarray(values), percentile))


def _native_inputs(
    processor: Any,
    audio: np.ndarray,
    context: str,
    language: str,
) -> Any:
    messages = [
        {"role": "system", "content": context or ""},
        {"role": "user", "content": [{"type": "audio", "audio": ""}]},
    ]
    prompt = processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=False,
    )
    if language:
        prompt += f"language {language}<asr_text>"
    return processor(
        text=[prompt],
        audio=[audio],
        return_tensors="pt",
        padding=True,
    )


def _build_legacy(
    model_id: str,
    local_files_only: bool,
    max_new_tokens: int,
) -> Any:
    from qwen_asr import Qwen3ASRModel

    return Qwen3ASRModel.from_pretrained(
        model_id,
        dtype=torch.bfloat16,
        device_map="cuda",
        attn_implementation="sdpa",
        max_new_tokens=max_new_tokens,
        local_files_only=local_files_only,
    )


def _build_native(model_id: str, local_files_only: bool) -> tuple[Any, Any]:
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    processor = AutoProcessor.from_pretrained(
        model_id,
        local_files_only=local_files_only,
        trust_remote_code=False,
    )
    model = AutoModelForMultimodalLM.from_pretrained(
        model_id,
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        local_files_only=local_files_only,
        trust_remote_code=False,
    )
    model = model.to("cuda").eval()
    return model, processor


def _transcribe_legacy(
    model: Any,
    audio: np.ndarray,
    context: str,
    language: str,
) -> str:
    result = model.transcribe(
        audio=(audio, 16000),
        context=context,
        language=language,
    )
    return result[0].text.strip()


def _transcribe_native(
    model: Any,
    processor: Any,
    audio: np.ndarray,
    context: str,
    language: str,
    max_new_tokens: int,
) -> str:
    inputs = _native_inputs(processor, audio, context, language)
    inputs = inputs.to(model.device, model.dtype)
    with torch.inference_mode():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
        )
    sequences = getattr(output_ids, "sequences", output_ids)
    generated_ids = sequences[:, inputs["input_ids"].shape[1] :]
    decoded = processor.decode(
        generated_ids,
        return_format="transcription_only",
        clean_up_tokenization_spaces=False,
    )
    if isinstance(decoded, str):
        return decoded.strip()
    return decoded[0].strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("legacy", "native"), required=True)
    parser.add_argument("--model-size", choices=("0.6B", "1.7B"), default="0.6B")
    parser.add_argument("--audio", type=Path, default=_DEFAULT_AUDIO)
    parser.add_argument("--context", default="")
    parser.add_argument("--language", default="Japanese")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument(
        "--include-transcript",
        action="store_true",
        help="include transcript text in JSON; never commit private recordings",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.max_new_tokens < 1:
        parser.error("--max-new-tokens must be at least 1")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available")

    audio, duration_sec, audio_sha256 = _load_audio(args.audio)
    model_id = _MODEL_IDS[(args.backend, args.model_size)]
    model_path = _verified_model_path(
        args.backend,
        model_id,
        local_files_only=args.local_files_only,
    )

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    load_started = time.perf_counter()
    processor = None
    if args.backend == "legacy":
        model = _build_legacy(
            model_path,
            True,
            args.max_new_tokens,
        )
    else:
        model, processor = _build_native(model_path, True)
    torch.cuda.synchronize()
    load_sec = time.perf_counter() - load_started

    result: dict[str, Any] = {
        "backend": args.backend,
        "model_id": model_id,
        "audio_name": args.audio.name,
        "audio_sha256": audio_sha256,
        "audio_duration_sec": duration_sec,
        "load_sec": load_sec,
        "gpu": torch.cuda.get_device_name(0),
        "torch": _package_version("torch"),
        "transformers": _package_version("transformers"),
        "qwen_asr": _package_version("qwen-asr"),
        "max_new_tokens": args.max_new_tokens,
    }

    if not args.prepare_only:
        transcribe = (
            (lambda: _transcribe_legacy(
                model, audio, args.context, args.language
            ))
            if args.backend == "legacy"
            else (lambda: _transcribe_native(
                model,
                processor,
                audio,
                args.context,
                args.language,
                args.max_new_tokens,
            ))
        )

        torch.cuda.synchronize()
        warmup_started = time.perf_counter()
        transcript = transcribe()
        torch.cuda.synchronize()
        warmup_sec = time.perf_counter() - warmup_started

        run_times: list[float] = []
        for _ in range(args.runs):
            torch.cuda.synchronize()
            started = time.perf_counter()
            transcript = transcribe()
            torch.cuda.synchronize()
            run_times.append(time.perf_counter() - started)

        result.update(
            {
                "warmup_sec": warmup_sec,
                "run_times_sec": run_times,
                "median_sec": statistics.median(run_times),
                "p95_sec": _percentile(run_times, 95),
                "median_rtf": statistics.median(run_times) / duration_sec,
                "p95_rtf": _percentile(run_times, 95) / duration_sec,
                "peak_vram_mib": torch.cuda.max_memory_allocated() / (1024**2),
                "transcript_chars": len(transcript),
                "transcript_sha256": hashlib.sha256(
                    transcript.encode("utf-8")
                ).hexdigest(),
            }
        )
        if args.include_transcript:
            result["transcript"] = transcript

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

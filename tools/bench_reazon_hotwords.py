r"""ReazonSpeech K2 dynamic-hotword feasibility benchmark.

This script is intentionally independent from the tray app. It compares:

1. The current ReazonSpeech greedy decoder.
2. Modified beam search without hotwords.
3. Modified beam search with per-stream hotwords at one or more scores.

Only the selected model files are downloaded from Hugging Face. This avoids the
upstream ``snapshot_download()`` behavior, which downloads every precision in
the model repository.

PowerShell example:
    mise exec -- uv run --locked --no-sync python tools\bench_reazon_hotwords.py `
      --audio tools\bench_outputs\recording_16k.wav `
      --audio tools\samples\bench_sample_ja.wav `
      --term mise --term uv --term Python `
      --term faster-whisper --term Whisper.cpp
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

DEFAULT_POSITIVE_AUDIO = _HERE / "bench_outputs" / "recording_16k.wav"
DEFAULT_NEGATIVE_AUDIO = _HERE / "samples" / "bench_sample_ja.wav"
DEFAULT_OUTPUT = _HERE / "bench_outputs" / "reazon_hotwords.json"
DEFAULT_SCORES = (0.5, 1.0, 1.5, 2.0)

_MODEL_LAYOUT = {
    "ja": 99,
}


@dataclass(frozen=True)
class ModelFiles:
    tokens: Path
    encoder: Path
    decoder: Path
    joiner: Path


@dataclass
class BenchResult:
    audio: str
    mode: str
    hotword_score: float | None
    elapsed_sec: float
    audio_sec: float
    rtf: float
    matched_terms: list[str]
    text: str


class _PerStreamHotwordRecognizer:
    """Inject dynamic hotwords while preserving upstream transcription logic."""

    def __init__(self, recognizer: Any, hotwords: str) -> None:
        self._recognizer = recognizer
        self._hotwords = hotwords

    def create_stream(self):
        return self._recognizer.create_stream(self._hotwords)

    def decode_stream(self, stream) -> None:
        self._recognizer.decode_stream(stream)


def model_filenames(language: str, precision: str) -> dict[str, str]:
    """Return the pinned ReazonSpeech v2 Hugging Face file layout."""
    try:
        epoch = _MODEL_LAYOUT[language]
    except KeyError as exc:
        raise ValueError(f"unsupported language: {language}") from exc

    base = f"epoch-{epoch}-avg-1"
    if precision == "fp32":
        return {
            "tokens": "tokens.txt",
            "encoder": f"encoder-{base}.onnx",
            "decoder": f"decoder-{base}.onnx",
            "joiner": f"joiner-{base}.onnx",
        }
    if precision == "int8":
        return {
            "tokens": "tokens.txt",
            "encoder": f"encoder-{base}.int8.onnx",
            "decoder": f"decoder-{base}.int8.onnx",
            "joiner": f"joiner-{base}.int8.onnx",
        }
    if precision == "int8-fp32":
        return {
            "tokens": "tokens.txt",
            "encoder": f"encoder-{base}.int8.onnx",
            "decoder": f"decoder-{base}.onnx",
            "joiner": f"joiner-{base}.int8.onnx",
        }
    raise ValueError(f"unsupported precision: {precision}")


def download_model_files(language: str, precision: str) -> ModelFiles:
    """Download only the files needed by one Reazon model configuration."""
    from src.model_provenance import download_verified_snapshot, model_source

    filenames = model_filenames(language, precision)
    snapshot = Path(
        download_verified_snapshot(
            model_source("reazon_k2", language),
            required_files=tuple(filenames.values()),
        )
    )
    paths: dict[str, Path] = {
        role: snapshot / filename for role, filename in filenames.items()
    }
    return ModelFiles(**paths)


def load_token_symbols(path: Path) -> set[str]:
    """Load token symbols from a sherpa ``tokens.txt`` file."""
    symbols: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        symbol, _token_id = line.rsplit(maxsplit=1)
        symbols.add(symbol)
    return symbols


def encode_dynamic_hotwords(terms: list[str], token_symbols: set[str]) -> str:
    """Encode cjkchar hotwords for ``OfflineRecognizer.create_stream()``.

    Dynamic hotwords are already tokenized: tokens are separated by spaces and
    phrases by ``/``. This spike deliberately rejects whitespace in a term
    because the exact output-space token depends on the model vocabulary.
    """
    encoded: list[str] = []
    seen: set[str] = set()
    for raw_term in terms:
        term = unicodedata.normalize("NFC", raw_term.strip())
        if not term or term in seen:
            continue
        if "/" in term:
            raise ValueError(f"hotword contains the phrase delimiter '/': {term!r}")
        if any(char.isspace() for char in term):
            raise ValueError(
                f"hotword contains whitespace, which this spike does not encode: {term!r}"
            )
        missing = sorted({char for char in term if char not in token_symbols})
        if missing:
            chars = ", ".join(repr(char) for char in missing)
            raise ValueError(f"hotword {term!r} has unknown token(s): {chars}")
        encoded.append(" ".join(term))
        seen.add(term)
    if not encoded:
        raise ValueError("at least one non-empty hotword is required")
    return "/".join(encoded)


def load_recognizer(
    files: ModelFiles,
    *,
    decoding_method: str,
    num_threads: int,
    max_active_paths: int,
    hotword_score: float,
    hotwords_file: Path | None = None,
):
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_transducer(
        tokens=str(files.tokens),
        encoder=str(files.encoder),
        decoder=str(files.decoder),
        joiner=str(files.joiner),
        num_threads=num_threads,
        sample_rate=16000,
        feature_dim=80,
        decoding_method=decoding_method,
        max_active_paths=max_active_paths,
        hotwords_file=str(hotwords_file) if hotwords_file is not None else "",
        hotwords_score=hotword_score,
        modeling_unit="cjkchar",
        provider="cpu",
    )


def load_audio_chunks(
    path: Path,
    *,
    chunk_sec: float,
    trailing_silence_sec: float,
) -> tuple[list[np.ndarray], float]:
    """Load 16 kHz mono audio using the same chunk policy as the app."""
    audio, sample_rate = sf.read(path, dtype="float32")
    if sample_rate != 16000:
        raise ValueError(f"{path}: expected 16 kHz audio, got {sample_rate} Hz")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    audio = np.asarray(audio, dtype=np.float32)
    chunk_samples = max(1, int(chunk_sec * sample_rate))
    silence_samples = max(0, int(trailing_silence_sec * sample_rate))
    silence = np.zeros(silence_samples, dtype=np.float32)
    chunks: list[np.ndarray] = []
    for start in range(0, len(audio), chunk_samples):
        chunk = audio[start : start + chunk_samples]
        if silence_samples:
            chunk = np.concatenate([chunk, silence])
        chunks.append(chunk)
    return chunks, len(audio) / sample_rate


def transcribe_chunks(
    recognizer: Any,
    chunks: list[np.ndarray],
    *,
    encoded_hotwords: str | None,
) -> str:
    from reazonspeech.k2.asr import audio_from_numpy, transcribe

    model: Any = recognizer
    if encoded_hotwords is not None:
        model = _PerStreamHotwordRecognizer(recognizer, encoded_hotwords)

    parts: list[str] = []
    for chunk in chunks:
        result = transcribe(model, audio_from_numpy(chunk, 16000))
        text = getattr(result, "text", str(result)).strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def run_mode(
    recognizer: Any,
    audio_paths: list[Path],
    *,
    mode: str,
    hotword_score: float | None,
    encoded_hotwords: str | None,
    terms: list[str],
    chunk_sec: float,
    trailing_silence_sec: float,
) -> list[BenchResult]:
    results: list[BenchResult] = []
    for path in audio_paths:
        chunks, audio_sec = load_audio_chunks(
            path,
            chunk_sec=chunk_sec,
            trailing_silence_sec=trailing_silence_sec,
        )
        started = time.perf_counter()
        text = transcribe_chunks(
            recognizer,
            chunks,
            encoded_hotwords=encoded_hotwords,
        )
        elapsed = time.perf_counter() - started
        folded_text = text.casefold()
        matched = [term for term in terms if term.casefold() in folded_text]
        results.append(
            BenchResult(
                audio=str(path),
                mode=mode,
                hotword_score=hotword_score,
                elapsed_sec=elapsed,
                audio_sec=audio_sec,
                rtf=elapsed / audio_sec if audio_sec else 0.0,
                matched_terms=matched,
                text=text,
            )
        )
    return results


def print_results(results: list[BenchResult]) -> None:
    print()
    print("audio | mode | score | seconds | RTF | matched terms")
    print("------+------|-------|---------|-----|--------------")
    for result in results:
        score = "-" if result.hotword_score is None else f"{result.hotword_score:g}"
        matched = ", ".join(result.matched_terms) or "-"
        print(
            f"{Path(result.audio).name} | {result.mode} | {score} | "
            f"{result.elapsed_sec:.2f} | {result.rtf:.3f} | {matched}"
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare ReazonSpeech K2 decoding with dynamic hotwords.",
    )
    parser.add_argument(
        "--audio",
        action="append",
        type=Path,
        help="16 kHz WAV to test. Repeat to compare positive and negative samples.",
    )
    parser.add_argument(
        "--term",
        action="append",
        required=True,
        help="Canonical hotword spelling. Repeat for multiple terms.",
    )
    parser.add_argument(
        "--score",
        action="append",
        type=float,
        help="Hotword score to test. Repeat for a sweep (default: 0.5,1,1.5,2).",
    )
    parser.add_argument("--language", choices=sorted(_MODEL_LAYOUT), default="ja")
    parser.add_argument(
        "--precision",
        choices=("fp32", "int8", "int8-fp32"),
        default="fp32",
    )
    parser.add_argument("--num-threads", type=int, default=1)
    parser.add_argument("--max-active-paths", type=int, default=4)
    parser.add_argument("--chunk-sec", type=float, default=25.0)
    parser.add_argument("--trailing-silence-sec", type=float, default=0.5)
    parser.add_argument(
        "--include-static",
        action="store_true",
        help="Also test recognizer-load-time hotwords from a generated text file.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    if args.audio is None:
        args.audio = [DEFAULT_POSITIVE_AUDIO, DEFAULT_NEGATIVE_AUDIO]
    if args.score is None:
        args.score = list(DEFAULT_SCORES)
    if args.num_threads <= 0:
        parser.error("--num-threads must be positive")
    if args.max_active_paths <= 0:
        parser.error("--max-active-paths must be positive")
    if args.chunk_sec <= 0:
        parser.error("--chunk-sec must be positive")
    if args.trailing_silence_sec < 0:
        parser.error("--trailing-silence-sec must not be negative")
    if any(score <= 0 for score in args.score):
        parser.error("--score values must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    audio_paths = [path.resolve() for path in args.audio]
    missing_audio = [path for path in audio_paths if not path.is_file()]
    if missing_audio:
        for path in missing_audio:
            print(f"audio file not found: {path}", file=sys.stderr)
        return 2

    files = download_model_files(args.language, args.precision)
    encoded_hotwords = encode_dynamic_hotwords(
        args.term,
        load_token_symbols(files.tokens),
    )
    print(f"dynamic hotwords: {encoded_hotwords}")
    output = args.output.resolve()
    static_hotwords_file: Path | None = None
    if args.include_static:
        static_hotwords_file = output.with_suffix(".hotwords.txt")
        static_hotwords_file.parent.mkdir(parents=True, exist_ok=True)
        static_hotwords_file.write_text(
            encoded_hotwords.replace("/", "\n") + "\n",
            encoding="utf-8",
        )
        print(f"static hotwords: {static_hotwords_file}")

    results: list[BenchResult] = []
    recognizer = load_recognizer(
        files,
        decoding_method="greedy_search",
        num_threads=args.num_threads,
        max_active_paths=args.max_active_paths,
        hotword_score=1.5,
    )
    results.extend(
        run_mode(
            recognizer,
            audio_paths,
            mode="greedy",
            hotword_score=None,
            encoded_hotwords=None,
            terms=args.term,
            chunk_sec=args.chunk_sec,
            trailing_silence_sec=args.trailing_silence_sec,
        )
    )
    del recognizer
    gc.collect()

    for index, score in enumerate(args.score):
        recognizer = load_recognizer(
            files,
            decoding_method="modified_beam_search",
            num_threads=args.num_threads,
            max_active_paths=args.max_active_paths,
            hotword_score=score,
        )
        if index == 0:
            results.extend(
                run_mode(
                    recognizer,
                    audio_paths,
                    mode="modified-beam",
                    hotword_score=None,
                    encoded_hotwords=None,
                    terms=args.term,
                    chunk_sec=args.chunk_sec,
                    trailing_silence_sec=args.trailing_silence_sec,
                )
            )
        results.extend(
            run_mode(
                recognizer,
                audio_paths,
                mode="dynamic-hotwords",
                hotword_score=score,
                encoded_hotwords=encoded_hotwords,
                terms=args.term,
                chunk_sec=args.chunk_sec,
                trailing_silence_sec=args.trailing_silence_sec,
            )
        )
        del recognizer
        gc.collect()
        if static_hotwords_file is not None:
            recognizer = load_recognizer(
                files,
                decoding_method="modified_beam_search",
                num_threads=args.num_threads,
                max_active_paths=args.max_active_paths,
                hotword_score=score,
                hotwords_file=static_hotwords_file,
            )
            results.extend(
                run_mode(
                    recognizer,
                    audio_paths,
                    mode="static-hotwords",
                    hotword_score=score,
                    encoded_hotwords=None,
                    terms=args.term,
                    chunk_sec=args.chunk_sec,
                    trailing_silence_sec=args.trailing_silence_sec,
                )
            )
            del recognizer
            gc.collect()

    print_results(results)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "settings": {
            "language": args.language,
            "precision": args.precision,
            "num_threads": args.num_threads,
            "max_active_paths": args.max_active_paths,
            "chunk_sec": args.chunk_sec,
            "trailing_silence_sec": args.trailing_silence_sec,
            "terms": args.term,
            "encoded_hotwords": encoded_hotwords,
            "static_hotwords_file": (
                str(static_hotwords_file) if static_hotwords_file is not None else None
            ),
            "scores": args.score,
        },
        "results": [asdict(result) for result in results],
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nJSON: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

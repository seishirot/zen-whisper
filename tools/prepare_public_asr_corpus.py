"""Prepare a pinned, local-only public Japanese ASR benchmark corpus.

The command downloads one verified Parquet artifact from the official
Hugging Face dataset repository, selects a deterministic Common Voice subset,
and converts it to 16 kHz mono PCM16 WAV. Audio and references are written
under tools/bench_outputs and therefore stay outside Git.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import soundfile as sf
from huggingface_hub import HfApi, hf_hub_download
from scipy.signal import resample_poly

SAMPLE_RATE = 16_000
DATASET_ID = "japanese-asr/ja_asr.common_voice_8_0"
DATASET_REVISION = "bf8819e8d9a5feb51b0c718686bd20ea67a3c729"
DATASET_SPLIT = "test"
PARQUET_FILE = "data/test-00000-of-00001.parquet"
PARQUET_SIZE = 151_322_876
PARQUET_SHA256 = "44a9141bc16cfa34877955fb39003ad34d3b730417a05c9eb50d8e90ba3ec40a"
SOURCE_LICENSE = "CC0-1.0 (upstream Mozilla Common Voice)"
SOURCE_LICENSE_URL = "https://commonvoice.mozilla.org/terms"
DEFAULT_COUNT = 30
DEFAULT_SEED = 20_260_825
DEFAULT_OUT_DIR = (
    Path(__file__).resolve().parent
    / "bench_outputs"
    / "reazon-production"
    / "corpus"
    / "public"
)
TARGET_DURATIONS = (10, 20, 25, 30, 45, 60)


@dataclass(frozen=True)
class SourceRow:
    row_index: int
    transcription: str
    encoded_audio: bytes
    source_audio_sha256: str
    original_sample_rate: int
    original_duration_sec: float
    duration_bin: str
    feature_tags: tuple[str, ...]
    rank: str


@dataclass(frozen=True)
class PreparedClip:
    item_id: str
    audio: np.ndarray
    reference: str
    source_row_key: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_reference(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def feature_tags(text: str) -> tuple[str, ...]:
    tags: list[str] = []
    if re.search(r"[0-9０-９一二三四五六七八九十百千万億]", text):
        tags.append("number")
    if re.search(r"[A-Za-zＡ-Ｚａ-ｚ]", text):
        tags.append("latin")
    if re.search(r"[ァ-ヶー]", text):
        tags.append("katakana")
    if re.search(r"[一-龯々]", text):
        tags.append("kanji")
    return tuple(tags)


def duration_bin(duration_sec: float) -> str:
    if duration_sec < 3.5:
        return "short"
    if duration_sec < 6.0:
        return "medium"
    return "long"


def deterministic_rank(
    *, revision: str, split: str, row_index: int, transcription: str, seed: int
) -> str:
    value = f"{seed}\0{revision}\0{split}\0{row_index}\0{transcription}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decode_audio(encoded_audio: bytes) -> tuple[np.ndarray, int]:
    audio, sample_rate = sf.read(
        io.BytesIO(encoded_audio),
        dtype="float32",
        always_2d=True,
    )
    mono = np.asarray(audio.mean(axis=1), dtype=np.float32)
    return mono, int(sample_rate)


def convert_audio(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    if sample_rate <= 0:
        raise ValueError("sample rate must be positive")
    if sample_rate != SAMPLE_RATE:
        divisor = math.gcd(sample_rate, SAMPLE_RATE)
        audio = resample_poly(
            audio,
            SAMPLE_RATE // divisor,
            sample_rate // divisor,
        )
    return np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)


def _write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def choose_rows(rows: Iterable[SourceRow], count: int) -> list[SourceRow]:
    if count < DEFAULT_COUNT:
        raise ValueError(f"count must be at least {DEFAULT_COUNT}")
    rows = list(rows)
    if len(rows) < count:
        raise ValueError(f"dataset has only {len(rows)} usable rows")

    base = count // 3
    quotas = {"short": base, "medium": base, "long": count - (2 * base)}
    chosen: list[SourceRow] = []
    chosen_indices: set[int] = set()
    for name in ("short", "medium", "long"):
        candidates = [row for row in rows if row.duration_bin == name]
        candidates.sort(
            key=lambda row: (
                -len(row.feature_tags),
                row.rank,
                row.row_index,
            )
        )
        for row in candidates[: quotas[name]]:
            chosen.append(row)
            chosen_indices.add(row.row_index)

    if len(chosen) < count:
        remaining = sorted(
            (row for row in rows if row.row_index not in chosen_indices),
            key=lambda row: (row.rank, row.row_index),
        )
        chosen.extend(remaining[: count - len(chosen)])
    return sorted(chosen, key=lambda row: (row.rank, row.row_index))


def _compose_to_length(
    clips: list[PreparedClip],
    target_samples: int,
    *,
    start_index: int,
    gap_samples: int,
) -> tuple[np.ndarray, str, list[dict[str, Any]]]:
    parts: list[np.ndarray] = []
    references: list[str] = []
    recipe: list[dict[str, Any]] = []
    cursor = 0
    clip_index = start_index
    attempts = 0
    while cursor < target_samples and attempts < len(clips) * 8:
        clip = clips[clip_index % len(clips)]
        clip_index += 1
        attempts += 1
        gap = gap_samples if parts else 0
        if cursor + gap + len(clip.audio) > target_samples:
            continue
        if gap:
            parts.append(np.zeros(gap, dtype=np.float32))
            cursor += gap
        start = cursor
        parts.append(clip.audio)
        cursor += len(clip.audio)
        references.append(clip.reference)
        recipe.append(
            {
                "source_row_key": clip.source_row_key,
                "start_sample": start,
                "end_sample": cursor,
            }
        )
    if cursor < target_samples:
        parts.append(np.zeros(target_samples - cursor, dtype=np.float32))
    audio = np.concatenate(parts) if parts else np.zeros(target_samples, np.float32)
    return audio, "".join(references), recipe


def _window_rms(audio: np.ndarray, center: int, radius: int) -> float:
    frame = audio[max(0, center - radius) : min(len(audio), center + radius)]
    if len(frame) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))


def _best_crossing_start(audio: np.ndarray) -> tuple[int, dict[str, float]]:
    radius = int(0.12 * SAMPLE_RATE)
    earliest = max(0, int(30.0 * SAMPLE_RATE) - len(audio) + radius)
    latest = min(int(24.75 * SAMPLE_RATE), int(25.0 * SAMPLE_RATE) - radius)
    if earliest > latest:
        raise ValueError("no selected clip is long enough to cross 25s and 30s")
    best_start = earliest
    best_score = -1.0
    best_energy = {"25s": 0.0, "30s": 0.0}
    for start in range(earliest, latest + 1, int(0.02 * SAMPLE_RATE)):
        energy_25 = _window_rms(audio, int(25 * SAMPLE_RATE) - start, radius)
        energy_30 = _window_rms(audio, int(30 * SAMPLE_RATE) - start, radius)
        score = min(energy_25, energy_30)
        if score > best_score:
            best_start = start
            best_score = score
            best_energy = {"25s": energy_25, "30s": energy_30}
    return best_start, best_energy


def build_composites(
    clips: list[PreparedClip], out_dir: Path
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    gap_samples = int(0.15 * SAMPLE_RATE)
    for index, seconds in enumerate(TARGET_DURATIONS):
        audio, reference, recipe = _compose_to_length(
            clips,
            seconds * SAMPLE_RATE,
            start_index=index,
            gap_samples=gap_samples,
        )
        path = out_dir / "duration" / f"public_duration_{seconds:02d}s.wav"
        _write_wav(path, audio)
        items.append(
            {
                "id": f"public-duration-{seconds:02d}s",
                "kind": "duration",
                "audio": str(path.relative_to(out_dir)),
                "reference": reference,
                "target_duration_sec": seconds,
                "wav_sha256": sha256_file(path),
                "composition_recipe": recipe,
            }
        )

    target_samples = 65 * SAMPLE_RATE
    first_length = int(24.6 * SAMPLE_RATE)
    first, first_reference, first_recipe = _compose_to_length(
        clips,
        first_length,
        start_index=7,
        gap_samples=gap_samples,
    )
    silence = np.zeros(int(0.8 * SAMPLE_RATE), dtype=np.float32)
    middle_length = int(4.2 * SAMPLE_RATE)
    middle, middle_reference, middle_recipe = _compose_to_length(
        clips,
        middle_length,
        start_index=11,
        gap_samples=gap_samples,
    )
    suffix_start = int(30.4 * SAMPLE_RATE)
    last, last_reference, last_recipe = _compose_to_length(
        clips,
        target_samples - suffix_start,
        start_index=17,
        gap_samples=gap_samples,
    )
    silence_audio = np.concatenate([first, silence, middle, silence, last])
    silence_reference = first_reference + middle_reference + last_reference
    silence_recipe = first_recipe + [
        {
            **part,
            "start_sample": part["start_sample"] + int(25.4 * SAMPLE_RATE),
            "end_sample": part["end_sample"] + int(25.4 * SAMPLE_RATE),
        }
        for part in middle_recipe
    ] + [
        {
            **part,
            "start_sample": part["start_sample"] + suffix_start,
            "end_sample": part["end_sample"] + suffix_start,
        }
        for part in last_recipe
    ]
    silence_path = out_dir / "boundary" / "public_boundary_silence.wav"
    _write_wav(silence_path, silence_audio)
    items.append(
        {
            "id": "public-boundary-silence",
            "kind": "boundary",
            "audio": str(silence_path.relative_to(out_dir)),
            "reference": silence_reference,
            "wav_sha256": sha256_file(silence_path),
            "boundary_expectation": "explicit silence centered at 25s and 30s",
            "composition_recipe": silence_recipe,
        }
    )

    crossing = max(clips, key=lambda clip: len(clip.audio))
    crossing_start, boundary_energy = _best_crossing_start(crossing.audio)
    prefix, prefix_reference, prefix_recipe = _compose_to_length(
        clips,
        crossing_start,
        start_index=13,
        gap_samples=gap_samples,
    )
    remaining = target_samples - crossing_start - len(crossing.audio)
    suffix, suffix_reference, suffix_recipe = _compose_to_length(
        clips,
        max(0, remaining),
        start_index=19,
        gap_samples=gap_samples,
    )
    speech_audio = np.concatenate([prefix, crossing.audio, suffix])[:target_samples]
    if len(speech_audio) < target_samples:
        speech_audio = np.pad(speech_audio, (0, target_samples - len(speech_audio)))
    speech_path = out_dir / "boundary" / "public_boundary_speech.wav"
    _write_wav(speech_path, speech_audio)
    shifted_suffix = [
        {
            **part,
            "start_sample": part["start_sample"] + crossing_start + len(crossing.audio),
            "end_sample": part["end_sample"] + crossing_start + len(crossing.audio),
        }
        for part in suffix_recipe
    ]
    items.append(
        {
            "id": "public-boundary-speech",
            "kind": "boundary",
            "audio": str(speech_path.relative_to(out_dir)),
            "reference": prefix_reference + crossing.reference + suffix_reference,
            "wav_sha256": sha256_file(speech_path),
            "boundary_expectation": (
                "selected speech has measured energy at 25s and 30s"
            ),
            "boundary_rms": boundary_energy,
            "composition_recipe": prefix_recipe
            + [
                {
                    "source_row_key": crossing.source_row_key,
                    "start_sample": crossing_start,
                    "end_sample": crossing_start + len(crossing.audio),
                    "crosses": ["25s", "30s"],
                }
            ]
            + shifted_suffix,
        }
    )
    return items


def _read_source_rows(
    parquet_path: Path,
    *,
    revision: str,
    split: str,
    seed: int,
) -> list[SourceRow]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:  # pragma: no cover - exercised by CLI environment
        raise RuntimeError(
            "pyarrow is required; sync the locked benchmark dependency group"
        ) from exc
    table = parquet.read_table(parquet_path, columns=["audio", "transcription"])
    audio_column = table.column("audio").combine_chunks()
    text_column = table.column("transcription").combine_chunks()
    rows: list[SourceRow] = []
    for row_index in range(table.num_rows):
        audio_value = audio_column[row_index].as_py()
        transcription = normalize_reference(str(text_column[row_index].as_py()))
        encoded = audio_value.get("bytes") if isinstance(audio_value, dict) else None
        if not encoded or not transcription:
            continue
        audio, sample_rate = decode_audio(encoded)
        duration = len(audio) / sample_rate
        rows.append(
            SourceRow(
                row_index=row_index,
                transcription=transcription,
                encoded_audio=encoded,
                source_audio_sha256=hashlib.sha256(encoded).hexdigest(),
                original_sample_rate=sample_rate,
                original_duration_sec=duration,
                duration_bin=duration_bin(duration),
                feature_tags=feature_tags(transcription),
                rank=deterministic_rank(
                    revision=revision,
                    split=split,
                    row_index=row_index,
                    transcription=transcription,
                    seed=seed,
                ),
            )
        )
    return rows


def prepare_corpus(
    *, out_dir: Path, revision: str, count: int, seed: int, overwrite: bool
) -> Path:
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(
            f"{manifest_path} already exists; pass --overwrite to regenerate"
        )
    api = HfApi()
    info = api.dataset_info(DATASET_ID, revision=revision, files_metadata=True)
    resolved_revision = str(info.sha)
    if revision == DATASET_REVISION and resolved_revision != DATASET_REVISION:
        raise RuntimeError(
            f"resolved revision mismatch: {resolved_revision} != {DATASET_REVISION}"
        )
    sibling = next(
        (entry for entry in info.siblings if entry.rfilename == PARQUET_FILE),
        None,
    )
    if sibling is None or sibling.size != PARQUET_SIZE:
        raise RuntimeError("pinned Parquet file metadata does not match")
    if sibling.lfs is None or sibling.lfs.sha256 != PARQUET_SHA256:
        raise RuntimeError("pinned Parquet LFS SHA-256 does not match")
    cache_dir = out_dir / "_cache"
    parquet_path = Path(
        hf_hub_download(
            repo_id=DATASET_ID,
            repo_type="dataset",
            filename=PARQUET_FILE,
            revision=resolved_revision,
            cache_dir=cache_dir,
        )
    )
    actual_parquet_sha = sha256_file(parquet_path)
    if actual_parquet_sha != PARQUET_SHA256:
        raise RuntimeError(
            f"download SHA-256 mismatch: {actual_parquet_sha} != {PARQUET_SHA256}"
        )

    rows = _read_source_rows(
        parquet_path,
        revision=resolved_revision,
        split=DATASET_SPLIT,
        seed=seed,
    )
    selected = choose_rows(rows, count)
    clips: list[PreparedClip] = []
    items: list[dict[str, Any]] = []
    for ordinal, row in enumerate(selected, 1):
        original_audio, original_rate = decode_audio(row.encoded_audio)
        converted = convert_audio(original_audio, original_rate)
        item_id = f"common-voice-{ordinal:02d}"
        path = out_dir / "clips" / f"{item_id}.wav"
        _write_wav(path, converted)
        source_row_key = f"{DATASET_SPLIT}:{row.row_index:05d}"
        reference = normalize_reference(row.transcription)
        clips.append(
            PreparedClip(
                item_id=item_id,
                audio=converted,
                reference=reference,
                source_row_key=source_row_key,
            )
        )
        items.append(
            {
                "id": item_id,
                "kind": "public",
                "audio": str(path.relative_to(out_dir)),
                "reference": reference,
                "dataset_id": DATASET_ID,
                "dataset_revision": resolved_revision,
                "split": DATASET_SPLIT,
                "source_row_key": source_row_key,
                "source_row_index": row.row_index,
                "source_url": (
                    f"https://huggingface.co/datasets/{DATASET_ID}/blob/"
                    f"{resolved_revision}/{PARQUET_FILE}"
                ),
                "source_license": SOURCE_LICENSE,
                "original_transcription": row.transcription,
                "converted_reference": reference,
                "original_sample_rate": row.original_sample_rate,
                "converted_sample_rate": SAMPLE_RATE,
                "original_duration_sec": row.original_duration_sec,
                "converted_duration_sec": len(converted) / SAMPLE_RATE,
                "source_audio_sha256": row.source_audio_sha256,
                "wav_sha256": sha256_file(path),
                "duration_bin": row.duration_bin,
                "feature_tags": list(row.feature_tags),
            }
        )
    items.extend(build_composites(clips, out_dir))

    counts_by_duration = {
        name: sum(row.duration_bin == name for row in selected)
        for name in ("short", "medium", "long")
    }
    feature_counts = {
        name: sum(name in row.feature_tags for row in selected)
        for name in ("number", "latin", "katakana", "kanji")
    }
    manifest = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": "tools/prepare_public_asr_corpus.py",
        "privacy": (
            "public evaluation audio and references remain local-only under "
            "the gitignored benchmark output directory"
        ),
        "dataset": {
            "id": DATASET_ID,
            "requested_revision": revision,
            "resolved_revision": resolved_revision,
            "split": DATASET_SPLIT,
            "source_license": SOURCE_LICENSE,
            "source_license_url": SOURCE_LICENSE_URL,
            "license_note": (
                "The Hugging Face mirror card omits a license field; the pinned "
                "dataset identifies Common Voice 8.0 and Mozilla publishes "
                "Common Voice under CC0. Data is not redistributed by this tool."
            ),
            "asset": {
                "filename": PARQUET_FILE,
                "url": (
                    f"https://huggingface.co/datasets/{DATASET_ID}/resolve/"
                    f"{resolved_revision}/{PARQUET_FILE}"
                ),
                "size": PARQUET_SIZE,
                "sha256": PARQUET_SHA256,
                "local_path": str(parquet_path),
            },
        },
        "selection": {
            "algorithm": (
                "SHA-256 rank over seed/revision/split/row/transcription, "
                "balanced across short/medium/long duration bins with surface-"
                "feature preference"
            ),
            "seed": seed,
            "requested_count": count,
            "selected_count": len(selected),
            "duration_bin_counts": counts_by_duration,
            "feature_counts": feature_counts,
            "speaker_note": (
                "The pinned mirror exposes only audio and transcription, so "
                "speaker identity is unavailable and speaker diversity cannot "
                "be proven from metadata."
            ),
        },
        "items": items,
    }
    _write_json(manifest_path, manifest)
    return manifest_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and prepare the pinned public Japanese ASR corpus."
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--revision", default=DATASET_REVISION)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if args.count < DEFAULT_COUNT:
        parser.error(f"--count must be at least {DEFAULT_COUNT}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = prepare_corpus(
        out_dir=args.out_dir.resolve(),
        revision=args.revision,
        count=args.count,
        seed=args.seed,
        overwrite=args.overwrite,
    )
    print(f"public corpus manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

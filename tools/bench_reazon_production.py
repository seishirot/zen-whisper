"""Reproducible, local-only Windows CPU ASR measurement for ZenWhisper.

Raw audio, references, and transcripts stay under the gitignored output
directory. Heavy trials run in child processes with configurable RAM and
timeout guards. The normal run path never downloads a model or runtime;
prepare-model is the explicit, hash-verified acquisition command.
"""

from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import soundfile as sf

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from src.asr.reazon import (  # noqa: E402
    _iter_reazon_chunks,
    _load_pinned_reazon_model,
    _reazon_model_files,
)
from src.config import ASR_SAMPLE_RATE, RecognitionConfig, load_config  # noqa: E402
from src.model_provenance import (  # noqa: E402
    download_verified_snapshot,
    model_source,
    resolve_model,
)

DEFAULT_OUT_DIR = _HERE / "bench_outputs" / "reazon-production"
DEFAULT_CORPUS_DIR = DEFAULT_OUT_DIR / "corpus"
DEFAULT_MANIFEST = DEFAULT_CORPUS_DIR / "manifest.json"
DEFAULT_DURATIONS = (10, 20, 25, 30, 45, 60)
DEFAULT_MEMORY_CEILING_MB = 4096
DEFAULT_TIMEOUT_SEC = 600
SCHEMA_VERSION = 1

# Synthetic, non-private prompts. Generated WAVs and the resulting manifest
# remain gitignored. A pinned public corpus may be prepared separately.
CONTROLLED_PROMPTS = (
    "今日の予定を三件、時刻順に並べてください。",
    "保存してからアプリケーションを終了します。",
    "会議は二千二十六年八月二十五日、午後三時開始です。",
    "注文番号は、A、B、C、ハイフン、九、七、二です。",
    "ZenWhisperとReazon K2の動作を確認します。",
    "CPU使用率とメモリ使用量を同時に測定します。",
    "ファイル名は、ベンチ、アンダースコア、結果、ドット、ジェイソンです。",
    "東京都千代田区から大阪府大阪市までの経路を検索します。",
    "音声はクラウドへ送信せず、端末の中だけで処理します。",
    "短い命令です。新しいタブを開いてください。",
    "句読点、数字、英字の混在を、正確に認識できるか試します。",
    "GitのHEADと作業ツリーの差分を記録してください。",
    "モデルのリビジョンとSHA二百五十六を照合します。",
    "第一候補を採用せず、測定結果が出るまで現状を維持します。",
    "静かな区間の近くへ分割境界を移動できるか確認します。",
    "前の文で述べた青い箱を、次の文でも同じ表記で参照します。",
    "青い箱には四十二個の小さな部品が入っています。",
    "株式会社サンプル技研の第七開発部へ連絡してください。",
    "Wi-Fi 6EとUSB Type-Cの表記を比較します。",
    "ゼロ点ゼロ五秒ごとに、プロセスの状態を観測します。",
    "音声認識が失敗した場合は、理由を記録して停止します。",
    "メモリ上限を超えそうなら、後続の長い試行を実行しません。",
    "暖機後の中央値と九十五パーセンタイルを報告します。",
    "最後の文章まで欠落や重複がないことを確認します。",
)


@dataclass(frozen=True)
class CorpusItem:
    item_id: str
    kind: str
    audio: Path
    reference: str
    target_duration_sec: float | None = None
    accepted_without_edit: bool | None = None
    corrected_text: str | None = None
    reference_segments: tuple[dict[str, Any], ...] = ()

    @property
    def duration_sec(self) -> float:
        return len(load_audio(self.audio)) / ASR_SAMPLE_RATE


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_audio(path: Path) -> np.ndarray:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if sample_rate != ASR_SAMPLE_RATE:
        raise ValueError(
            f"{path}: expected {ASR_SAMPLE_RATE} Hz, got {sample_rate} Hz"
        )
    return np.asarray(audio, dtype=np.float32)


def normalize_for_cer(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip()
    ignored = frozenset(" \t\r\n　、。，．,.！？!?「」『』（）()")
    return "".join(char for char in normalized if char not in ignored)


def levenshtein_distance(reference: str, hypothesis: str) -> int:
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, ref_char in enumerate(reference, 1):
        current = [row]
        for column, hyp_char in enumerate(hypothesis, 1):
            current.append(
                min(
                    current[column - 1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (ref_char != hyp_char),
                )
            )
        previous = current
    return previous[-1]


def quality_metrics(reference: str, hypothesis: str) -> dict[str, Any]:
    strict_distance = levenshtein_distance(reference, hypothesis)
    normalized_reference = normalize_for_cer(reference)
    normalized_hypothesis = normalize_for_cer(hypothesis)
    normalized_distance = levenshtein_distance(
        normalized_reference,
        normalized_hypothesis,
    )
    return {
        "strict_edits": strict_distance,
        "strict_cer": strict_distance / max(1, len(reference)),
        "normalized_edits": normalized_distance,
        "normalized_cer": normalized_distance / max(1, len(normalized_reference)),
        "reference_chars": len(reference),
        "hypothesis_chars": len(hypothesis),
    }


def edit_breakdown(reference: str, hypothesis: str) -> dict[str, int]:
    """Return deterministic Levenshtein insertion/deletion/substitution counts."""
    rows = len(reference) + 1
    columns = len(hypothesis) + 1
    costs = [[0] * columns for _ in range(rows)]
    operations = [[""] * columns for _ in range(rows)]
    for row in range(1, rows):
        costs[row][0] = row
        operations[row][0] = "delete"
    for column in range(1, columns):
        costs[0][column] = column
        operations[0][column] = "insert"
    priority = {"equal": 0, "substitute": 1, "delete": 2, "insert": 3}
    for row in range(1, rows):
        for column in range(1, columns):
            candidates = [
                (
                    costs[row - 1][column - 1]
                    + (reference[row - 1] != hypothesis[column - 1]),
                    (
                        "equal"
                        if reference[row - 1] == hypothesis[column - 1]
                        else "substitute"
                    ),
                ),
                (costs[row - 1][column] + 1, "delete"),
                (costs[row][column - 1] + 1, "insert"),
            ]
            cost, operation = min(
                candidates,
                key=lambda candidate: (candidate[0], priority[candidate[1]]),
            )
            costs[row][column] = cost
            operations[row][column] = operation
    counts = {"insertions": 0, "deletions": 0, "substitutions": 0}
    row = len(reference)
    column = len(hypothesis)
    while row or column:
        operation = operations[row][column]
        if operation == "equal":
            row -= 1
            column -= 1
        elif operation == "substitute":
            counts["substitutions"] += 1
            row -= 1
            column -= 1
        elif operation == "delete":
            counts["deletions"] += 1
            row -= 1
        else:
            counts["insertions"] += 1
            column -= 1
    return counts


def quality_with_correction(
    reference: str,
    hypothesis: str,
    *,
    corrected_text: str | None,
    accepted_without_edit: bool | None,
) -> dict[str, Any]:
    metrics = quality_metrics(reference, hypothesis)
    if corrected_text:
        edits = levenshtein_distance(hypothesis, corrected_text)
        metrics["correction_edits"] = edits
        metrics["correction_edits_per_100_chars"] = (
            edits / max(1, len(corrected_text)) * 100
        )
    metrics["accepted_without_edit"] = accepted_without_edit
    return metrics


def fixed_ranges(
    sample_count: int,
    *,
    sample_rate: int = ASR_SAMPLE_RATE,
    chunk_sec: float = 25.0,
) -> list[tuple[int, int]]:
    if sample_count <= 0:
        return []
    chunk_samples = max(1, int(chunk_sec * sample_rate))
    return [
        (start, min(sample_count, start + chunk_samples))
        for start in range(0, sample_count, chunk_samples)
    ]


def silence_aware_ranges(
    audio: np.ndarray,
    *,
    sample_rate: int = ASR_SAMPLE_RATE,
    minimum_sec: float = 24.0,
    maximum_sec: float = 30.0,
    energy_window_sec: float = 0.24,
    hop_sec: float = 0.02,
) -> list[tuple[int, int]]:
    """Split at the lowest-energy point in each configurable 24-30 s window."""
    if len(audio) == 0:
        return []
    if not 0 < minimum_sec <= maximum_sec:
        raise ValueError("expected 0 < minimum_sec <= maximum_sec")
    minimum = max(1, int(minimum_sec * sample_rate))
    maximum = max(minimum, int(maximum_sec * sample_rate))
    half_window = max(1, int(energy_window_sec * sample_rate) // 2)
    hop = max(1, int(hop_sec * sample_rate))
    ranges: list[tuple[int, int]] = []
    start = 0
    while len(audio) - start > maximum:
        search_start = start + minimum
        search_end = min(len(audio), start + maximum)
        candidates = range(search_start, search_end + 1, hop)

        def local_energy(center: int) -> float:
            left = max(start, center - half_window)
            right = min(len(audio), center + half_window)
            frame = audio[left:right]
            return float(np.mean(np.square(frame, dtype=np.float64)))

        boundary = min(candidates, key=local_energy)
        ranges.append((start, boundary))
        start = boundary
    ranges.append((start, len(audio)))
    return ranges


def percentile95(values: Iterable[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[rank]


_LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"\[[A-Z]+\] [^:]+: (?P<message>.*)$"
)
_TRANSCRIBE_LOG_RE = re.compile(
    r"文字起こし完了 \((?P<engine>[^)]+)\): .*?"
    r"音声=(?P<audio>[0-9.]+)秒, 処理=(?P<asr>[0-9.]+)秒, "
    r"RTF=(?P<rtf>[0-9.]+)"
)


def real_usage_summary(path: Path, *, limit: int = 30) -> dict[str, Any]:
    """Aggregate recent stop-to-paste timings without retaining transcripts."""
    if not path.is_file():
        return {"status": "SKIP", "message": "application log is absent"}
    sessions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _LOG_LINE_RE.match(line)
        if not match:
            continue
        timestamp = datetime.strptime(
            match.group("timestamp"),
            "%Y-%m-%d %H:%M:%S,%f",
        )
        message = match.group("message")
        if "録音を停止します" in message:
            current = {"stop_at": timestamp}
            continue
        if current is None:
            continue
        transcribe = _TRANSCRIBE_LOG_RE.search(message)
        if transcribe:
            current.update(
                {
                    "asr_completed_at": timestamp,
                    "engine": transcribe.group("engine"),
                    "audio_sec": float(transcribe.group("audio")),
                    "asr_sec": float(transcribe.group("asr")),
                    "rtf": float(transcribe.group("rtf")),
                }
            )
            continue
        if message.startswith("ペースト完了:") and "asr_completed_at" in current:
            current["paste_at"] = timestamp
            current["end_to_end_sec"] = (
                timestamp - current["stop_at"]
            ).total_seconds()
            current["post_asr_to_paste_sec"] = (
                timestamp - current["asr_completed_at"]
            ).total_seconds()
            sessions.append(current)
            current = None
        elif "録音データなし" in message or "音声が検出されませんでした" in message:
            current = None
    sessions = sessions[-limit:]
    if not sessions:
        return {"status": "SKIP", "message": "no complete stop-to-paste event"}

    def aggregate(name: str) -> dict[str, float]:
        values = [float(session[name]) for session in sessions]
        return {
            "median": statistics.median(values),
            "p95": float(percentile95(values)),
        }

    engines: dict[str, int] = {}
    for session in sessions:
        engine = str(session["engine"])
        engines[engine] = engines.get(engine, 0) + 1
    return {
        "status": "OK",
        "source": str(path),
        "source_sha256": _sha256(path),
        "sample_count": len(sessions),
        "engine_counts": engines,
        "audio_sec": aggregate("audio_sec"),
        "asr_sec": aggregate("asr_sec"),
        "rtf": aggregate("rtf"),
        "end_to_end_sec": aggregate("end_to_end_sec"),
        "post_asr_to_paste_sec": aggregate("post_asr_to_paste_sec"),
        "privacy": "aggregate timing only; no transcript or event timestamp copied",
    }


def _encode_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def synthesize_sapi(text: str, output: Path, voice: str | None = None) -> None:
    if platform.system() != "Windows":
        raise RuntimeError("SAPI corpus generation is Windows-only")
    output.parent.mkdir(parents=True, exist_ok=True)
    text64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    path64 = base64.b64encode(str(output.resolve()).encode("utf-8")).decode("ascii")
    voice64 = base64.b64encode((voice or "").encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{text64}'))
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{path64}'))
$requested = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{voice64}'))
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{
  if ($requested) {{
    $synth.SelectVoice($requested)
  }} else {{
    $ja = $synth.GetInstalledVoices() |
      Where-Object {{ $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'ja-JP' }} |
      Select-Object -First 1
    if (-not $ja) {{ throw 'No enabled ja-JP SAPI voice is installed' }}
    $synth.SelectVoice($ja.VoiceInfo.Name)
  }}
  $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono
  )
  $synth.SetOutputToWaveFile($path, $format)
  $synth.Speak($text)
}} finally {{
  $synth.Dispose()
}}
"""
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            _encode_powershell(script),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "SAPI synthesis failed: "
            + (completed.stderr or completed.stdout).strip()[:500]
        )


def _write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, audio, ASR_SAMPLE_RATE, subtype="PCM_16")


def generate_sapi_corpus(
    corpus_dir: Path,
    *,
    voice: str | None,
    overwrite: bool,
) -> Path:
    manifest_path = corpus_dir / "manifest.json"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(
            f"{manifest_path} already exists; pass --overwrite to regenerate"
        )
    controlled_dir = corpus_dir / "controlled"
    items: list[dict[str, Any]] = []
    rendered: list[tuple[np.ndarray, str]] = []
    for index, prompt in enumerate(CONTROLLED_PROMPTS, 1):
        path = controlled_dir / f"controlled_{index:02d}.wav"
        synthesize_sapi(prompt, path, voice)
        audio = load_audio(path)
        rendered.append((audio, prompt))
        items.append(
            {
                "id": f"controlled-{index:02d}",
                "kind": "controlled",
                "audio": str(path.relative_to(corpus_dir)),
                "reference": prompt,
            }
        )

    duration_dir = corpus_dir / "duration"
    shortest_audio, shortest_text = min(rendered, key=lambda pair: len(pair[0]))
    gap = np.zeros(int(0.15 * ASR_SAMPLE_RATE), dtype=np.float32)
    unit = np.concatenate([shortest_audio, gap])
    for duration in DEFAULT_DURATIONS:
        target_samples = duration * ASR_SAMPLE_RATE
        repetitions = max(1, target_samples // len(unit))
        while repetitions * len(unit) > target_samples:
            repetitions -= 1
        if repetitions <= 0:
            raise RuntimeError("shortest SAPI prompt is longer than 10 seconds")
        audio = np.tile(unit, repetitions)
        audio = np.pad(audio, (0, target_samples - len(audio)))
        reference = shortest_text * repetitions
        path = duration_dir / f"duration_{duration:02d}s.wav"
        _write_wav(path, audio)
        items.append(
            {
                "id": f"duration-{duration:02d}s",
                "kind": "duration",
                "target_duration_sec": duration,
                "audio": str(path.relative_to(corpus_dir)),
                "reference": reference,
            }
        )

    boundary_dir = corpus_dir / "boundary"
    for case_index, offset in enumerate((0, 8), 1):
        pieces: list[np.ndarray] = []
        references: list[str] = []
        total_samples = 0
        cursor = offset
        while total_samples < 62 * ASR_SAMPLE_RATE:
            audio, text = rendered[cursor % len(rendered)]
            silence = np.zeros(int(0.32 * ASR_SAMPLE_RATE), np.float32)
            pieces.extend([audio, silence])
            total_samples += len(audio) + len(silence)
            references.append(text)
            cursor += 1
        combined = np.concatenate(pieces)
        path = boundary_dir / f"boundary_{case_index:02d}.wav"
        _write_wav(path, combined)
        items.append(
            {
                "id": f"boundary-{case_index:02d}",
                "kind": "boundary",
                "audio": str(path.relative_to(corpus_dir)),
                "reference": "".join(references),
            }
        )

    natural_dir = corpus_dir / "natural"
    natural_dir.mkdir(parents=True, exist_ok=True)
    (natural_dir / "README.txt").write_text(
        "Place at least 10 private 16 kHz mono WAV files here and add matching\n"
        "kind=natural entries to manifest.json. Keep references, corrections,\n"
        "and raw transcripts local; this directory is gitignored.\n",
        encoding="utf-8",
    )
    manifest = {
        "version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": "Windows System.Speech SAPI",
        "voice": voice or "first enabled ja-JP voice",
        "privacy": "synthetic controlled corpus; natural dictation remains local-only",
        "items": items,
    }
    _write_json(manifest_path, manifest)
    return manifest_path


def load_corpus_manifest(path: Path) -> list[CorpusItem]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != SCHEMA_VERSION or not isinstance(raw.get("items"), list):
        raise ValueError("unsupported corpus manifest")
    references_by_source = {
        str(entry["source_row_key"]): str(entry["reference"])
        for entry in raw["items"]
        if entry.get("source_row_key") and entry.get("reference")
    }
    items: list[CorpusItem] = []
    seen: set[str] = set()
    for entry in raw["items"]:
        item_id = str(entry.get("id", "")).strip()
        kind = str(entry.get("kind", "")).strip()
        reference = entry.get("reference")
        if not item_id or item_id in seen:
            raise ValueError(f"invalid or duplicate corpus id: {item_id!r}")
        if kind not in {
            "controlled",
            "public",
            "natural",
            "duration",
            "boundary",
        }:
            raise ValueError(f"{item_id}: unsupported kind {kind!r}")
        if not isinstance(reference, str) or not reference:
            raise ValueError(f"{item_id}: reference is required")
        audio = Path(str(entry.get("audio", "")))
        if not audio.is_absolute():
            audio = path.parent / audio
        audio = audio.resolve()
        if not audio.is_file():
            raise ValueError(f"{item_id}: audio file not found: {audio}")
        load_audio(audio)
        reference_segments: list[dict[str, Any]] = []
        for segment in entry.get("composition_recipe", []):
            source_key = str(segment.get("source_row_key", ""))
            if source_key not in references_by_source:
                continue
            start_sample = segment.get("start_sample")
            end_sample = segment.get("end_sample")
            if (
                not isinstance(start_sample, int)
                or not isinstance(end_sample, int)
                or start_sample < 0
                or end_sample <= start_sample
            ):
                raise ValueError(f"{item_id}: invalid composition segment")
            reference_segments.append(
                {
                    "source_row_key": source_key,
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "reference": references_by_source[source_key],
                }
            )
        seen.add(item_id)
        items.append(
            CorpusItem(
                item_id=item_id,
                kind=kind,
                audio=audio,
                reference=reference,
                target_duration_sec=entry.get("target_duration_sec"),
                accepted_without_edit=entry.get("accepted_without_edit"),
                corrected_text=entry.get("corrected_text"),
                reference_segments=tuple(reference_segments),
            )
        )
    return items


def _windows_process_memory(pid: int) -> dict[str, int] | None:
    if platform.system() != "Windows":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(Counters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_bool
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not handle:
        return None
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            ctypes.sizeof(counters),
        ):
            return None
        return {
            "working_set_bytes": int(counters.WorkingSetSize),
            "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
            "private_bytes": int(counters.PrivateUsage),
            "peak_private_bytes": int(counters.PeakPagefileUsage),
        }
    finally:
        kernel32.CloseHandle(handle)


def _windows_thread_count(pid: int) -> int | None:
    if platform.system() != "Windows":
        return None

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ThreadID", ctypes.c_ulong),
            ("th32OwnerProcessID", ctypes.c_ulong),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Thread32First.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ThreadEntry32),
    ]
    kernel32.Thread32First.restype = ctypes.c_bool
    kernel32.Thread32Next.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ThreadEntry32),
    ]
    kernel32.Thread32Next.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return None
    count = 0
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                count += 1
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        return count
    finally:
        kernel32.CloseHandle(snapshot)


class ProcessMonitor:
    def __init__(self, pid: int | None = None, interval_sec: float = 0.05) -> None:
        self.pid = pid or os.getpid()
        self.interval_sec = interval_sec
        self.peak_working_set_bytes = 0
        self.peak_private_bytes = 0
        self.peak_threads = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ProcessMonitor":
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._sample()

    def _sample(self) -> None:
        memory = _windows_process_memory(self.pid)
        if memory:
            self.peak_working_set_bytes = max(
                self.peak_working_set_bytes,
                memory["working_set_bytes"],
                memory["peak_working_set_bytes"],
            )
            self.peak_private_bytes = max(
                self.peak_private_bytes,
                memory["private_bytes"],
                memory["peak_private_bytes"],
            )
        threads = _windows_thread_count(self.pid)
        if threads is not None:
            self.peak_threads = max(self.peak_threads, threads)

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval_sec)


def _loaded_modules() -> list[dict[str, Any]]:
    if platform.system() != "Windows":
        return []
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.EnumProcessModulesEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
    ]
    psapi.EnumProcessModulesEx.restype = ctypes.c_bool
    psapi.GetModuleFileNameExW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
    ]
    psapi.GetModuleFileNameExW.restype = ctypes.c_ulong
    process = kernel32.GetCurrentProcess()
    modules = (ctypes.c_void_p * 2048)()
    needed = ctypes.c_ulong()
    if not psapi.EnumProcessModulesEx(
        process,
        ctypes.byref(modules),
        ctypes.sizeof(modules),
        ctypes.byref(needed),
        0x03,
    ):
        return []
    count = min(len(modules), needed.value // ctypes.sizeof(ctypes.c_void_p))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(count):
        buffer = ctypes.create_unicode_buffer(32768)
        if not psapi.GetModuleFileNameExW(
            process,
            modules[index],
            buffer,
            len(buffer),
        ):
            continue
        path = Path(buffer.value)
        key = str(path).casefold()
        if key in seen or not path.is_file():
            continue
        lower = str(path).casefold()
        if (
            str(_ROOT / ".venv").casefold() not in lower
            and "\\mise\\installs\\python\\" not in lower
            and not any(
                marker in path.name.casefold()
                for marker in ("onnx", "sherpa", "av", "ffmpeg", "ctranslate")
            )
        ):
            continue
        seen.add(key)
        try:
            rows.append(
                {
                    "path": str(path),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                    "signature": "not_collected",
                }
            )
        except OSError as exc:
            rows.append({"path": str(path), "error": type(exc).__name__})
    return rows


def _add_authenticode_status(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows or platform.system() != "Windows":
        return rows
    temporary = tempfile.TemporaryDirectory(prefix="zen_signature_inventory_")
    input_path = Path(temporary.name) / "paths.json"
    output_path = Path(temporary.name) / "signatures.json"
    _write_json(input_path, [row["path"] for row in rows])
    path64 = base64.b64encode(str(input_path).encode("utf-8")).decode("ascii")
    output64 = base64.b64encode(str(output_path).encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{path64}'))
$output = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{output64}'))
$paths = ConvertFrom-Json (Get-Content -Raw -LiteralPath $path)
$result = @($paths | ForEach-Object {{
  $signature = Get-AuthenticodeSignature -LiteralPath $_
  [PSCustomObject]@{{
    path = $_
    status = [string]$signature.Status
    signer = if ($signature.SignerCertificate) {{
      [string]$signature.SignerCertificate.Subject
    }} else {{ "" }}
  }}
}})
$result | ConvertTo-Json -Compress | Set-Content -LiteralPath $output -Encoding UTF8
"""
    try:
        completed = subprocess.run(
            [
                "pwsh.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                _encode_powershell(script),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        temporary.cleanup()
        return rows
    if completed.returncode != 0 or not output_path.is_file():
        temporary.cleanup()
        return rows
    try:
        signatures = json.loads(output_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        temporary.cleanup()
        return rows
    temporary.cleanup()
    if isinstance(signatures, dict):
        signatures = [signatures]
    by_path = {entry["path"].casefold(): entry for entry in signatures}
    for row in rows:
        signature = by_path.get(row["path"].casefold())
        if signature:
            row["signature"] = signature["status"]
            row["signer"] = signature["signer"]
    return rows


def _memory_snapshot() -> dict[str, int | None]:
    memory = _windows_process_memory(os.getpid()) or {}
    return {
        "working_set_bytes": memory.get("working_set_bytes"),
        "private_bytes": memory.get("private_bytes"),
        "process_threads": _windows_thread_count(os.getpid()),
    }


def _chunks_for_strategy(
    audio: np.ndarray,
    *,
    strategy: str,
    chunk_sec: float,
    trailing_silence_sec: float,
) -> list[np.ndarray]:
    ranges = _ranges_for_strategy(
        audio,
        strategy=strategy,
        chunk_sec=chunk_sec,
    )
    if strategy == "single":
        return [audio.astype(np.float32, copy=False)] if len(audio) else []
    if strategy == "fixed":
        config = RecognitionConfig(
            reazon_chunk_sec=chunk_sec,
            reazon_trailing_silence_sec=trailing_silence_sec,
        )
        return list(_iter_reazon_chunks(audio, config))
    silence = np.zeros(
        int(trailing_silence_sec * ASR_SAMPLE_RATE),
        dtype=np.float32,
    )
    chunks = []
    for start, end in ranges:
        chunk = audio[start:end].astype(np.float32, copy=False)
        if len(silence):
            chunk = np.concatenate([chunk, silence])
        chunks.append(chunk)
    return chunks


def _ranges_for_strategy(
    audio: np.ndarray,
    *,
    strategy: str,
    chunk_sec: float,
) -> list[tuple[int, int]]:
    if strategy == "single":
        return [(0, len(audio))] if len(audio) else []
    if strategy == "fixed":
        return fixed_ranges(len(audio), chunk_sec=chunk_sec)
    if strategy == "silence-aware":
        return silence_aware_ranges(audio)
    raise ValueError(f"unknown strategy: {strategy}")


def _reference_for_window(
    segments: Iterable[dict[str, Any]],
    *,
    start_sample: int,
    end_sample: int,
) -> str:
    """Approximate the time-local reference by proportional segment slicing."""
    parts: list[str] = []
    for segment in sorted(segments, key=lambda value: value["start_sample"]):
        segment_start = int(segment["start_sample"])
        segment_end = int(segment["end_sample"])
        text = str(segment["reference"])
        overlap_start = max(start_sample, segment_start)
        overlap_end = min(end_sample, segment_end)
        if overlap_start >= overlap_end or segment_end <= segment_start or not text:
            continue
        length = segment_end - segment_start
        first = math.floor((overlap_start - segment_start) / length * len(text))
        last = math.ceil((overlap_end - segment_start) / length * len(text))
        parts.append(text[first:last])
    return "".join(parts)


def _boundary_local_metrics(
    *,
    ranges: list[tuple[int, int]],
    timed_tokens: list[dict[str, Any]],
    reference_segments: Iterable[dict[str, Any]],
    radius_sec: float = 3.0,
) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    radius_samples = int(radius_sec * ASR_SAMPLE_RATE)
    for _start, boundary_sample in ranges[:-1]:
        window_start = max(0, boundary_sample - radius_samples)
        window_end = boundary_sample + radius_samples
        reference = _reference_for_window(
            reference_segments,
            start_sample=window_start,
            end_sample=window_end,
        )
        hypothesis = "".join(
            str(token["token"])
            for token in timed_tokens
            if window_start / ASR_SAMPLE_RATE
            <= float(token["seconds"])
            <= window_end / ASR_SAMPLE_RATE
        )
        quality = quality_metrics(reference, hypothesis)
        metrics.append(
            {
                "boundary_sec": boundary_sample / ASR_SAMPLE_RATE,
                "window_radius_sec": radius_sec,
                "reference_chars": len(reference),
                "hypothesis_chars": len(hypothesis),
                "hypothesis_sha256": _text_sha256(hypothesis),
                "quality": quality,
                "edits": edit_breakdown(reference, hypothesis),
                "reference_alignment": (
                    "duration-proportional source segment slicing"
                ),
            }
        )
    return metrics


def _worker_reazon(request: dict[str, Any]) -> dict[str, Any]:
    from reazonspeech.k2.asr import audio_from_path, transcribe

    resolved: dict[str, Any] = {}

    def on_resolved(snapshot: Path, files: dict[str, Path]) -> None:
        source = model_source("reazon_k2", "ja")
        resolved.update(
            {
                "repo_id": source.repo_id,
                "revision": source.revision,
                "license": source.license,
                "snapshot": str(snapshot),
                "files": {
                    name: {
                        "path": str(path),
                        "size": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                    for name, path in files.items()
                },
            }
        )

    before_load = _memory_snapshot()
    with ProcessMonitor() as load_monitor:
        load_started = time.perf_counter()
        model = _load_pinned_reazon_model(
            device="cpu",
            precision=request["precision"],
            language="ja",
            num_threads=request["threads"],
            local_files_only=True,
            on_model_resolved=on_resolved,
        )
        load_sec = time.perf_counter() - load_started
    after_load = _memory_snapshot()
    trials: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="zen_reazon_bench_") as temp_dir:
        for run_index in range(request["runs"]):
            for raw_item in request["items"]:
                audio = load_audio(Path(raw_item["audio"]))
                ranges = _ranges_for_strategy(
                    audio,
                    strategy=request["strategy"],
                    chunk_sec=request["chunk_sec"],
                )
                chunks = _chunks_for_strategy(
                    audio,
                    strategy=request["strategy"],
                    chunk_sec=request["chunk_sec"],
                    trailing_silence_sec=request["trailing_silence_sec"],
                )
                parts: list[str] = []
                timed_tokens: list[dict[str, Any]] = []
                started = time.perf_counter()
                with ProcessMonitor() as decode_monitor:
                    for chunk_index, chunk in enumerate(chunks):
                        path = Path(temp_dir) / (
                            f"{raw_item['id']}_{run_index}_{chunk_index}.wav"
                        )
                        _write_wav(path, chunk)
                        result = transcribe(model, audio_from_path(str(path)))
                        text = getattr(result, "text", str(result)).strip()
                        if text:
                            parts.append(text)
                        chunk_start_sec = (
                            ranges[chunk_index][0] / ASR_SAMPLE_RATE
                        )
                        for subword in getattr(result, "subwords", ()):
                            token = str(getattr(subword, "token", ""))
                            if token:
                                timed_tokens.append(
                                    {
                                        "token": token,
                                        "seconds": chunk_start_sec
                                        + float(getattr(subword, "seconds", 0.0)),
                                    }
                                )
                decode_sec = time.perf_counter() - started
                transcript = " ".join(parts).strip()
                duration_sec = len(audio) / ASR_SAMPLE_RATE
                trial = {
                    "item_id": raw_item["id"],
                    "kind": raw_item["kind"],
                    "run_index": run_index + 1,
                    "audio_sec": duration_sec,
                    "decode_sec": decode_sec,
                    "rtf": decode_sec / duration_sec if duration_sec else None,
                    "chunks": len(chunks),
                    "transcript_chars": len(transcript),
                    "transcript_sha256": _text_sha256(transcript),
                    "quality": quality_with_correction(
                        raw_item["reference"],
                        transcript,
                        corrected_text=raw_item.get("corrected_text"),
                        accepted_without_edit=raw_item.get("accepted_without_edit"),
                    ),
                    "peak_working_set_bytes": decode_monitor.peak_working_set_bytes,
                    "peak_private_bytes": decode_monitor.peak_private_bytes,
                    "peak_process_threads": decode_monitor.peak_threads,
                    "after_decode": _memory_snapshot(),
                    "boundary_local": _boundary_local_metrics(
                        ranges=ranges,
                        timed_tokens=timed_tokens,
                        reference_segments=raw_item.get(
                            "reference_segments",
                            (),
                        ),
                    ),
                }
                private_dir = request.get("private_transcript_dir")
                if private_dir:
                    private_path = Path(private_dir) / (
                        f"{raw_item['id']}_{request['strategy']}_"
                        f"t{request['threads']}_r{run_index + 1}.txt"
                    )
                    private_path.parent.mkdir(parents=True, exist_ok=True)
                    private_path.write_text(transcript + "\n", encoding="utf-8")
                    trial["private_transcript_path"] = str(private_path)
                trials.append(trial)
    return {
        "status": "OK",
        "engine": "reazon-k2",
        "precision": request["precision"],
        "configured_inference_threads": request["threads"],
        "strategy": request["strategy"],
        "chunk_sec": request["chunk_sec"],
        "trailing_silence_sec": request["trailing_silence_sec"],
        "load_sec": load_sec,
        "before_load": before_load,
        "after_load": after_load,
        "load_peak_working_set_bytes": load_monitor.peak_working_set_bytes,
        "load_peak_private_bytes": load_monitor.peak_private_bytes,
        "load_peak_process_threads": load_monitor.peak_threads,
        "model": resolved,
        "loaded_modules": _loaded_modules(),
        "trials": trials,
    }


def _worker_faster_whisper(request: dict[str, Any]) -> dict[str, Any]:
    from faster_whisper import WhisperModel

    model_name = request.get("model_name", "large-v3-turbo")
    resolved = resolve_model(
        "faster_whisper",
        model_name,
        aliases={"turbo": "large-v3-turbo"},
    )
    assert resolved.model_source is not None
    snapshot = Path(
        download_verified_snapshot(
            resolved.model_source,
            local_files_only=True,
        )
    )
    before_load = _memory_snapshot()
    with ProcessMonitor() as load_monitor:
        started = time.perf_counter()
        model = WhisperModel(
            str(snapshot),
            device="cpu",
            compute_type="int8",
            cpu_threads=request["threads"],
            num_workers=1,
            local_files_only=True,
        )
        load_sec = time.perf_counter() - started
    after_load = _memory_snapshot()
    trials = []
    for run_index in range(request["runs"]):
        for raw_item in request["items"]:
            audio = load_audio(Path(raw_item["audio"]))
            started = time.perf_counter()
            with ProcessMonitor() as decode_monitor:
                transcribe_kwargs = {
                    "language": "ja",
                    "task": "transcribe",
                    "beam_size": 1,
                    "vad_filter": False,
                    "condition_on_previous_text": request[
                        "condition_on_previous_text"
                    ],
                }
                if request.get("chunk_length") is not None:
                    transcribe_kwargs["chunk_length"] = request["chunk_length"]
                segments, _info = model.transcribe(
                    str(raw_item["audio"]),
                    **transcribe_kwargs,
                )
                transcript = "".join(segment.text for segment in segments).strip()
            decode_sec = time.perf_counter() - started
            duration_sec = len(audio) / ASR_SAMPLE_RATE
            trials.append(
                {
                    "item_id": raw_item["id"],
                    "kind": raw_item["kind"],
                    "run_index": run_index + 1,
                    "audio_sec": duration_sec,
                    "decode_sec": decode_sec,
                    "rtf": decode_sec / duration_sec if duration_sec else None,
                    "chunks": None,
                    "transcript_chars": len(transcript),
                    "transcript_sha256": _text_sha256(transcript),
                    "quality": quality_with_correction(
                        raw_item["reference"],
                        transcript,
                        corrected_text=raw_item.get("corrected_text"),
                        accepted_without_edit=raw_item.get("accepted_without_edit"),
                    ),
                    "peak_working_set_bytes": decode_monitor.peak_working_set_bytes,
                    "peak_private_bytes": decode_monitor.peak_private_bytes,
                    "peak_process_threads": decode_monitor.peak_threads,
                    "after_decode": _memory_snapshot(),
                }
            )
    return {
        "status": "OK",
        "engine": request.get("engine_label", "faster-whisper"),
        "precision": "int8",
        "configured_inference_threads": request["threads"],
        "condition_on_previous_text": request["condition_on_previous_text"],
        "strategy": "runtime-windowing",
        "load_sec": load_sec,
        "before_load": before_load,
        "after_load": after_load,
        "load_peak_working_set_bytes": load_monitor.peak_working_set_bytes,
        "load_peak_private_bytes": load_monitor.peak_private_bytes,
        "load_peak_process_threads": load_monitor.peak_threads,
        "model": {
            "repo_id": resolved.model_source.repo_id,
            "revision": resolved.model_source.revision,
            "license": resolved.model_source.license,
            "snapshot": str(snapshot),
        },
        "loaded_modules": _loaded_modules(),
        "trials": trials,
    }


def run_worker(request_path: Path, result_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        if request["engine"] == "reazon-k2":
            result = _worker_reazon(request)
        elif request["engine"] == "faster-whisper":
            result = _worker_faster_whisper(request)
        else:
            raise ValueError(f"unsupported engine: {request['engine']}")
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        status = "SKIP" if isinstance(exc, (ImportError, FileNotFoundError)) else "FAIL"
        if "local cache" in message.lower() or "not found" in message.lower():
            status = "SKIP"
        result = {
            "status": status,
            "engine": request.get("engine"),
            "message": message,
            "trials": [],
        }
    _write_json(result_path, result)
    return 0 if result["status"] in {"OK", "SKIP"} else 1


def _physical_core_count() -> int | None:
    if platform.system() != "Windows":
        return None
    script = (
        "(Get-CimInstance Win32_Processor | "
        "Measure-Object -Property NumberOfCores -Sum).Sum"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        return int(completed.stdout.strip())
    except ValueError:
        return None


def _total_ram_bytes() -> int | None:
    if platform.system() != "Windows":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys)


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.stdout.strip()


def environment_inventory() -> dict[str, Any]:
    config = load_config()
    recognition = config.recognition
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "repository": str(_ROOT),
        "git_head": _git_output("rev-parse", "HEAD"),
        "git_status": _git_output("status", "--short"),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "windows_build": platform.version(),
        "cpu": platform.processor(),
        "physical_cores": _physical_core_count(),
        "logical_cores": os.cpu_count(),
        "total_ram_bytes": _total_ram_bytes(),
        "real_usage": real_usage_summary(_ROOT / "zen-whisper.log"),
        "effective_config": {
            "engine": recognition.engine,
            "device": recognition.device,
            "reazon_language": recognition.reazon_language,
            "reazon_precision": recognition.reazon_precision,
            "reazon_inference_threads": recognition.reazon_inference_threads,
            "reazon_chunk_sec": recognition.reazon_chunk_sec,
            "reazon_trailing_silence_sec": (
                recognition.reazon_trailing_silence_sec
            ),
            "max_recording_sec": config.recording.max_recording_sec,
        },
        "production_path": {
            "loader": "src.asr.reazon._load_pinned_reazon_model",
            "chunker": "src.asr.reazon._iter_reazon_chunks",
            "backend": "src.asr.reazon.ReazonK2Backend.transcribe",
            "join": "single ASCII space between non-empty chunk texts",
            "stream_context": "new reazonspeech transcribe() call for every chunk",
        },
    }


def _distribution_license(name: str) -> dict[str, Any]:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return {
            "category": "python-package",
            "name": name,
            "status": "SKIP",
            "version": "",
            "license": "",
            "notice": "package is not installed",
            "path": "",
            "sha256": "",
        }
    metadata = distribution.metadata
    license_value = (
        metadata.get("License-Expression")
        or metadata.get("License")
        or "; ".join(
            classifier.removeprefix("License :: ")
            for classifier in metadata.get_all("Classifier", [])
            if classifier.startswith("License :: ")
        )
        or "Review required"
    )
    return {
        "category": "python-package",
        "name": name,
        "status": "Present",
        "version": distribution.version,
        "license": license_value,
        "notice": "Verify bundled native components separately before redistribution",
        "path": str(distribution.locate_file("")),
        "sha256": "",
    }


def license_inventory(
    reazon_snapshot: Path | None,
    corpus_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows = [
        _distribution_license(name)
        for name in (
            "reazonspeech-k2-asr",
            "sherpa-onnx",
            "sherpa-onnx-core",
            "onnxruntime",
            "faster-whisper",
            "ctranslate2",
            "av",
            "huggingface-hub",
            "pyarrow",
        )
    ]
    rows.append(
        {
            "category": "python-runtime-source",
            "name": "reazonspeech-k2-asr locked source",
            "status": "Present",
            "version": "2d4d4762e7ee294ac8e47a177ac2e9b0e8d0d43f",
            "license": "Apache-2.0",
            "notice": (
                "Repository LICENSE applies; retain license/notice obligations "
                "when redistributing"
            ),
            "path": (
                "https://github.com/reazon-research/ReazonSpeech/tree/"
                "2d4d4762e7ee294ac8e47a177ac2e9b0e8d0d43f/pkg/k2-asr"
            ),
            "sha256": "",
        }
    )
    source = model_source("reazon_k2", "ja")
    required = _reazon_model_files("int8-fp32")
    for role, filename in required.items():
        path = reazon_snapshot / filename if reazon_snapshot else None
        rows.append(
            {
                "category": "model",
                "name": f"Reazon K2 {role}",
                "status": "Present" if path and path.is_file() else "SKIP",
                "version": source.revision,
                "license": source.license,
                "notice": (
                    f"{source.license} model license; retain the model "
                    "repository's license and redistribution notices"
                ),
                "path": str(path) if path else filename,
                "sha256": _sha256(path) if path and path.is_file() else "",
            }
        )
    av_version = next(
        (row["version"] for row in rows if row["name"] == "av"),
        "",
    )
    whisper_exe = _HERE / "bin" / "whisper-cli.exe"
    rows.extend(
        [
            {
                "category": "candidate",
                "name": "SenseVoiceSmall official weights",
                "status": "Eligible with obligations",
                "version": "FunASR Model License v1.1",
                "license": "FunASR Model Open Source License Agreement v1.1",
                "notice": (
                    "Official weights are permitted for commercial local use; "
                    "retain attribution and the SenseVoice model name. Review "
                    "each third-party ONNX/GGUF conversion separately."
                ),
                "path": "https://github.com/QwenAudio/SenseVoice#license",
                "sha256": "",
            },
            {
                "category": "native-runtime",
                "name": "PyAV / FFmpeg payload",
                "status": "Review required",
                "version": av_version,
                "license": "Inspect the installed wheel's exact FFmpeg build",
                "notice": "Package metadata alone is insufficient",
                "path": "",
                "sha256": "",
            },
            {
                "category": "candidate",
                "name": "whisper.cpp executable and GGUF model",
                "status": (
                    "Present"
                    if whisper_exe.is_file() and (_HERE / "models").is_dir()
                    else "SKIP"
                ),
                "version": "",
                "license": "Review exact executable and model artifacts",
                "notice": "Local artifact is required; no automatic download",
                "path": str(whisper_exe),
                "sha256": _sha256(whisper_exe) if whisper_exe.is_file() else "",
            },
        ]
    )
    if corpus_metadata:
        asset = corpus_metadata.get("asset", {})
        rows.append(
            {
                "category": "dataset",
                "name": str(corpus_metadata.get("id", "public ASR corpus")),
                "status": "Present",
                "version": str(corpus_metadata.get("resolved_revision", "")),
                "license": str(corpus_metadata.get("source_license", "")),
                "notice": str(corpus_metadata.get("license_note", "")),
                "path": str(asset.get("url", "")),
                "sha256": str(asset.get("sha256", "")),
            }
        )
    return rows


def preflight_local_artifacts() -> dict[str, Any]:
    source = model_source("reazon_k2", "ja")
    required = tuple(_reazon_model_files("int8-fp32").values())
    runtime_present = (
        importlib.util.find_spec("reazonspeech") is not None
        and importlib.util.find_spec("reazonspeech.k2.asr") is not None
    )
    snapshot: Path | None = None
    model_error = ""
    try:
        snapshot = Path(
            download_verified_snapshot(
                source,
                required_files=required,
                local_files_only=True,
            )
        )
    except Exception as exc:
        model_error = f"{type(exc).__name__}: {exc}"

    faster_snapshot: Path | None = None
    faster_error = ""
    try:
        resolved = resolve_model(
            "faster_whisper",
            "large-v3-turbo",
            aliases={"turbo": "large-v3-turbo"},
        )
        assert resolved.model_source is not None
        faster_snapshot = Path(
            download_verified_snapshot(
                resolved.model_source,
                local_files_only=True,
            )
        )
    except Exception as exc:
        faster_error = f"{type(exc).__name__}: {exc}"
    return {
        "reazon_ready": runtime_present and snapshot is not None,
        "reazon_runtime_present": runtime_present,
        "reazon_model_present": snapshot is not None,
        "reazon_snapshot": str(snapshot) if snapshot else "",
        "reazon_model_error": model_error,
        "required_reazon_runtime": (
            "reazonspeech-k2-asr from locked git commit "
            "2d4d4762e7ee294ac8e47a177ac2e9b0e8d0d43f"
        ),
        "required_reazon_model": {
            "repo_id": source.repo_id,
            "revision": source.revision,
            "files": list(required),
            "license": source.license,
        },
        "faster_whisper_ready": (
            importlib.util.find_spec("faster_whisper") is not None
            and faster_snapshot is not None
        ),
        "faster_whisper_snapshot": str(faster_snapshot) if faster_snapshot else "",
        "faster_whisper_error": faster_error,
    }


def prepare_reazon_model(precision: str, out_dir: Path) -> Path:
    """Download only the pinned files required by one benchmark precision."""
    source = model_source("reazon_k2", "ja")
    required = _reazon_model_files(precision)
    snapshot = Path(
        download_verified_snapshot(
            source,
            required_files=tuple(required.values()),
            local_files_only=False,
        )
    )
    files = []
    for role, filename in required.items():
        path = snapshot / filename
        expected = source.files[filename]
        files.append(
            {
                "role": role,
                "filename": filename,
                "url": (
                    f"https://huggingface.co/{source.repo_id}/resolve/"
                    f"{source.revision}/{filename}"
                ),
                "size": expected.size,
                "sha256": expected.sha256,
                "local_path": str(path),
            }
        )
    manifest_path = out_dir / "reazon-model.json"
    _write_json(
        manifest_path,
        {
            "prepared_at": datetime.now(UTC).isoformat(),
            "repo_id": source.repo_id,
            "revision": source.revision,
            "license": source.license,
            "precision": precision,
            "snapshot": str(snapshot),
            "download_size": sum(item["size"] for item in files),
            "files": files,
        },
    )
    return manifest_path


def _item_payload(items: list[CorpusItem]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.item_id,
            "kind": item.kind,
            "audio": str(item.audio),
            "reference": item.reference,
            "reference_segments": list(item.reference_segments),
            "accepted_without_edit": item.accepted_without_edit,
            "corrected_text": item.corrected_text,
        }
        for item in items
    ]


def run_guarded_worker(
    request: dict[str, Any],
    *,
    run_dir: Path,
    label: str,
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> dict[str, Any]:
    request_path = run_dir / "requests" / f"{label}.json"
    result_path = run_dir / "worker-results" / f"{label}.json"
    _write_json(request_path, request)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker-request",
            str(request_path),
            "--worker-result",
            str(result_path),
        ],
        cwd=_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ceiling_bytes = memory_ceiling_mb * 1024 * 1024
    started = time.monotonic()
    peak_working_set = 0
    peak_private = 0
    stop_reason = ""
    while process.poll() is None:
        memory = _windows_process_memory(process.pid)
        if memory:
            peak_working_set = max(peak_working_set, memory["working_set_bytes"])
            peak_private = max(peak_private, memory["private_bytes"])
            if max(memory["working_set_bytes"], memory["private_bytes"]) > ceiling_bytes:
                stop_reason = f"memory ceiling exceeded ({memory_ceiling_mb} MiB)"
        if time.monotonic() - started > timeout_sec:
            stop_reason = f"timeout exceeded ({timeout_sec} seconds)"
        if stop_reason:
            process.terminate()
            break
        time.sleep(0.1)
    try:
        _stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        _stdout, stderr = process.communicate()
    if stop_reason:
        return {
            "status": "STOPPED",
            "engine": request["engine"],
            "message": stop_reason,
            "guard_peak_working_set_bytes": peak_working_set,
            "guard_peak_private_bytes": peak_private,
            "trials": [],
        }
    if not result_path.is_file():
        return {
            "status": "FAIL",
            "engine": request["engine"],
            "message": (
                f"worker exited {process.returncode} without a result: "
                f"{stderr.strip()[:500]}"
            ),
            "trials": [],
        }
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["guard_peak_working_set_bytes"] = peak_working_set
    result["guard_peak_private_bytes"] = peak_private
    if stderr.strip():
        result["worker_stderr_sha256"] = _text_sha256(stderr)
    return result


def _base_request(
    *,
    engine: str,
    items: list[CorpusItem],
    runs: int,
    threads: int,
    strategy: str,
    precision: str,
    private_transcript_dir: Path | None,
) -> dict[str, Any]:
    return {
        "engine": engine,
        "items": _item_payload(items),
        "runs": runs,
        "threads": threads,
        "strategy": strategy,
        "precision": precision,
        "chunk_sec": 25.0,
        "trailing_silence_sec": 0.5,
        "private_transcript_dir": (
            str(private_transcript_dir) if private_transcript_dir else ""
        ),
        "condition_on_previous_text": False,
    }


def execute_suite(
    items: list[CorpusItem],
    *,
    run_dir: Path,
    preflight: dict[str, Any],
    phases: set[str],
    cold_runs: int,
    warm_runs: int,
    memory_ceiling_mb: int,
    timeout_sec: int,
    precision: str,
    retain_private_transcripts: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    private_dir = run_dir / "private-transcripts" if retain_private_transcripts else None
    controlled = [
        item
        for item in items
        if item.kind in {"controlled", "public", "natural"}
    ]
    duration_items = sorted(
        (item for item in items if item.kind == "duration"),
        key=lambda item: item.target_duration_sec or item.duration_sec,
    )
    boundary_items = [item for item in items if item.kind == "boundary"]

    def run(label: str, phase: str, request: dict[str, Any]) -> dict[str, Any]:
        result = run_guarded_worker(
            request,
            run_dir=run_dir,
            label=label,
            memory_ceiling_mb=memory_ceiling_mb,
            timeout_sec=timeout_sec,
        )
        result["phase"] = phase
        result["label"] = label
        results.append(result)
        return result

    if preflight["reazon_ready"]:
        if "baseline" in phases and controlled:
            for index in range(cold_runs):
                request = _base_request(
                    engine="reazon-k2",
                    items=controlled,
                    runs=1,
                    threads=1,
                    strategy="fixed",
                    precision=precision,
                    private_transcript_dir=private_dir,
                )
                run(f"reazon-cold-{index + 1}", "baseline-cold", request)
            request = _base_request(
                engine="reazon-k2",
                items=controlled,
                runs=warm_runs,
                threads=1,
                strategy="fixed",
                precision=precision,
                private_transcript_dir=private_dir,
            )
            run("reazon-warm", "baseline-warm", request)

        if "duration" in phases:
            for item in duration_items:
                request = _base_request(
                    engine="reazon-k2",
                    items=[item],
                    runs=1,
                    threads=1,
                    strategy="single",
                    precision=precision,
                    private_transcript_dir=private_dir,
                )
                target = int(item.target_duration_sec or item.duration_sec)
                result = run(
                    f"reazon-duration-{target}",
                    "memory-duration",
                    request,
                )
                if result["status"] in {"STOPPED", "FAIL"}:
                    break

        if "threads" in phases and duration_items:
            representatives = [
                min(
                    duration_items,
                    key=lambda item: abs(
                        (item.target_duration_sec or item.duration_sec) - target
                    ),
                )
                for target in (25, 30)
            ]
            physical = _physical_core_count() or max(1, (os.cpu_count() or 2) // 2)
            for threads in dict.fromkeys((1, 2, 4, physical)):
                request = _base_request(
                    engine="reazon-k2",
                    items=representatives,
                    runs=3,
                    threads=threads,
                    strategy="single",
                    precision=precision,
                    private_transcript_dir=private_dir,
                )
                run(f"reazon-threads-{threads}", "thread-sweep", request)

        if "boundary" in phases and boundary_items:
            for strategy in ("fixed", "silence-aware"):
                request = _base_request(
                    engine="reazon-k2",
                    items=boundary_items,
                    runs=3,
                    threads=1,
                    strategy=strategy,
                    precision=precision,
                    private_transcript_dir=private_dir,
                )
                run(f"reazon-boundary-{strategy}", "boundary", request)
    else:
        results.append(
            {
                "status": "SKIP",
                "engine": "reazon-k2",
                "phase": "all-reazon",
                "label": "reazon-preflight",
                "message": (
                    "required locked runtime and/or verified local model snapshot "
                    "is absent; no download attempted"
                ),
                "trials": [],
            }
        )

    if (
        "faster-whisper" in phases
        and boundary_items
        and preflight["faster_whisper_ready"]
    ):
        for condition in (False, True):
            request = _base_request(
                engine="faster-whisper",
                items=boundary_items,
                runs=1,
                threads=4,
                strategy="runtime-windowing",
                precision="int8",
                private_transcript_dir=None,
            )
            request["condition_on_previous_text"] = condition
            run(
                f"faster-boundary-context-{str(condition).lower()}",
                "faster-whisper-context",
                request,
            )
    elif "faster-whisper" in phases:
        results.append(
            {
                "status": "SKIP",
                "engine": "faster-whisper",
                "phase": "faster-whisper-context",
                "label": "faster-whisper-preflight",
                "message": "runtime, local model snapshot, or boundary corpus is absent",
                "trials": [],
            }
        )
    return results


def add_loaded_modules_to_license_inventory(
    licenses: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    seen = {
        str(row.get("path", "")).casefold()
        for row in licenses
        if row.get("path")
    }
    pending: list[dict[str, Any]] = []
    for result in results:
        for module in result.get("loaded_modules", []):
            path = str(module.get("path", ""))
            if not path or path.casefold() in seen:
                continue
            seen.add(path.casefold())
            pending.append(module)
    for module in _add_authenticode_status(pending):
        path = str(module["path"])
        signature = module.get("signature", "not_collected")
        signer = module.get("signer", "")
        licenses.append(
            {
                "category": "loaded-native-module",
                "name": Path(path).name,
                "status": "Present",
                "version": "",
                "license": "Review owning package/runtime license",
                "notice": f"Authenticode={signature}; signer={signer or 'none'}",
                "path": path,
                "sha256": module.get("sha256", ""),
            }
        )


def _flatten_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        if not result.get("trials"):
            rows.append(
                {
                    "phase": result.get("phase", ""),
                    "label": result.get("label", ""),
                    "engine": result.get("engine", ""),
                    "status": result.get("status", ""),
                    "message": result.get("message", ""),
                }
            )
            continue
        for trial in result["trials"]:
            quality = trial.get("quality", {})
            boundary_local = trial.get("boundary_local", [])
            boundary_cers = [
                value["quality"]["normalized_cer"]
                for value in boundary_local
            ]
            rows.append(
                {
                    "phase": result.get("phase", ""),
                    "label": result.get("label", ""),
                    "engine": result.get("engine", ""),
                    "status": result.get("status", ""),
                    "item_id": trial.get("item_id", ""),
                    "kind": trial.get("kind", ""),
                    "run_index": trial.get("run_index", ""),
                    "strategy": result.get("strategy", ""),
                    "precision": result.get("precision", ""),
                    "configured_inference_threads": result.get(
                        "configured_inference_threads",
                        "",
                    ),
                    "observed_peak_process_threads": trial.get(
                        "peak_process_threads",
                        "",
                    ),
                    "condition_on_previous_text": result.get(
                        "condition_on_previous_text",
                        "",
                    ),
                    "audio_sec": trial.get("audio_sec", ""),
                    "load_sec": result.get("load_sec", ""),
                    "decode_sec": trial.get("decode_sec", ""),
                    "rtf": trial.get("rtf", ""),
                    "peak_working_set_bytes": trial.get(
                        "peak_working_set_bytes",
                        "",
                    ),
                    "peak_private_bytes": trial.get("peak_private_bytes", ""),
                    "strict_cer": quality.get("strict_cer", ""),
                    "normalized_cer": quality.get("normalized_cer", ""),
                    "boundary_count": len(boundary_local),
                    "boundary_local_normalized_cer_mean": (
                        statistics.fmean(boundary_cers)
                        if boundary_cers
                        else ""
                    ),
                    "boundary_local_insertions": sum(
                        value["edits"]["insertions"]
                        for value in boundary_local
                    ),
                    "boundary_local_deletions": sum(
                        value["edits"]["deletions"]
                        for value in boundary_local
                    ),
                    "boundary_local_substitutions": sum(
                        value["edits"]["substitutions"]
                        for value in boundary_local
                    ),
                    "correction_edits": quality.get("correction_edits", ""),
                    "correction_edits_per_100_chars": quality.get(
                        "correction_edits_per_100_chars",
                        "",
                    ),
                    "accepted_without_edit": quality.get(
                        "accepted_without_edit",
                        "",
                    ),
                    "transcript_chars": trial.get("transcript_chars", ""),
                    "transcript_sha256": trial.get("transcript_sha256", ""),
                    "message": "",
                }
            )
    return rows


def write_outputs(
    run_dir: Path,
    *,
    environment: dict[str, Any],
    preflight: dict[str, Any],
    items: list[CorpusItem],
    licenses: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    _write_json(run_dir / "environment.json", environment)
    _write_json(
        run_dir / "summary.json",
        {
            "version": SCHEMA_VERSION,
            "environment": environment,
            "preflight": preflight,
            "corpus": {
                "controlled": sum(item.kind == "controlled" for item in items),
                "public": sum(item.kind == "public" for item in items),
                "natural": sum(item.kind == "natural" for item in items),
                "duration": sum(item.kind == "duration" for item in items),
                "boundary": sum(item.kind == "boundary" for item in items),
            },
            "results": results,
        },
    )
    rows = _flatten_results(results)
    fieldnames = sorted({key for row in rows for key in row}) if rows else ["status"]
    with (run_dir / "measurements.csv").open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    license_fields = list(licenses[0]) if licenses else [
        "category",
        "name",
        "status",
        "version",
        "license",
        "notice",
        "path",
        "sha256",
    ]
    with (run_dir / "license-inventory.csv").open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=license_fields)
        writer.writeheader()
        writer.writerows(licenses)

    ok_trials = [
        trial
        for result in results
        if result.get("status") == "OK"
        for trial in result.get("trials", [])
    ]
    rtf_values = [
        float(trial["rtf"])
        for trial in ok_trials
        if trial.get("rtf") is not None
    ]
    controlled_count = sum(item.kind == "controlled" for item in items)
    public_count = sum(item.kind == "public" for item in items)
    natural_count = sum(item.kind == "natural" for item in items)
    lines = [
        "# ZenWhisper Windows CPU ASR measurement",
        "",
        f"- Captured: {environment['captured_at']}",
        f"- Git HEAD: `{environment['git_head']}`",
        f"- Git dirty: {'yes' if environment['git_status'] else 'no'}",
        (
            "- Effective app config: "
            f"engine=`{environment['effective_config']['engine']}`, "
            f"Reazon precision="
            f"`{environment['effective_config']['reazon_precision']}`, "
            f"threads={environment['effective_config']['reazon_inference_threads']}, "
            f"chunk={environment['effective_config']['reazon_chunk_sec']} s, "
            "trailing silence="
            f"{environment['effective_config']['reazon_trailing_silence_sec']} s"
        ),
        (
            "- Corpus: "
            f"controlled={controlled_count}, public={public_count}, "
            f"natural={natural_count}, "
            f"duration={sum(item.kind == 'duration' for item in items)}, "
            f"boundary={sum(item.kind == 'boundary' for item in items)}"
        ),
        "",
        "## Preflight",
        "",
        (
            "- Reazon locked runtime: "
            f"{'present' if preflight['reazon_runtime_present'] else 'missing'}"
        ),
        (
            "- Reazon verified local model: "
            f"{'present' if preflight['reazon_model_present'] else 'missing'}"
        ),
        (
            "- faster-whisper CPU smoke: "
            f"{'ready' if preflight['faster_whisper_ready'] else 'SKIP'}"
        ),
        "",
        "## Measurement result",
        "",
    ]
    if not preflight["reazon_ready"]:
        required_model = preflight["required_reazon_model"]
        lines.extend(
            [
                "- Reazon: SKIP. Locked runtime and/or verified local snapshot "
                "is absent; no download was attempted.",
                f"- Required runtime: {preflight['required_reazon_runtime']}",
                (
                    f"- Required model: {required_model['repo_id']}@"
                    f"{required_model['revision']} ({required_model['license']}); "
                    f"files={', '.join(required_model['files'])}"
                ),
            ]
        )
    failed = [
        result
        for result in results
        if result.get("status") in {"FAIL", "STOPPED"}
    ]
    if ok_trials:
        lines.extend(
            [
                f"- Completed trials: {len(ok_trials)}",
                f"- RTF median: {statistics.median(rtf_values):.4f}",
                f"- RTF p95: {percentile95(rtf_values):.4f}",
            ]
        )
        for condition in (False, True):
            context_trials = [
                trial
                for result in results
                if result.get("engine") == "faster-whisper"
                and result.get("condition_on_previous_text") is condition
                for trial in result.get("trials", [])
            ]
            if context_trials:
                context_rtfs = [float(trial["rtf"]) for trial in context_trials]
                context_cers = [
                    float(trial["quality"]["normalized_cer"])
                    for trial in context_trials
                ]
                lines.append(
                    "- faster-whisper "
                    f"condition_on_previous_text={condition}: "
                    f"median RTF={statistics.median(context_rtfs):.4f}, "
                    f"median normalized CER={statistics.median(context_cers):.4f}"
                )
    else:
        lines.extend(
            [
                "- No ASR trial completed.",
            ]
        )
    if failed:
        lines.extend(
            [
                "",
                "### Failed or guarded trials",
                "",
                *[
                    f"- {result.get('label', result.get('engine', 'trial'))}: "
                    f"{result.get('status')} — {result.get('message', '')}"
                    for result in failed
                ],
            ]
        )
    lines.extend(
        [
            "",
            "## Acceptance status",
            "",
            (
                f"- Public Common Voice count: {public_count} "
                "(target >= 30 for the public baseline)."
            ),
            f"- Synthetic controlled prompt count: {controlled_count}.",
            (
                f"- Private natural dictation count: {natural_count}; optional "
                "and not required for this public baseline."
            ),
            "- Raw audio, references, and transcripts were not added to Git.",
            "- This benchmark command does not mutate production config or "
            "security policy; the captured effective settings are listed above.",
            "- This public-baseline command does not choose production defaults; "
            "use bench_private_asr.py for the retained real-work/app-path decision.",
            "",
            "Machine-readable details: `summary.json`, `measurements.csv`, "
            "and `license-inventory.csv`.",
            "",
        ]
    )
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    _write_detailed_summary(
        run_dir,
        environment=environment,
        preflight=preflight,
        items=items,
        licenses=licenses,
        results=results,
    )


def _write_detailed_summary(
    run_dir: Path,
    *,
    environment: dict[str, Any],
    preflight: dict[str, Any],
    items: list[CorpusItem],
    licenses: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    def ok_trials(result: dict[str, Any]) -> list[dict[str, Any]]:
        if result.get("status") != "OK":
            return []
        return list(result.get("trials", []))

    def mib(value: int | float | None) -> float:
        return float(value or 0) / (1024 * 1024)

    def by_phase(phase: str) -> list[dict[str, Any]]:
        return [
            result
            for result in results
            if result.get("phase") == phase and result.get("status") == "OK"
        ]

    def inventory_values(
        *,
        category: str,
        field: str,
        name_contains: str = "",
    ) -> list[str]:
        return sorted(
            {
                str(row.get(field, "")).strip()
                for row in licenses
                if row.get("category") == category
                and name_contains.casefold()
                in str(row.get("name", "")).casefold()
                and str(row.get(field, "")).strip()
            }
        )

    reazon_model_versions = inventory_values(
        category="model",
        field="version",
        name_contains="Reazon K2",
    )
    reazon_model_licenses = inventory_values(
        category="model",
        field="license",
        name_contains="Reazon K2",
    )
    reazon_runtime_licenses = inventory_values(
        category="python-runtime-source",
        field="license",
        name_contains="reazonspeech-k2-asr",
    )

    public_count = sum(item.kind == "public" for item in items)
    duration_count = sum(item.kind == "duration" for item in items)
    boundary_count = sum(item.kind == "boundary" for item in items)
    corpus = environment.get("corpus_manifest", {})
    dataset = corpus.get("dataset", {})
    selection = corpus.get("selection", {})
    real_usage = environment.get("real_usage", {})
    cold = by_phase("baseline-cold")
    warm = by_phase("baseline-warm")
    cold_trials = [trial for result in cold for trial in ok_trials(result)]
    warm_trials = [trial for result in warm for trial in ok_trials(result)]
    warm_run_indices = {
        int(trial["run_index"])
        for trial in warm_trials
        if trial.get("run_index") is not None
    }
    warm_run_count = len(warm_run_indices)
    if not warm_run_count and public_count:
        warm_run_count = math.ceil(len(warm_trials) / public_count)
    lines = [
        "# ZenWhisper Windows CPU ASR measurement",
        "",
        "## Reproducibility",
        "",
        f"- Captured: {environment['captured_at']}",
        f"- Git HEAD: {environment['git_head']}",
        f"- Git dirty: {'yes' if environment['git_status'] else 'no'}",
        (
            "- Effective production control: "
            f"precision={environment['effective_config']['reazon_precision']}, "
            "configured inference threads="
            f"{environment['effective_config']['reazon_inference_threads']}, "
            f"fixed chunk={environment['effective_config']['reazon_chunk_sec']}s, "
            "overlap=none, trailing silence="
            f"{environment['effective_config']['reazon_trailing_silence_sec']}s, "
            "new stream per chunk, ASCII-space text join"
        ),
        (
            "- Runtime: reazonspeech-k2-asr 3.0.0 at "
            "2d4d4762e7ee294ac8e47a177ac2e9b0e8d0d43f"
        ),
        (
            "- Model: reazon-research/reazonspeech-k2-v2 at "
            f"{', '.join(reazon_model_versions) or 'not inventoried'}, "
            f"license={', '.join(reazon_model_licenses) or 'not inventoried'}"
        ),
        (
            f"- Corpus: public={public_count}, duration={duration_count}, "
            f"boundary={boundary_count}, private natural="
            f"{sum(item.kind == 'natural' for item in items)}"
        ),
    ]
    if dataset:
        asset = dataset.get("asset", {})
        lines.extend(
            [
                (
                    f"- Dataset: {dataset.get('id')} at "
                    f"{dataset.get('resolved_revision')} "
                    f"split={dataset.get('split')}"
                ),
                (
                    f"- Dataset artifact: size={asset.get('size')} bytes, "
                    f"SHA-256={asset.get('sha256')}, "
                    f"license={dataset.get('source_license')}"
                ),
                (
                    "- Selection: "
                    f"seed={selection.get('seed')}, "
                    f"duration bins={selection.get('duration_bin_counts')}; "
                    "speaker metadata unavailable in the pinned mirror"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Production-equivalent public baseline",
            "",
        ]
    )
    if cold and warm_trials:
        cold_loads = [float(result["load_sec"]) for result in cold]
        cold_rtfs = [float(trial["rtf"]) for trial in cold_trials]
        warm_rtfs = [float(trial["rtf"]) for trial in warm_trials]
        warm_cers = [
            float(trial["quality"]["normalized_cer"])
            for trial in warm_trials
        ]
        lines.extend(
            [
                (
                    f"- Cold: processes={len(cold)}, trials={len(cold_trials)}, "
                    f"load median={statistics.median(cold_loads):.3f}s, "
                    f"load p95={percentile95(cold_loads):.3f}s, "
                    f"RTF median={statistics.median(cold_rtfs):.4f}, "
                    f"RTF p95={percentile95(cold_rtfs):.4f}"
                ),
                (
                    f"- Warm: runs={warm_run_count} x {public_count} clips, "
                    f"RTF median={statistics.median(warm_rtfs):.4f}, "
                    f"RTF p95={percentile95(warm_rtfs):.4f}"
                ),
                (
                    f"- Quality: normalized CER mean="
                    f"{statistics.fmean(warm_cers):.4f}, "
                    f"median={statistics.median(warm_cers):.4f}, "
                    f"p95={percentile95(warm_cers):.4f}, "
                    f"normalized exact match="
                    f"{sum(value == 0 for value in warm_cers) / len(warm_cers):.1%}"
                ),
                (
                    "- Baseline result groups: "
                    f"OK={len(cold) + len(warm)}, "
                    "failed or guarded="
                    f"{sum(result.get('phase') in {'baseline-cold', 'baseline-warm'} and result.get('status') != 'OK' for result in results)}."
                ),
            ]
        )
    else:
        lines.append("- Reazon baseline: SKIP or incomplete.")

    lines.extend(["", "## Memory-duration curve", ""])
    duration_results = sorted(
        by_phase("memory-duration"),
        key=lambda result: float(result["trials"][0]["audio_sec"]),
    )
    if duration_results:
        duration_trials = [
            trial
            for result in duration_results
            for trial in ok_trials(result)
        ]
        lines.extend(
            [
                "| Audio | Decode | RTF | Peak WS | Peak private | Residual private | Normalized CER |",
                "|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for result in duration_results:
            trial = result["trials"][0]
            lines.append(
                f"| {trial['audio_sec']:.0f}s | {trial['decode_sec']:.3f}s | "
                f"{trial['rtf']:.4f} | "
                f"{mib(trial['peak_working_set_bytes']):.0f} MiB | "
                f"{mib(trial['peak_private_bytes']):.0f} MiB | "
                f"{mib(trial['after_decode']['private_bytes']):.0f} MiB | "
                f"{trial['quality']['normalized_cer']:.4f} |"
            )
        longest = max(
            duration_trials,
            key=lambda trial: float(trial["audio_sec"]),
        )
        lines.append(
            f"- Longest completed input={float(longest['audio_sec']):.0f}s; "
            f"peak private={mib(longest['peak_private_bytes']):.0f} MiB; "
            "normalized CER="
            f"{float(longest['quality']['normalized_cer']):.4f}."
        )
        duration_failures = [
            result
            for result in results
            if result.get("phase") == "memory-duration"
            and result.get("status") != "OK"
        ]
        if duration_failures:
            lines.append(
                f"- Failed or guarded duration result groups={len(duration_failures)}; "
                "inspect summary.json before drawing a production conclusion."
            )

    lines.extend(["", "## Thread sweep", ""])
    thread_results = sorted(
        by_phase("thread-sweep"),
        key=lambda result: int(result["configured_inference_threads"]),
    )
    if thread_results:
        lines.extend(
            [
                "| Configured threads | Trials | Median RTF | Median normalized CER | Peak private | Observed OS threads |",
                "|---:|---:|---:|---:|---:|---:|",
            ]
        )
        thread_stats: dict[int, dict[str, float]] = {}
        for result in thread_results:
            trials = ok_trials(result)
            if not trials:
                continue
            threads = int(result["configured_inference_threads"])
            stats = {
                "rtf": statistics.median(
                    float(trial["rtf"]) for trial in trials
                ),
                "cer": statistics.median(
                    float(trial["quality"]["normalized_cer"])
                    for trial in trials
                ),
                "peak_private_mib": max(
                    mib(trial["peak_private_bytes"]) for trial in trials
                ),
                "observed_threads": max(
                    int(trial["peak_process_threads"]) for trial in trials
                ),
            }
            thread_stats[threads] = stats
            lines.append(
                f"| {threads} | {len(trials)} | {stats['rtf']:.4f} | "
                f"{stats['cer']:.4f} | {stats['peak_private_mib']:.0f} MiB | "
                f"{stats['observed_threads']:.0f} |"
            )
        if 1 in thread_stats and 4 in thread_stats:
            one = thread_stats[1]
            four = thread_stats[4]
            gain = 1 - four["rtf"] / one["rtf"]
            lines.append(
                f"- threads=4 reduced representative median RTF by {gain:.1%} "
                "versus threads=1; normalized CER delta="
                f"{four['cer'] - one['cer']:+.4f}; peak private delta="
                f"{four['peak_private_mib'] - one['peak_private_mib']:+.0f} MiB."
            )
        if thread_stats:
            fastest_threads, fastest = min(
                thread_stats.items(),
                key=lambda item: item[1]["rtf"],
            )
            lines.append(
                f"- Fastest measured configuration=threads={fastest_threads}; "
                f"median RTF={fastest['rtf']:.4f}; observed OS threads="
                f"{fastest['observed_threads']:.0f}."
            )

    lines.extend(["", "## Chunk-boundary comparison", ""])
    boundary_results = by_phase("boundary")
    if boundary_results:
        lines.extend(
            [
                "| Strategy | Whole normalized CER | Boundary-local CER | Local insertions | Local deletions | RTF | Peak private |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for result in boundary_results:
            trials = ok_trials(result)
            whole_cer = statistics.median(
                float(trial["quality"]["normalized_cer"]) for trial in trials
            )
            local = [
                boundary
                for trial in trials
                for boundary in trial.get("boundary_local", [])
            ]
            local_cer = (
                statistics.median(
                    float(value["quality"]["normalized_cer"])
                    for value in local
                )
                if local
                else float("nan")
            )
            insertions_per_trial = [
                sum(value["edits"]["insertions"] for value in trial.get(
                    "boundary_local",
                    [],
                ))
                for trial in trials
            ]
            deletions_per_trial = [
                sum(value["edits"]["deletions"] for value in trial.get(
                    "boundary_local",
                    [],
                ))
                for trial in trials
            ]
            lines.append(
                f"| {result['strategy']} | {whole_cer:.4f} | "
                f"{local_cer:.4f} | "
                f"{statistics.median(insertions_per_trial):.0f} | "
                f"{statistics.median(deletions_per_trial):.0f} | "
                f"{statistics.median(float(trial['rtf']) for trial in trials):.4f} | "
                f"{max(mib(trial['peak_private_bytes']) for trial in trials):.0f} MiB |"
            )
        lines.append(
            "- Boundary-local reference ranges use the manifest composition "
            "recipe and duration-proportional slicing; this is reproducible but "
            "less exact than human word-level alignment."
        )

    lines.extend(["", "## Optional faster-whisper context A/B", ""])
    faster_stats: dict[bool, dict[str, float]] = {}
    for condition in (False, True):
        candidates = [
            result
            for result in results
            if result.get("engine") == "faster-whisper"
            and result.get("condition_on_previous_text") is condition
            and result.get("status") == "OK"
        ]
        trials = [
            trial
            for result in candidates
            for trial in result.get("trials", [])
        ]
        if trials:
            stats = {
                "rtf": statistics.median(float(t["rtf"]) for t in trials),
                "cer": statistics.median(
                    float(t["quality"]["normalized_cer"]) for t in trials
                ),
            }
            faster_stats[condition] = stats
            lines.append(
                f"- condition_on_previous_text={condition}: "
                f"median RTF={stats['rtf']:.4f}, "
                "median normalized CER="
                f"{stats['cer']:.4f}"
            )
    faster_worse = False
    if set(faster_stats) == {False, True}:
        disabled = faster_stats[False]
        enabled = faster_stats[True]
        faster_worse = (
            enabled["rtf"] > disabled["rtf"]
            and enabled["cer"] > disabled["cer"]
        )
        lines.append(
            "- Enabling context changed median RTF by "
            f"{enabled['rtf'] - disabled['rtf']:+.4f} and median normalized "
            f"CER by {enabled['cer'] - disabled['cer']:+.4f}."
        )
    elif not faster_stats:
        lines.append("- SKIP in this selected phase set.")

    lines.extend(["", "## Actual app path observation", ""])
    if real_usage.get("status") == "OK":
        lines.extend(
            [
                (
                    f"- Recent complete stop-to-paste samples: "
                    f"{real_usage['sample_count']}; engines="
                    f"{real_usage['engine_counts']}"
                ),
                (
                    f"- Stop-to-paste median="
                    f"{real_usage['end_to_end_sec']['median']:.3f}s, "
                    f"p95={real_usage['end_to_end_sec']['p95']:.3f}s; "
                    f"post-ASR paste overhead median="
                    f"{real_usage['post_asr_to_paste_sec']['median']:.3f}s"
                ),
                (
                    "- These are redacted real-use app observations from the "
                    "observed engines "
                    f"{real_usage.get('engine_counts', {})}. Public Reazon clips "
                    "provide ASR latency/CER; no automated active-window paste "
                    "was sent for public audio."
                ),
            ]
        )
    else:
        lines.append(f"- SKIP: {real_usage.get('message', 'no app log')}")

    signature_counts: dict[str, int] = {}
    for row in licenses:
        if row.get("category") != "loaded-native-module":
            continue
        notice = str(row.get("notice", ""))
        status = notice.partition("Authenticode=")[2].partition(";")[0]
        signature_counts[status or "not_collected"] = (
            signature_counts.get(status or "not_collected", 0) + 1
        )
    lines.extend(
        [
            "",
            "## License and policy evidence",
            "",
            f"- Inventory rows={len(licenses)}; loaded native modules="
            f"{sum(row.get('category') == 'loaded-native-module' for row in licenses)}.",
            f"- Authenticode status counts: {signature_counts}. Every readable "
            "loaded module has a SHA-256.",
            "- Reazon runtime source license="
            f"{', '.join(reazon_runtime_licenses) or 'not inventoried'}; "
            "model license="
            f"{', '.join(reazon_model_licenses) or 'not inventoried'}. "
            "Common Voice upstream is "
            "CC0-1.0; the pinned Hugging Face mirror omits a license field and "
            "is kept local-only.",
            "- SenseVoice official weights are commercially usable under the "
            "FunASR Model License v1.1 with attribution/model-name obligations; "
            "third-party conversions remain separately reviewed. The exact "
            "PyAV/FFmpeg payload remains Review required.",
            "- No AppLocker, WDAC, EDR, allowlist, signature policy, or security "
            "setting was changed.",
            "",
            "## Decision",
            "",
        ]
    )
    if duration_results:
        measured_duration_labels = sorted(
            {
                round(float(trial["audio_sec"]))
                for result in duration_results
                for trial in ok_trials(result)
            }
        )
        lines.append(
            "- Duration evidence was collected for "
            f"{measured_duration_labels}; captured production fixed chunk="
            f"{environment['effective_config']['reazon_chunk_sec']}s. "
            "This report does not mutate that setting."
        )
    if faster_worse:
        lines.append(
            "- Context carry-over was both slower and less accurate in this run; "
            "do not enable it from this evidence."
        )
    elif set(faster_stats) == {False, True}:
        lines.append(
            "- Context carry-over did not regress both measured dimensions; "
            "no categorical setting decision was made."
        )
    if thread_results:
        measured_thread_labels = sorted(
            int(result["configured_inference_threads"])
            for result in thread_results
        )
        lines.append(
            "- Measured thread configurations="
            f"{measured_thread_labels}; captured production setting="
            f"{environment['effective_config']['reazon_inference_threads']}. "
            "bench_private_asr.py verifies the retained real-work corpus and actual "
            "Transcriber path; reazon_inference_threads=1 remains the rollback."
        )
    if boundary_results:
        lines.append(
            "- Defer silence-aware production chunking to a separate change "
            "because its alignment method needs stronger review."
        )
    if not any((duration_results, thread_results, boundary_results, faster_stats)):
        lines.append("- No production decision was made from this partial phase set.")

    baseline_complete = (
        public_count >= 30
        and len(cold) >= 3
        and len(warm_trials) >= public_count * 5
    )
    measured_durations = {
        round(float(result["trials"][0]["audio_sec"]))
        for result in duration_results
    }
    duration_complete = set(DEFAULT_DURATIONS).issubset(measured_durations)
    measured_threads = {
        int(result["configured_inference_threads"])
        for result in thread_results
    }
    thread_complete = {1, 2, 4}.issubset(measured_threads) and len(measured_threads) >= 4
    measured_strategies = {
        str(result.get("strategy"))
        for result in boundary_results
    }
    boundary_complete = {"fixed", "silence-aware"}.issubset(measured_strategies)
    lines.extend(
        [
            "",
            "## Acceptance and limitations",
            "",
            (
                f"- Public fixed subset: {public_count} clips; cold 3 and warm 5 "
                f"{'complete' if baseline_complete else 'incomplete'}."
            ),
            (
                "- Duration 10/20/25/30/45/60s: "
                f"{'complete' if duration_complete else 'incomplete'}; "
                "threads 1/2/4/physical: "
                f"{'complete' if thread_complete else 'incomplete'}; "
                "fixed and silence-aware boundaries: "
                f"{'complete' if boundary_complete else 'incomplete'}; "
                "faster-whisper context A/B: "
                f"{'complete' if set(faster_stats) == {False, True} else 'incomplete or SKIP'}."
            ),
            "- Public subset records ASR latency, RTF, strict/normalized CER, "
            "failure status, RAM, configured threads, and observed OS threads.",
            "- Actual app logs provide redacted end-to-end timing. User-specific "
            "correction burden and microphone/voice suitability remain unmeasured "
            "and are an explicit non-blocking limitation of the public corpus.",
            "- Raw audio, references, and transcript text remain under the "
            "gitignored output directory; result files store hashes and metrics.",
            "- This benchmark command does not mutate production config or "
            "security policy; captured effective settings are reported above.",
            "",
            "Machine-readable details: summary.json, measurements.csv, "
            "license-inventory.csv, environment.json, and the pinned corpus "
            "manifest.",
            "",
        ]
    )
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def parse_phases(value: str) -> set[str]:
    valid = {
        "baseline",
        "duration",
        "threads",
        "boundary",
        "faster-whisper",
    }
    requested = {part.strip() for part in value.split(",") if part.strip()}
    if requested == {"all"}:
        return valid
    unknown = requested - valid
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown phase(s): {', '.join(sorted(unknown))}"
        )
    return requested


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure ZenWhisper Windows CPU ASR without downloads.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "init-corpus", "prepare-model"),
        default="run",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--voice")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--phases", type=parse_phases, default=parse_phases("all"))
    parser.add_argument("--cold-runs", type=int, default=3)
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument(
        "--memory-ceiling-mb",
        type=int,
        default=DEFAULT_MEMORY_CEILING_MB,
    )
    parser.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument(
        "--precision",
        choices=("int8-fp32", "int8", "fp32"),
        default="int8-fp32",
    )
    parser.add_argument("--retain-private-transcripts", action="store_true")
    parser.add_argument("--worker-request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.cold_runs < 3:
        parser.error("--cold-runs must be at least 3")
    if args.warm_runs < 5:
        parser.error("--warm-runs must be at least 5")
    if args.memory_ceiling_mb <= 0 or args.timeout_sec <= 0:
        parser.error("memory ceiling and timeout must be positive")
    if bool(args.worker_request) != bool(args.worker_result):
        parser.error("--worker-request and --worker-result must be used together")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.worker_request:
        return run_worker(args.worker_request, args.worker_result)
    if args.command == "init-corpus":
        manifest = generate_sapi_corpus(
            args.manifest.resolve().parent,
            voice=args.voice,
            overwrite=args.overwrite,
        )
        print(f"generated local corpus: {manifest}")
        return 0
    if args.command == "prepare-model":
        artifact = prepare_reazon_model(args.precision, args.out_dir.resolve())
        print(f"prepared Reazon model: {artifact}")
        return 0
    manifest = args.manifest.resolve()
    if not manifest.is_file():
        print(
            f"corpus manifest not found: {manifest}\n"
            "Run the init-corpus command first.",
            file=sys.stderr,
        )
        return 2
    manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    items = load_corpus_manifest(manifest)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.out_dir.resolve() / f"run-{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    environment = environment_inventory()
    environment["corpus_manifest"] = {
        "path": str(manifest),
        "sha256": _sha256(manifest),
        "dataset": manifest_document.get("dataset", {}),
        "selection": manifest_document.get("selection", {}),
    }
    preflight = preflight_local_artifacts()
    licenses = license_inventory(
        Path(preflight["reazon_snapshot"]) if preflight["reazon_snapshot"] else None,
        manifest_document.get("dataset"),
    )
    results = execute_suite(
        items,
        run_dir=run_dir,
        preflight=preflight,
        phases=args.phases,
        cold_runs=args.cold_runs,
        warm_runs=args.warm_runs,
        memory_ceiling_mb=args.memory_ceiling_mb,
        timeout_sec=args.timeout_sec,
        precision=args.precision,
        retain_private_transcripts=args.retain_private_transcripts,
    )
    add_loaded_modules_to_license_inventory(licenses, results)
    write_outputs(
        run_dir,
        environment=environment,
        preflight=preflight,
        items=items,
        licenses=licenses,
        results=results,
    )
    print(f"measurement summary: {run_dir / 'summary.md'}")
    return 1 if any(result.get("status") == "FAIL" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

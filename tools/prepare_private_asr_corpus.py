"""Prepare a retained, local-only real-work ASR evaluation corpus.

The source recording is never modified.  Audio is decoded through the
project's locked faster-whisper/PyAV stack, converted to 16 kHz mono PCM16,
split at long pauses, and accompanied by a hash-pinned local manifest.
Everything lives below ``tools/bench_outputs/`` and is ignored by Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from faster_whisper.audio import decode_audio


HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE / "bench_outputs" / "private" / "Recording (3).m4a"
DEFAULT_OUTPUT = HERE / "bench_outputs" / "private" / "realwork-v1"
SAMPLE_RATE = 16_000

PROMPTS = (
    "今日の作業では、Codexエージェントに既存コードの調査と実装、それからテストまで任せます。",
    "作業ツリーには未コミットの変更が残っているので、関係のないファイルは変更しないでください。",
    "プルリクエストの差分を確認して、重大度の高い問題から順番に指摘してください。",
    "この処理はGPUではなくCPUで動かすため、推論速度とメモリ使用量の両方を確認します。",
    "ReazonSpeech K2は、日本語の短い音声をCPU環境で処理するときにかなり高速です。",
    "SenseVoiceSmallを同じ条件で測定して、認識精度、レイテンシー、ライセンス条件を比較します。",
    "faster-whisperのlarge-v3-turboは、英語を含む文章や固有名詞の認識も確認する必要があります。",
    "Codex CLIからサブエージェントを起動して、独立した調査を並列に進めてください。",
    "miseで管理されたPythonとuvを使い、ロックファイルに記録された依存関係を再現します。",
    "GitHub ActionsのCIが失敗しているので、テストログを確認して原因を切り分けてください。",
    "Python 3.12を使い、ポート8080でAPIサーバーを起動します。タイムアウトは30秒に設定してください。",
    "このAPIはJSON形式のリクエストを受け取り、HTTPステータスコードの200か400を返します。",
    "PostgreSQLのトランザクションを確認し、インデックスの不足によるクエリーの遅延を調査します。",
    "OAuth 2.0とOpenID Connectを使って認証し、アクセストークンを安全に保存します。",
    "RAGの検索結果から関連する文書を取得し、LLMへ渡すコンテキストを組み立てます。",
    "Kubernetes上のコンテナを再起動する前に、ヘルスチェックとアプリケーションログを確認してください。",
    "srcディレクトリのmodel manifest JSONを更新し、testsディレクトリに回帰テストを追加します。",
    "スレッド数は8に変更してください。失礼、8ではなく4に変更して、もう一度ベンチマークを実行してください。",
    "今日の進捗を共有します。音声認識バックエンドの候補を比較したところ、公開データセットではReazonSpeech K2が最も高速でした。一方で、実際の業務ではCodex、エージェント、ワークツリー、プルリクエストといった専門用語を頻繁に使います。そのため、一般的な文字誤り率だけではなく、修正せずにそのまま送信できる割合も測定したいと考えています。",
    "次のリリースでは、音声入力を停止してから文字列が貼り付けられるまでの待ち時間を短縮します。まず、推論スレッドが1の場合と4の場合を比較し、中央値と95パーセンタイルを確認します。ただし、ベンチマークだけ速くても、バックグラウンドでビルドやテストを実行しているときに操作が重くなるなら採用しません。認識精度、CPU使用率、ピークメモリ、処理失敗の有無をまとめて判断します。専門用語の誤認識が多い場合は、ホットワードやmodified beam searchも検証します。最後にSenseVoiceSmallを同一音声で測定し、速度と品質の両方で明確な利点があるか確認します。",
)


@dataclass(frozen=True)
class Span:
    start: int
    end: int


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    padded = np.pad(mask.astype(np.int8), (1, 1))
    edges = np.flatnonzero(np.diff(padded))
    return list(zip(edges[::2], edges[1::2], strict=True))


def detect_utterances(
    audio: np.ndarray,
    *,
    min_split_silence_sec: float = 1.8,
    padding_sec: float = 0.25,
) -> tuple[list[Span], dict[str, float]]:
    """Detect utterances separated by deliberate long pauses."""
    frame = int(0.03 * SAMPLE_RATE)
    hop = int(0.01 * SAMPLE_RATE)
    if len(audio) < frame:
        return [], {"threshold": 0.0, "noise_floor": 0.0}
    frames = np.lib.stride_tricks.sliding_window_view(audio, frame)[::hop]
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    noise_floor = float(np.percentile(rms, 20))
    speech_floor = float(np.percentile(rms, 90))
    threshold = max(0.0025, noise_floor * 3.5, speech_floor * 0.08)
    active = rms >= threshold

    # Bridge brief intra-sentence pauses, but preserve the requested long gaps.
    max_bridge = max(1, int(0.45 / (hop / SAMPLE_RATE)))
    for start, end in _runs(~active):
        if start > 0 and end < len(active) and end - start <= max_bridge:
            active[start:end] = True

    min_active = max(1, int(0.16 / (hop / SAMPLE_RATE)))
    for start, end in _runs(active):
        if end - start < min_active:
            active[start:end] = False

    speech_runs = _runs(active)
    if not speech_runs:
        return [], {"threshold": threshold, "noise_floor": noise_floor}

    split_frames = max(1, int(min_split_silence_sec / (hop / SAMPLE_RATE)))
    grouped: list[tuple[int, int]] = []
    group_start, group_end = speech_runs[0]
    for start, end in speech_runs[1:]:
        if start - group_end >= split_frames:
            grouped.append((group_start, group_end))
            group_start = start
        group_end = end
    grouped.append((group_start, group_end))

    padding = int(padding_sec * SAMPLE_RATE)
    spans = [
        Span(
            int(max(0, start * hop - padding)),
            int(min(len(audio), end * hop + frame + padding)),
        )
        for start, end in grouped
    ]
    return spans, {"threshold": threshold, "noise_floor": noise_floor}


def _write_detection_diagnostics(
    output: Path,
    audio: np.ndarray,
    spans: list[Span],
    detection: dict[str, float],
) -> Path:
    detected_dir = output / "detected"
    detected_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for index, span in enumerate(spans, 1):
        path = detected_dir / f"detected_{index:02d}.wav"
        sf.write(path, audio[span.start : span.end], SAMPLE_RATE, subtype="PCM_16")
        entries.append(
            {
                "detected_index": index,
                "audio": str(path.relative_to(output)),
                "duration_sec": round((span.end - span.start) / SAMPLE_RATE, 6),
                "start_sample": span.start,
                "end_sample": span.end,
                "wav_sha256": sha256(path),
            }
        )
    path = output / "detection.json"
    path.write_text(
        json.dumps(
            {"detection": detection, "detected_count": len(spans), "items": entries},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def prepare(
    source: Path,
    output: Path,
    *,
    overwrite: bool,
    exclude_detected_indices: tuple[int, ...] = (),
) -> Path:
    source = source.resolve()
    output = output.resolve()
    manifest_path = output / "manifest.json"
    if not source.is_file():
        raise FileNotFoundError(source)
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"{manifest_path} exists; pass --overwrite")

    audio = np.asarray(decode_audio(str(source), sampling_rate=SAMPLE_RATE), np.float32)
    spans, detection = detect_utterances(audio)
    invalid_exclusions = [
        index for index in exclude_detected_indices if index < 1 or index > len(spans)
    ]
    if invalid_exclusions:
        raise ValueError(f"invalid detected indices: {invalid_exclusions}")
    excluded = set(exclude_detected_indices)
    selected_spans = [
        span for index, span in enumerate(spans, 1) if index not in excluded
    ]
    if len(selected_spans) != len(PROMPTS):
        diagnostic = _write_detection_diagnostics(output, audio, spans, detection)
        durations = [round((span.end - span.start) / SAMPLE_RATE, 2) for span in spans]
        raise RuntimeError(
            f"expected {len(PROMPTS)} selected utterances, detected {len(spans)} and "
            f"selected {len(selected_spans)}; durations={durations}; "
            f"detection={detection}; diagnostics={diagnostic}"
        )
    spans = selected_spans

    output.mkdir(parents=True, exist_ok=True)
    full_wav = output / "source_16k_mono.wav"
    sf.write(full_wav, audio, SAMPLE_RATE, subtype="PCM_16")
    clips_dir = output / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, object]] = []
    for index, (span, reference) in enumerate(zip(spans, PROMPTS, strict=True), 1):
        path = clips_dir / f"realwork_{index:02d}.wav"
        sf.write(path, audio[span.start : span.end], SAMPLE_RATE, subtype="PCM_16")
        items.append(
            {
                "id": f"realwork-{index:02d}",
                "kind": "natural",
                "audio": str(path.relative_to(output)),
                "reference": reference,
                "duration_sec": round((span.end - span.start) / SAMPLE_RATE, 6),
                "source_start_sample": span.start,
                "source_end_sample": span.end,
                "wav_sha256": sha256(path),
                "accepted_without_edit": None,
            }
        )

    manifest = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": "tools/prepare_private_asr_corpus.py",
        "privacy": {
            "classification": "private-local-only",
            "retention": "retained by user for future model comparisons",
            "training_use": False,
            "git_tracked": False,
            "raw_transcripts_retained": False,
        },
        "source": {
            "path": str(source),
            "size_bytes": source.stat().st_size,
            "sha256": sha256(source),
            "decoded_duration_sec": round(len(audio) / SAMPLE_RATE, 6),
            "derived_wav": str(full_wav.relative_to(output)),
            "derived_wav_sha256": sha256(full_wav),
        },
        "segmentation": {
            "sample_rate": SAMPLE_RATE,
            "minimum_split_silence_sec": 1.8,
            "padding_sec": 0.25,
            **detection,
            "excluded_detected_indices": sorted(excluded),
        },
        "selection": {
            "name": "realwork-v1",
            "prompt_count": len(PROMPTS),
            "frozen": True,
        },
        "items": items,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--exclude-detected-index",
        type=int,
        action="append",
        default=[],
        help="One-based detected span to omit; repeat for multiple spans.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = prepare(
        args.source,
        args.output,
        overwrite=args.overwrite,
        exclude_detected_indices=tuple(args.exclude_detected_index),
    )
    print(f"private corpus: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

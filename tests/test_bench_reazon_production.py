"""Pure helper tests for the production-equivalent Reazon benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tools.bench_reazon_production import (
    CorpusItem,
    _boundary_local_metrics,
    _chunks_for_strategy,
    _reference_for_window,
    _write_detailed_summary,
    edit_breakdown,
    fixed_ranges,
    levenshtein_distance,
    load_corpus_manifest,
    normalize_for_cer,
    parse_phases,
    percentile95,
    quality_metrics,
    real_usage_summary,
    silence_aware_ranges,
)


def _summary_environment() -> dict:
    return {
        "captured_at": "2026-08-31T00:00:00Z",
        "git_head": "deadbeef",
        "git_status": " M config.example.toml",
        "effective_config": {
            "reazon_precision": "int8-fp32",
            "reazon_inference_threads": 4,
            "reazon_chunk_sec": 25.0,
            "reazon_trailing_silence_sec": 0.5,
        },
        "corpus_manifest": {},
        "real_usage": {"status": "SKIP", "message": "test"},
    }


def _summary_licenses() -> list[dict]:
    return [
        {
            "category": "python-runtime-source",
            "name": "reazonspeech-k2-asr locked source",
            "version": "runtime-revision",
            "license": "Apache-2.0",
        },
        {
            "category": "model",
            "name": "Reazon K2 encoder",
            "version": "model-revision",
            "license": "MIT",
        },
    ]


def _summary_trial(
    *,
    item_id: str,
    audio_sec: float,
    rtf: float,
    cer: float,
    peak_private_mib: int,
    peak_threads: int,
) -> dict:
    return {
        "item_id": item_id,
        "audio_sec": audio_sec,
        "decode_sec": audio_sec * rtf,
        "rtf": rtf,
        "peak_working_set_bytes": (peak_private_mib + 10) * 1024 * 1024,
        "peak_private_bytes": peak_private_mib * 1024 * 1024,
        "peak_process_threads": peak_threads,
        "after_decode": {
            "private_bytes": (peak_private_mib - 5) * 1024 * 1024,
        },
        "quality": {"normalized_cer": cer},
    }


def test_quality_metrics_keep_strict_and_normalized_cer_separate() -> None:
    metrics = quality_metrics("ＡＢＣ、 テスト。", "ABCテスト")

    assert metrics["strict_cer"] > 0
    assert metrics["normalized_cer"] == 0
    assert normalize_for_cer("ＡＢＣ、 テスト。") == "ABCテスト"
    assert levenshtein_distance("abc", "adc") == 1


def test_percentile95_uses_observed_nearest_rank() -> None:
    assert percentile95([]) is None
    assert percentile95([1.0, 2.0, 3.0, 4.0, 5.0]) == 5.0


def test_fixed_ranges_match_production_sample_boundaries() -> None:
    ranges = fixed_ranges(61 * 16000, chunk_sec=25.0)

    assert ranges == [
        (0, 25 * 16000),
        (25 * 16000, 50 * 16000),
        (50 * 16000, 61 * 16000),
    ]


def test_fixed_strategy_reuses_trailing_silence_contract() -> None:
    audio = np.ones(26 * 16000, dtype=np.float32)

    chunks = _chunks_for_strategy(
        audio,
        strategy="fixed",
        chunk_sec=25.0,
        trailing_silence_sec=0.5,
    )

    assert [len(chunk) for chunk in chunks] == [
        int(25.5 * 16000),
        int(1.5 * 16000),
    ]
    assert np.all(chunks[0][-8000:] == 0)


def test_silence_aware_ranges_choose_gap_inside_search_window() -> None:
    sample_rate = 100
    audio = np.ones(65 * sample_rate, dtype=np.float32)
    audio[26 * sample_rate : 27 * sample_rate] = 0
    audio[53 * sample_rate : 54 * sample_rate] = 0

    ranges = silence_aware_ranges(
        audio,
        sample_rate=sample_rate,
        minimum_sec=24,
        maximum_sec=30,
        energy_window_sec=0.2,
        hop_sec=0.1,
    )

    boundaries = [end / sample_rate for _start, end in ranges[:-1]]
    assert boundaries[0] == pytest.approx(26.1, abs=0.2)
    assert boundaries[1] == pytest.approx(53.1, abs=0.2)
    assert all((end - start) / sample_rate <= 30 for start, end in ranges)


def test_manifest_requires_existing_16khz_audio(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, np.float32), 16000)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "controlled-01",
                        "kind": "controlled",
                        "audio": "sample.wav",
                        "reference": "テスト",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    items = load_corpus_manifest(manifest)

    assert items[0].audio == audio.resolve()
    assert items[0].duration_sec == pytest.approx(0.1)


def test_manifest_accepts_public_baseline_items(tmp_path: Path) -> None:
    audio = tmp_path / "public.wav"
    sf.write(audio, np.zeros(1600, np.float32), 16000)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "common-voice-01",
                        "kind": "public",
                        "audio": "public.wav",
                        "reference": "公開評価",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    items = load_corpus_manifest(manifest)

    assert items[0].kind == "public"


def test_parse_phases_expands_all_and_rejects_unknown() -> None:
    assert "faster-whisper" in parse_phases("all")
    assert parse_phases("baseline,duration") == {"baseline", "duration"}
    with pytest.raises(Exception, match="unknown phase"):
        parse_phases("download")


def test_edit_breakdown_distinguishes_insert_delete_substitute() -> None:
    assert edit_breakdown("abc", "axcd") == {
        "insertions": 1,
        "deletions": 0,
        "substitutions": 1,
    }
    assert edit_breakdown("abc", "ac")["deletions"] == 1


def test_boundary_local_metrics_use_segment_time_ranges() -> None:
    segments = [
        {
            "start_sample": 0,
            "end_sample": 10 * 16000,
            "reference": "あいうえおかきくけこ",
        }
    ]
    assert _reference_for_window(
        segments,
        start_sample=2 * 16000,
        end_sample=5 * 16000,
    ) == "うえお"

    metrics = _boundary_local_metrics(
        ranges=[(0, 5 * 16000), (5 * 16000, 10 * 16000)],
        timed_tokens=[
            {"seconds": 3.0, "token": "う"},
            {"seconds": 5.0, "token": "お"},
            {"seconds": 7.0, "token": "き"},
        ],
        reference_segments=segments,
        radius_sec=2.0,
    )

    assert metrics[0]["boundary_sec"] == 5.0
    assert metrics[0]["hypothesis_chars"] == 3


def test_real_usage_summary_redacts_events_and_aggregates_latency(
    tmp_path: Path,
) -> None:
    log = tmp_path / "zen-whisper.log"
    log.write_text(
        "\n".join(
            [
                "2026-08-25 10:00:00,000 [INFO] zen-whisper: "
                "トグルキー: 録音を停止します",
                "2026-08-25 10:00:00,500 [INFO] src.transcriber: "
                "文字起こし完了 (reazon-k2): lang=ja, 文字数=5, "
                "音声=4.0秒, 処理=0.50秒, RTF=0.125",
                "2026-08-25 10:00:00,900 [INFO] src.paster: "
                "ペースト完了: 5文字",
            ]
        ),
        encoding="utf-8",
    )

    summary = real_usage_summary(log)

    assert summary["sample_count"] == 1
    assert summary["engine_counts"] == {"reazon-k2": 1}
    assert summary["end_to_end_sec"]["median"] == pytest.approx(0.9)
    assert "events" not in summary


def test_detailed_summary_derives_partial_duration_and_license_evidence(
    tmp_path: Path,
) -> None:
    trial = _summary_trial(
        item_id="custom-12s",
        audio_sec=12.0,
        rtf=0.25,
        cer=0.125,
        peak_private_mib=123,
        peak_threads=9,
    )
    _write_detailed_summary(
        tmp_path,
        environment=_summary_environment(),
        preflight={},
        items=[
            CorpusItem(
                item_id="public-1",
                kind="public",
                audio=tmp_path / "unused.wav",
                reference="test",
            )
        ],
        licenses=_summary_licenses(),
        results=[
            {
                "phase": "memory-duration",
                "status": "OK",
                "trials": [trial],
            }
        ],
    )

    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Longest completed input=12s" in summary
    assert "peak private=123 MiB" in summary
    assert "license=MIT" in summary
    assert "runtime source license=Apache-2.0; model license=MIT" in summary
    assert "3.99 GiB" not in summary
    assert "defaults and security policy were not changed" not in summary
    assert "does not mutate production config" in summary


def test_detailed_summary_derives_custom_thread_and_context_comparison(
    tmp_path: Path,
) -> None:
    thread_results = []
    for threads, rtf, cer, peak_mib, observed in (
        (1, 1.0, 0.20, 200, 11),
        (4, 0.6, 0.23, 210, 24),
        (8, 0.4, 0.19, 230, 37),
    ):
        thread_results.append(
            {
                "phase": "thread-sweep",
                "status": "OK",
                "configured_inference_threads": threads,
                "trials": [
                    _summary_trial(
                        item_id=f"custom-{threads}",
                        audio_sec=7.0,
                        rtf=rtf,
                        cer=cer,
                        peak_private_mib=peak_mib,
                        peak_threads=observed,
                    )
                ],
            }
        )
    context_results = [
        {
            "phase": "faster-whisper-context",
            "engine": "faster-whisper",
            "condition_on_previous_text": False,
            "status": "OK",
            "trials": [
                _summary_trial(
                    item_id="context-off",
                    audio_sec=7.0,
                    rtf=0.8,
                    cer=0.10,
                    peak_private_mib=250,
                    peak_threads=12,
                )
            ],
        },
        {
            "phase": "faster-whisper-context",
            "engine": "faster-whisper",
            "condition_on_previous_text": True,
            "status": "OK",
            "trials": [
                _summary_trial(
                    item_id="context-on",
                    audio_sec=7.0,
                    rtf=0.7,
                    cer=0.12,
                    peak_private_mib=250,
                    peak_threads=12,
                )
            ],
        },
    ]

    _write_detailed_summary(
        tmp_path,
        environment=_summary_environment(),
        preflight={},
        items=[],
        licenses=_summary_licenses(),
        results=thread_results + context_results,
    )

    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "threads=4 reduced representative median RTF by 40.0%" in summary
    assert "normalized CER delta=+0.0300" in summary
    assert "Fastest measured configuration=threads=8" in summary
    assert "observed OS threads=37" in summary
    assert "threads=16" not in summary
    assert "OS threads to 99" not in summary
    assert "no categorical setting decision was made" in summary


def test_detailed_summary_derives_custom_warm_runs_and_real_usage_engine(
    tmp_path: Path,
) -> None:
    environment = _summary_environment()
    environment["real_usage"] = {
        "status": "OK",
        "sample_count": 3,
        "engine_counts": {"reazon-k2": 3},
        "end_to_end_sec": {"median": 0.5, "p95": 0.8},
        "post_asr_to_paste_sec": {"median": 0.1},
    }
    items = [
        CorpusItem(
            item_id=f"public-{index}",
            kind="public",
            audio=tmp_path / f"unused-{index}.wav",
            reference="test",
        )
        for index in range(2)
    ]
    cold_trial = _summary_trial(
        item_id="public-0",
        audio_sec=2.0,
        rtf=0.2,
        cer=0.1,
        peak_private_mib=100,
        peak_threads=8,
    )
    warm_trials = []
    for run_index in range(1, 8):
        for item in items:
            trial = _summary_trial(
                item_id=item.item_id,
                audio_sec=2.0,
                rtf=0.2,
                cer=0.1,
                peak_private_mib=100,
                peak_threads=8,
            )
            trial["run_index"] = run_index
            warm_trials.append(trial)

    _write_detailed_summary(
        tmp_path,
        environment=environment,
        preflight={},
        items=items,
        licenses=_summary_licenses(),
        results=[
            {
                "phase": "baseline-cold",
                "status": "OK",
                "load_sec": 0.3,
                "trials": [cold_trial],
            },
            {
                "phase": "baseline-warm",
                "status": "OK",
                "trials": warm_trials,
            },
        ],
    )

    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Warm: runs=7 x 2 clips" in summary
    assert "observed engines {'reazon-k2': 3}" in summary
    assert "current faster-whisper configuration" not in summary

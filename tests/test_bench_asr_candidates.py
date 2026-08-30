"""Pure tests for the fixed public-corpus candidate benchmark."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tools.bench_asr_candidates import (
    KOTOBA_FASTER_KEY,
    _candidate_request,
    _safe_extract_zip,
    parse_args,
    summarize_trials,
)
from tools.bench_reazon_production import CorpusItem


def test_summarize_trials_reports_distribution_and_quality() -> None:
    trials = [
        {
            "rtf": 0.1,
            "quality": {"normalized_cer": 0.0, "strict_cer": 0.1},
            "peak_private_bytes": 100,
        },
        {
            "rtf": 0.3,
            "quality": {"normalized_cer": 0.2, "strict_cer": 0.3},
            "peak_private_bytes": 200,
        },
    ]

    summary = summarize_trials(trials)

    assert summary["trial_count"] == 2
    assert summary["median_rtf"] == pytest.approx(0.2)
    assert summary["p95_rtf"] == pytest.approx(0.3)
    assert summary["mean_normalized_cer"] == pytest.approx(0.1)
    assert summary["normalized_exact_match"] == pytest.approx(0.5)
    assert summary["peak_private_bytes"] == 200


def test_candidate_request_pins_model_and_keeps_transcripts_private(
    tmp_path: Path,
) -> None:
    item = CorpusItem(
        item_id="public-01",
        kind="public",
        audio=tmp_path / "audio.wav",
        reference="公開評価",
    )

    request = _candidate_request(
        [item],
        model_name=KOTOBA_FASTER_KEY,
        engine_label="kotoba-whisper-v2.0-faster",
        runs=3,
        threads=4,
        chunk_length=15,
    )

    assert request["model_name"] == KOTOBA_FASTER_KEY
    assert request["condition_on_previous_text"] is False
    assert request["chunk_length"] == 15
    assert not request["private_transcript_dir"]


def test_safe_extract_zip_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "no")

    with pytest.raises(RuntimeError, match="unsafe zip member"):
        _safe_extract_zip(archive, tmp_path / "out")

    assert not (tmp_path / "escape.txt").exists()


def test_parse_args_rejects_non_positive_runs() -> None:
    args = parse_args(["run", "--runs", "3", "--threads", "4"])
    assert args.command == "run"
    assert args.runs == 3
    with pytest.raises(SystemExit):
        parse_args(["run", "--runs", "0"])

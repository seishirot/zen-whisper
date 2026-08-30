"""Tests for private real-work benchmark decisions and privacy."""

from __future__ import annotations

from dataclasses import fields

from tools.bench_private_asr import (
    FUNASR_RUNTIME,
    SENSEVOICE,
    _decision,
    _markdown_cell,
    parse_args,
)
from tools.bench_reazon_hotwords import BenchResult


def _trial(seconds: float, hash_value: str) -> dict[str, object]:
    return {
        "decode_sec": seconds,
        "rtf": seconds,
        "transcript_sha256": hash_value,
        "quality": {
            "normalized_cer": 0.1,
            "strict_cer": 0.1,
        },
        "peak_private_bytes": 100,
    }


def _result(label: str, seconds: float, hash_value: str = "same"):
    return {
        "label": label,
        "status": "OK",
        "trials": [_trial(seconds, hash_value), _trial(seconds, hash_value)],
    }


def test_thread_decision_accepts_faster_identical_output() -> None:
    decision = _decision(
        [
            _result("reazon-t1-normal", 1.0),
            _result("reazon-t4-normal", 0.7),
            _result("reazon-t1-cpu-load", 1.2),
            _result("reazon-t4-cpu-load", 0.9),
        ]
    )

    assert decision["change_default_to_4"] is True
    assert decision["quality_identical"] is True


def test_thread_decision_rejects_output_change() -> None:
    decision = _decision(
        [
            _result("reazon-t1-normal", 1.0),
            _result("reazon-t4-normal", 0.7, "different"),
            _result("reazon-t1-cpu-load", 1.2),
            _result("reazon-t4-cpu-load", 0.9),
        ]
    )

    assert decision["change_default_to_4"] is False
    assert decision["quality_identical"] is False


def test_private_benchmark_pins_official_sensevoice_artifacts() -> None:
    assert len(SENSEVOICE["revision"]) == 40
    assert len(SENSEVOICE["sha256"]) == 64
    assert FUNASR_RUNTIME["version"] == "runtime-llamacpp-v0.2.3"
    assert len(FUNASR_RUNTIME["commit"]) == 40


def test_hotword_result_has_no_transcript_body_field() -> None:
    assert "text" not in {field.name for field in fields(BenchResult)}


def test_private_transcript_markdown_escapes_table_breakers() -> None:
    assert _markdown_cell("left|right\nnext") == "left\\|right<br>next"


def test_resource_contention_defaults_to_four_cpu_stress_case() -> None:
    args = parse_args(["resource-contention"])

    assert args.logical_cpus == 4
    assert args.workers == 4
    assert args.duration_sec == 10.0

"""Accuracy aggregation must not reward failures or erase spelling differences."""
import pytest

from tools import bench_asr_compare as bench


def test_casefold_cer_does_not_hide_latin_spelling_metric():
    quality = bench.score("Codex API 12", "codex api 12")
    assert quality["cer"] == 0
    assert quality["latin_surface"] == {"expected": 2, "retained": 0}
    assert quality["latin_casefold"] == {"expected": 2, "retained": 2}
    assert quality["numeric_surface"] == {"expected": 1, "retained": 1}


def test_surface_numbers_preserve_spelling_differences():
    quality = bench.score("バージョン12と百円", "バージョン12と100円")
    assert quality["cer"] > 0
    assert quality["numeric_surface"] == {"expected": 2, "retained": 1}


def test_failures_and_suspected_omissions_do_not_become_fast_successes():
    reference = "比較用の文章を最後まで正しく認識します"
    rows = [
        {"status": "ok", "elapsed_sec": 2.0, "duration_sec": 5, "quality": bench.score(reference, reference)},
        {"status": "empty", "elapsed_sec": 0.001, "duration_sec": 5, "quality": bench.score(reference, "")},
        {"status": "ok", "elapsed_sec": 0.01, "duration_sec": 5, "quality": bench.score(reference, "比較")},
    ]
    result = bench.summarize(rows)
    assert result["count"] == 3
    assert result["failures"] == 1
    assert result["suspected_omissions"] == 1
    assert result["median_sec"] == result["p95_sec"] == 2.0
    assert result["exact_count"] == 1
    assert result["corpus_cer"] > 0.5
    assert result["latin_surface"]["rate"] is None


def test_corpus_cer_and_clip_average_are_distinct():
    rows = [
        {"status": "ok", "elapsed_sec": 1, "duration_sec": 1, "quality": bench.score("あ", "い")},
        {"status": "ok", "elapsed_sec": 1, "duration_sec": 1, "quality": bench.score("あ" * 9, "あ" * 9)},
    ]
    result = bench.summarize(rows)
    assert result["corpus_cer"] == 0.1
    assert result["mean_clip_cer"] == 0.5


def test_private_artifacts_cannot_escape_ignored_output_root(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "OUTPUTS", tmp_path / "ignored")
    assert bench.local_output(tmp_path / "ignored" / "summary.json").is_relative_to(tmp_path)
    with pytest.raises(ValueError, match="Private evaluation"):
        bench.local_output(tmp_path / "tracked" / "summary.json")


def test_smoke_rejects_tracked_output_before_reading_audio_or_loading_model(tmp_path):
    from types import SimpleNamespace
    from tools import bench_crispasr

    with pytest.raises(ValueError, match="Evaluation artifacts must stay"):
        bench_crispasr.run(SimpleNamespace(output=tmp_path / "summary"))
    assert not (tmp_path / "summary").exists()


def test_git_ignores_raw_and_derived_evaluation_artifacts():
    import subprocess

    paths = [
        "tools/bench_outputs/private/recording.m4a",
        "tools/bench_outputs/private/reports/comparison.md",
        "tools/bench_outputs/crispasr/run/summary.json",
        "tools/ASR_GPU_COMPARISON.md",
        "tools/asr-gpu-comparison-example.json",
        "tools/crispasr-cpu-comparison-example.json",
        "tools/crispasr-smoke-example.json",
        "tools/crispasr-runtime-inventory-example.json",
        "tools/report.local.md",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=bench.ROOT, input=("\0".join(paths) + "\0").encode(),
        capture_output=True, check=True,
    )
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(paths)
    public = subprocess.run(
        ["git", "check-ignore", "--no-index", "tools/ASR_BENCHMARKS.md",
         "tools/bench_asr_compare.py"],
        cwd=bench.ROOT, capture_output=True, text=True,
    )
    assert public.returncode == 1


@pytest.mark.parametrize("selection", ["smoke", "representative", "full"])
def test_public_only_run_never_reads_private_audio(monkeypatch, tmp_path, selection):
    import json
    from types import SimpleNamespace
    plan = {
        "items": [
            {"id": "public-a", "corpus": "public", "audio": "public-a.wav", "sha256": "fixture"},
            {"id": "private-a", "corpus": "private", "audio": "private-a.wav", "sha256": "fixture"},
        ],
        "selections": {"smoke": ["public-a", "private-a"], "representative": ["public-a", "private-a"]},
    }
    manifest = tmp_path / "synthetic.json"
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    reads = []

    def digest(path):
        assert path.name == "public-a.wav", "Excluded private input was read"
        reads.append(path.name)
        return "fixture"

    class StopBeforeInference(Exception):
        pass

    def stop(path):
        raise StopBeforeInference

    monkeypatch.setattr(bench, "digest", digest)
    monkeypatch.setattr(bench, "local_output", stop)
    args = SimpleNamespace(device="cpu", profile="reazon-k2", manifest=manifest,
                           selection=selection, corpora=["public"], output=tmp_path)
    with pytest.raises(StopBeforeInference):
        bench.run(args)
    assert reads == ["public-a.wav"]


@pytest.mark.parametrize("ids", [[], ["private-a"]])
def test_empty_selection_intersection_does_not_expand_to_other_inputs(monkeypatch, tmp_path, ids):
    import json
    from types import SimpleNamespace
    manifest = tmp_path / "synthetic.json"
    manifest.write_text(json.dumps({
        "items": [{"id": "public-a", "corpus": "public"}, {"id": "private-a", "corpus": "private"}],
        "selections": {"representative": ids},
    }), encoding="utf-8")
    monkeypatch.setattr(bench, "digest", lambda path: pytest.fail("No input should be read"))
    args = SimpleNamespace(device="cpu", profile="reazon-k2", manifest=manifest,
                           selection="representative", corpora=["public"])
    with pytest.raises(ValueError, match="No selected inputs"):
        bench.run(args)

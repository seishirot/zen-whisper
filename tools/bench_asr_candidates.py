"""Compare pinned Windows CPU ASR candidates on the fixed public corpus.

Preparation is explicit and is the only command that may use the network. The
benchmark command is local-only, stores no transcript text, and writes all
artifacts below the gitignored ``tools/bench_outputs`` directory.

Examples::

    mise exec -c 'uv run --locked --no-sync python tools/bench_asr_candidates.py prepare'
    mise exec -c 'uv run --locked --no-sync python tools/bench_asr_candidates.py run'
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from huggingface_hub import HfApi, hf_hub_download

from src.model_provenance import download_verified_snapshot, model_source
from tools.bench_reazon_production import (
    CorpusItem,
    _add_authenticode_status,
    _base_request,
    _item_payload,
    _sha256,
    _windows_process_memory,
    _windows_thread_count,
    load_corpus_manifest,
    percentile95,
    quality_with_correction,
    run_guarded_worker,
)

DEFAULT_CORPUS = (
    _HERE
    / "bench_outputs"
    / "reazon-production"
    / "corpus"
    / "public"
    / "manifest.json"
)
DEFAULT_OUTPUT_ROOT = _HERE / "bench_outputs" / "candidate-comparison"
DEFAULT_ARTIFACTS = DEFAULT_OUTPUT_ROOT / "artifacts.json"
MODEL_DIR = _HERE / "models" / "candidate-comparison"
BIN_DIR = _HERE / "bin" / "whisper-cpp-v1.9.2"

KOTOBA_FASTER_KEY = "kotoba-tech/kotoba-whisper-v2.0-faster"
KOTOBA_GGML = {
    "repo_id": "kotoba-tech/kotoba-whisper-v2.0-ggml",
    "revision": "e3a0cf6a62b95911703cfb97d819292e058f12c3",
    "filename": "ggml-kotoba-whisper-v2.0-q5_0.bin",
    "size": 537_819_875,
    "sha256": "4a3b92192b5d3578ff854a5876213e2e27af0c2d357492c2d14271e82c303658",
    "license": "Apache-2.0",
}
WHISPER_CPP = {
    "version": "v1.9.2",
    "commit": "306c88f4d1286aec1bf96e544632897886af5501",
    "filename": "whisper-bin-x64.zip",
    "size": 8_194_445,
    "sha256": "49dcc16de826f20bd53d44f947a1ae49dfa81f86cad67a64d80820cb192d674a",
    "url": (
        "https://github.com/ggml-org/whisper.cpp/releases/download/"
        "v1.9.2/whisper-bin-x64.zip"
    ),
    "license": "MIT",
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _verify_file(path: Path, *, size: int, sha256: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual_size = path.stat().st_size
    if actual_size != size:
        raise RuntimeError(f"{path}: size {actual_size} != {size}")
    actual_sha256 = _sha256(path)
    if actual_sha256 != sha256:
        raise RuntimeError(f"{path}: SHA-256 {actual_sha256} != {sha256}")


def _download_verified(url: str, destination: Path, *, size: int, sha256: str) -> None:
    if destination.is_file():
        _verify_file(destination, size=size, sha256=sha256)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "ZenWhisper-benchmark"})
    digest = hashlib.sha256()
    written = 0
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open(
        "wb"
    ) as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
            written += len(chunk)
    if written != size or digest.hexdigest() != sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"download verification failed: size={written}, sha256={digest.hexdigest()}"
        )
    os.replace(temporary, destination)


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            resolved = (destination / member.filename).resolve()
            if destination_root not in resolved.parents and resolved != destination_root:
                raise RuntimeError(f"unsafe zip member: {member.filename}")
        bundle.extractall(destination)


def _find_whisper_cli(directory: Path) -> Path:
    matches = sorted(directory.rglob("whisper-cli.exe"))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one whisper-cli.exe under {directory}, found {len(matches)}"
        )
    return matches[0].resolve()


def prepare_artifacts(output_path: Path = DEFAULT_ARTIFACTS) -> dict[str, Any]:
    faster_source = model_source("faster_whisper", KOTOBA_FASTER_KEY)
    faster_snapshot = Path(download_verified_snapshot(faster_source)).resolve()

    api = HfApi()
    ggml_info = api.model_info(
        KOTOBA_GGML["repo_id"],
        revision=KOTOBA_GGML["revision"],
        files_metadata=True,
    )
    if ggml_info.sha != KOTOBA_GGML["revision"]:
        raise RuntimeError(f"unexpected resolved GGML revision: {ggml_info.sha}")
    ggml_entry = next(
        sibling
        for sibling in ggml_info.siblings
        if sibling.rfilename == KOTOBA_GGML["filename"]
    )
    remote_sha = ggml_entry.lfs.sha256 if ggml_entry.lfs else ""
    if ggml_entry.size != KOTOBA_GGML["size"] or remote_sha != KOTOBA_GGML["sha256"]:
        raise RuntimeError("pinned Kotoba GGML metadata no longer matches")
    ggml_path = Path(
        hf_hub_download(
            repo_id=KOTOBA_GGML["repo_id"],
            filename=KOTOBA_GGML["filename"],
            revision=KOTOBA_GGML["revision"],
            local_dir=MODEL_DIR / "kotoba-whisper-v2.0-ggml",
        )
    ).resolve()
    _verify_file(
        ggml_path,
        size=KOTOBA_GGML["size"],
        sha256=KOTOBA_GGML["sha256"],
    )

    archive = BIN_DIR.parent / WHISPER_CPP["filename"]
    _download_verified(
        WHISPER_CPP["url"],
        archive,
        size=WHISPER_CPP["size"],
        sha256=WHISPER_CPP["sha256"],
    )
    _safe_extract_zip(archive, BIN_DIR)
    whisper_cli = _find_whisper_cli(BIN_DIR)
    signature = _add_authenticode_status(
        [{"path": str(whisper_cli), "signature": "not_collected"}]
    )[0]

    artifacts = {
        "prepared_at": datetime.now(UTC).isoformat(),
        "kotoba_faster": {
            "repo_id": faster_source.repo_id,
            "revision": faster_source.revision,
            "license": faster_source.license,
            "snapshot": str(faster_snapshot),
            "files": {
                name: {"size": spec.size, "sha256": spec.sha256}
                for name, spec in faster_source.files.items()
            },
            "source_url": (
                f"https://huggingface.co/{faster_source.repo_id}/tree/"
                f"{faster_source.revision}"
            ),
        },
        "kotoba_ggml": {
            **KOTOBA_GGML,
            "path": str(ggml_path),
            "source_url": (
                f"https://huggingface.co/{KOTOBA_GGML['repo_id']}/tree/"
                f"{KOTOBA_GGML['revision']}"
            ),
        },
        "whisper_cpp": {
            **WHISPER_CPP,
            "archive": str(archive.resolve()),
            "executable": str(whisper_cli),
            "executable_size": whisper_cli.stat().st_size,
            "executable_sha256": _sha256(whisper_cli),
            "authenticode_status": signature.get("signature", "not_collected"),
            "authenticode_signer": signature.get("signer", ""),
        },
    }
    _write_json(output_path, artifacts)
    return artifacts


def load_verified_artifacts(path: Path) -> dict[str, Any]:
    artifacts = json.loads(path.read_text(encoding="utf-8"))
    faster = artifacts["kotoba_faster"]
    source = model_source("faster_whisper", KOTOBA_FASTER_KEY)
    if faster["revision"] != source.revision or faster["license"] != source.license:
        raise RuntimeError("Kotoba faster artifact metadata differs from manifest")
    for name, spec in source.files.items():
        _verify_file(
            Path(faster["snapshot"]) / name,
            size=spec.size,
            sha256=spec.sha256,
        )
    ggml = artifacts["kotoba_ggml"]
    _verify_file(
        Path(ggml["path"]),
        size=KOTOBA_GGML["size"],
        sha256=KOTOBA_GGML["sha256"],
    )
    runtime = artifacts["whisper_cpp"]
    _verify_file(
        Path(runtime["archive"]),
        size=WHISPER_CPP["size"],
        sha256=WHISPER_CPP["sha256"],
    )
    executable = Path(runtime["executable"])
    if _sha256(executable) != runtime["executable_sha256"]:
        raise RuntimeError("whisper.cpp executable hash changed")
    return artifacts


def _candidate_request(
    items: list[CorpusItem],
    *,
    model_name: str,
    engine_label: str,
    runs: int,
    threads: int,
    chunk_length: int | None,
) -> dict[str, Any]:
    request = _base_request(
        engine="faster-whisper",
        items=items,
        runs=runs,
        threads=threads,
        strategy="runtime-windowing",
        precision="int8",
        private_transcript_dir=None,
    )
    request.update(
        {
            "model_name": model_name,
            "engine_label": engine_label,
            "chunk_length": chunk_length,
            "condition_on_previous_text": False,
        }
    )
    return request


def _run_ctranslate_candidate(
    items: list[CorpusItem],
    *,
    run_dir: Path,
    label: str,
    model_name: str,
    engine_label: str,
    runs: int,
    threads: int,
    chunk_length: int | None,
    phase: str,
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> dict[str, Any]:
    result = run_guarded_worker(
        _candidate_request(
            items,
            model_name=model_name,
            engine_label=engine_label,
            runs=runs,
            threads=threads,
            chunk_length=chunk_length,
        ),
        run_dir=run_dir,
        label=label,
        memory_ceiling_mb=memory_ceiling_mb,
        timeout_sec=timeout_sec,
    )
    result["phase"] = phase
    result["label"] = label
    return result


def _run_whisper_cpp_item(
    item: CorpusItem,
    *,
    executable: Path,
    model: Path,
    scratch_dir: Path,
    run_index: int,
    threads: int,
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> dict[str, Any]:
    stem = scratch_dir / f"{item.item_id}-r{run_index}"
    transcript_path = stem.with_suffix(".txt")
    command = [
        str(executable),
        "-m",
        str(model),
        "-f",
        str(item.audio),
        "-l",
        "ja",
        "-t",
        str(threads),
        "-bs",
        "1",
        "-ng",
        "-otxt",
        "-of",
        str(stem),
        "-np",
    ]
    scratch_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    peak_working_set = 0
    peak_private = 0
    peak_threads = 0
    stop_reason = ""
    ceiling_bytes = memory_ceiling_mb * 1024 * 1024
    while process.poll() is None:
        memory = _windows_process_memory(process.pid)
        if memory:
            peak_working_set = max(peak_working_set, memory["working_set_bytes"])
            peak_private = max(peak_private, memory["private_bytes"])
            if max(memory["working_set_bytes"], memory["private_bytes"]) > ceiling_bytes:
                stop_reason = f"memory ceiling exceeded ({memory_ceiling_mb} MiB)"
        process_threads = _windows_thread_count(process.pid)
        if process_threads is not None:
            peak_threads = max(peak_threads, process_threads)
        if time.perf_counter() - started > timeout_sec:
            stop_reason = f"timeout exceeded ({timeout_sec} seconds)"
        if stop_reason:
            process.terminate()
            break
        time.sleep(0.05)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    decode_sec = time.perf_counter() - started
    if stop_reason:
        transcript_path.unlink(missing_ok=True)
        raise RuntimeError(stop_reason)
    if process.returncode != 0:
        transcript_path.unlink(missing_ok=True)
        detail = (stderr or stdout).strip()[:500]
        raise RuntimeError(f"whisper.cpp exited {process.returncode}: {detail}")
    if not transcript_path.is_file():
        raise RuntimeError("whisper.cpp did not produce the expected text output")
    transcript = transcript_path.read_text(encoding="utf-8-sig").strip()
    transcript_path.unlink(missing_ok=True)
    audio_sec = item.duration_sec
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "run_index": run_index,
        "audio_sec": audio_sec,
        "decode_sec": decode_sec,
        "rtf": decode_sec / audio_sec,
        "chunks": None,
        "transcript_chars": len(transcript),
        "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        "quality": quality_with_correction(
            item.reference,
            transcript,
            corrected_text=item.corrected_text,
            accepted_without_edit=item.accepted_without_edit,
        ),
        "peak_working_set_bytes": peak_working_set,
        "peak_private_bytes": peak_private,
        "peak_process_threads": peak_threads,
    }


def _run_whisper_cpp(
    items: list[CorpusItem],
    *,
    artifacts: dict[str, Any],
    run_dir: Path,
    runs: int,
    threads: int,
    phase: str,
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> dict[str, Any]:
    runtime = artifacts["whisper_cpp"]
    ggml = artifacts["kotoba_ggml"]
    trials: list[dict[str, Any]] = []
    try:
        for run_index in range(1, runs + 1):
            for item in items:
                trials.append(
                    _run_whisper_cpp_item(
                        item,
                        executable=Path(runtime["executable"]),
                        model=Path(ggml["path"]),
                        scratch_dir=run_dir / "scratch-whisper-cpp",
                        run_index=run_index,
                        threads=threads,
                        memory_ceiling_mb=memory_ceiling_mb,
                        timeout_sec=timeout_sec,
                    )
                )
    except Exception as exc:
        return {
            "status": "FAIL",
            "engine": "kotoba-whisper-v2.0-whisper.cpp-q5_0",
            "phase": phase,
            "message": f"{type(exc).__name__}: {exc}",
            "trials": trials,
        }
    return {
        "status": "OK",
        "engine": "kotoba-whisper-v2.0-whisper.cpp-q5_0",
        "phase": phase,
        "precision": "q5_0",
        "configured_inference_threads": threads,
        "strategy": "external-cli-per-clip",
        "load_sec": None,
        "model": {
            "repo_id": ggml["repo_id"],
            "revision": ggml["revision"],
            "license": ggml["license"],
            "path": ggml["path"],
        },
        "runtime": runtime,
        "trials": trials,
    }


def summarize_trials(trials: list[dict[str, Any]]) -> dict[str, Any]:
    if not trials:
        return {"trial_count": 0}
    rtfs = [float(trial["rtf"]) for trial in trials]
    normalized_cers = [
        float(trial["quality"]["normalized_cer"]) for trial in trials
    ]
    strict_cers = [float(trial["quality"]["strict_cer"]) for trial in trials]
    peak_private = [
        int(trial.get("peak_private_bytes") or 0) for trial in trials
    ]
    return {
        "trial_count": len(trials),
        "median_rtf": statistics.median(rtfs),
        "p95_rtf": percentile95(rtfs),
        "mean_normalized_cer": statistics.fmean(normalized_cers),
        "median_normalized_cer": statistics.median(normalized_cers),
        "mean_strict_cer": statistics.fmean(strict_cers),
        "normalized_exact_match": sum(value == 0 for value in normalized_cers)
        / len(normalized_cers),
        "peak_private_bytes": max(peak_private),
    }


def _find_reazon_summary() -> Path | None:
    root = _HERE / "bench_outputs" / "reazon-production"
    for path in sorted(root.glob("run-*/summary.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if any(
            result.get("phase") == "baseline-warm"
            and result.get("status") == "OK"
            for result in payload.get("results", [])
        ):
            return path
    return None


def _reazon_reference(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = [
        result
        for result in payload.get("results", [])
        if result.get("phase") == "baseline-warm"
        and result.get("status") == "OK"
    ]
    if not candidates:
        return None
    result = candidates[-1]
    trials = [
        trial for trial in result.get("trials", []) if trial.get("kind") == "public"
    ]
    return {
        "engine": "reazon-k2-int8-fp32",
        "phase": "candidate-public-reference",
        "status": "OK",
        "load_sec": result.get("load_sec"),
        "configured_inference_threads": result.get("configured_inference_threads"),
        "strategy": "persistent-model; fixed 25s chunks",
        "source_summary": str(path.resolve()),
        "trials": trials,
    }


def _flatten_measurements(results: list[dict[str, Any]], path: Path) -> None:
    fields = [
        "engine",
        "phase",
        "item_id",
        "kind",
        "run_index",
        "audio_sec",
        "decode_sec",
        "rtf",
        "normalized_cer",
        "strict_cer",
        "peak_working_set_bytes",
        "peak_private_bytes",
        "peak_process_threads",
        "transcript_chars",
        "transcript_sha256",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            for trial in result.get("trials", []):
                quality = trial.get("quality", {})
                writer.writerow(
                    {
                        "engine": result.get("engine"),
                        "phase": result.get("phase"),
                        "item_id": trial.get("item_id"),
                        "kind": trial.get("kind"),
                        "run_index": trial.get("run_index"),
                        "audio_sec": trial.get("audio_sec"),
                        "decode_sec": trial.get("decode_sec"),
                        "rtf": trial.get("rtf"),
                        "normalized_cer": quality.get("normalized_cer"),
                        "strict_cer": quality.get("strict_cer"),
                        "peak_working_set_bytes": trial.get("peak_working_set_bytes"),
                        "peak_private_bytes": trial.get("peak_private_bytes"),
                        "peak_process_threads": trial.get("peak_process_threads"),
                        "transcript_chars": trial.get("transcript_chars"),
                        "transcript_sha256": trial.get("transcript_sha256"),
                    }
                )


def _write_summary(
    run_dir: Path,
    *,
    corpus_manifest: Path,
    artifacts: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    public_results = [
        result
        for result in results
        if result.get("phase") in {"candidate-public", "candidate-public-reference"}
        and result.get("status") == "OK"
    ]
    lines = [
        "# Windows CPU ASR candidate comparison",
        "",
        f"- Captured: {datetime.now(UTC).isoformat()}",
        f"- Corpus: {corpus_manifest.resolve()}",
        "- Raw transcript text: not retained",
        "",
        "## Public Common Voice subset",
        "",
        "| Engine | Trials | Lifecycle | Load | Median RTF | p95 RTF | Mean normalized CER | Exact match | Peak private |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    summaries: dict[str, Any] = {}
    for result in public_results:
        engine = str(result["engine"])
        metrics = summarize_trials(result["trials"])
        summaries[engine] = metrics
        lifecycle = (
            "cold CLI per clip"
            if result.get("strategy") == "external-cli-per-clip"
            else "persistent model"
        )
        load_value = result.get("load_sec")
        load_text = "-" if load_value is None else f"{float(load_value):.3f}s"
        lines.append(
            f"| {engine} | {metrics['trial_count']} | {lifecycle} | {load_text} | "
            f"{metrics['median_rtf']:.4f} | {metrics['p95_rtf']:.4f} | "
            f"{metrics['mean_normalized_cer']:.4f} | "
            f"{metrics['normalized_exact_match']:.1%} | "
            f"{metrics['peak_private_bytes'] / 1024 / 1024:.0f} MiB |"
        )
    lines.extend(
        [
            "",
            "whisper.cpp is intentionally measured as the deployable external CLI: "
            "each clip includes process and model startup, so its RTF is not a warm "
            "in-process latency comparison.",
            "",
            "## Duration and boundary stress",
            "",
            "| Engine | Item | Audio | RTF | Normalized CER | Peak private |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for result in results:
        if result.get("phase") != "candidate-stress" or result.get("status") != "OK":
            continue
        for trial in result.get("trials", []):
            lines.append(
                f"| {result['engine']} | {trial['item_id']} | "
                f"{trial['audio_sec']:.1f}s | {trial['rtf']:.4f} | "
                f"{trial['quality']['normalized_cer']:.4f} | "
                f"{int(trial.get('peak_private_bytes') or 0) / 1024 / 1024:.0f} MiB |"
            )
    lines.extend(
        [
            "",
            "## Artifact evidence",
            "",
            (
                "- Kotoba CTranslate2: "
                f"{artifacts['kotoba_faster']['repo_id']}@"
                f"{artifacts['kotoba_faster']['revision']}, "
                f"license={artifacts['kotoba_faster']['license']}"
            ),
            (
                "- Kotoba GGML q5_0: "
                f"{artifacts['kotoba_ggml']['repo_id']}@"
                f"{artifacts['kotoba_ggml']['revision']}, "
                f"SHA-256={artifacts['kotoba_ggml']['sha256']}, "
                f"license={artifacts['kotoba_ggml']['license']}"
            ),
            (
                "- whisper.cpp: "
                f"{artifacts['whisper_cpp']['version']}@"
                f"{artifacts['whisper_cpp']['commit']}, archive SHA-256="
                f"{artifacts['whisper_cpp']['sha256']}, license="
                f"{artifacts['whisper_cpp']['license']}, Authenticode="
                f"{artifacts['whisper_cpp']['authenticode_status']}"
            ),
            "",
            "Machine-readable details: results.json, measurements.csv, artifacts.json.",
            "",
        ]
    )
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    _write_json(run_dir / "metrics.json", summaries)


def execute(args: argparse.Namespace) -> Path:
    artifacts = load_verified_artifacts(args.artifacts)
    items = load_corpus_manifest(args.manifest)
    public_items = [item for item in items if item.kind == "public"]
    if len(public_items) < 30:
        raise RuntimeError(f"public corpus must contain at least 30 clips, got {len(public_items)}")
    stress_items = [
        item
        for item in items
        if item.kind == "boundary"
        or (item.kind == "duration" and round(item.target_duration_sec or 0) in {25, 60})
    ]
    run_dir = args.out_dir / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []

    for model_name, engine_label, chunk_length in (
        ("large-v3-turbo", "faster-whisper-large-v3-turbo", None),
        (KOTOBA_FASTER_KEY, "kotoba-whisper-v2.0-faster", 15),
    ):
        results.append(
            _run_ctranslate_candidate(
                public_items,
                run_dir=run_dir,
                label=f"{engine_label}-public",
                model_name=model_name,
                engine_label=engine_label,
                runs=args.runs,
                threads=args.threads,
                chunk_length=chunk_length,
                phase="candidate-public",
                memory_ceiling_mb=args.memory_ceiling_mb,
                timeout_sec=args.timeout_sec,
            )
        )
        results.append(
            _run_ctranslate_candidate(
                stress_items,
                run_dir=run_dir,
                label=f"{engine_label}-stress",
                model_name=model_name,
                engine_label=engine_label,
                runs=1,
                threads=args.threads,
                chunk_length=chunk_length,
                phase="candidate-stress",
                memory_ceiling_mb=args.memory_ceiling_mb,
                timeout_sec=args.timeout_sec,
            )
        )

    results.append(
        _run_whisper_cpp(
            public_items,
            artifacts=artifacts,
            run_dir=run_dir,
            runs=1,
            threads=args.threads,
            phase="candidate-public",
            memory_ceiling_mb=args.memory_ceiling_mb,
            timeout_sec=args.timeout_sec,
        )
    )
    results.append(
        _run_whisper_cpp(
            stress_items,
            artifacts=artifacts,
            run_dir=run_dir,
            runs=1,
            threads=args.threads,
            phase="candidate-stress",
            memory_ceiling_mb=args.memory_ceiling_mb,
            timeout_sec=args.timeout_sec,
        )
    )
    reazon = _reazon_reference(args.reazon_summary or _find_reazon_summary())
    if reazon is not None:
        results.append(reazon)

    payload = {
        "captured_at": datetime.now(UTC).isoformat(),
        "git_head": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip(),
        "manifest": str(args.manifest.resolve()),
        "settings": {
            "public_runs": args.runs,
            "threads": args.threads,
            "memory_ceiling_mb": args.memory_ceiling_mb,
            "timeout_sec": args.timeout_sec,
        },
        "artifacts": artifacts,
        "results": results,
    }
    _write_json(run_dir / "results.json", payload)
    _write_json(run_dir / "artifacts.json", artifacts)
    _flatten_measurements(results, run_dir / "measurements.csv")
    _write_summary(
        run_dir,
        corpus_manifest=args.manifest,
        artifacts=artifacts,
        results=results,
    )
    return run_dir


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    run = subparsers.add_parser("run")
    run.add_argument("--manifest", type=Path, default=DEFAULT_CORPUS)
    run.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    run.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    run.add_argument("--reazon-summary", type=Path)
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--threads", type=int, default=4)
    run.add_argument("--memory-ceiling-mb", type=int, default=6144)
    run.add_argument("--timeout-sec", type=int, default=900)
    args = parser.parse_args(argv)
    for name in ("runs", "threads", "memory_ceiling_mb", "timeout_sec"):
        if hasattr(args, name) and getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "prepare":
        artifacts = prepare_artifacts(args.artifacts)
        print(f"candidate artifacts: {args.artifacts.resolve()}")
        print(
            "prepared: "
            f"{artifacts['kotoba_faster']['repo_id']}, "
            f"{artifacts['kotoba_ggml']['filename']}, whisper.cpp "
            f"{artifacts['whisper_cpp']['version']}"
        )
        return 0
    run_dir = execute(args)
    print(f"candidate summary: {run_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

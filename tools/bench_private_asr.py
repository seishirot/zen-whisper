"""Benchmark the retained private real-work corpus without storing transcripts.

The network is used only by prepare-sensevoice. The run command verifies every
artifact locally, compares Reazon threads 1/4 in normal and CPU-contention
conditions, and runs the official SenseVoiceSmall GGUF CPU runtime.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
import shutil
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

from huggingface_hub import hf_hub_download

from tools.bench_asr_candidates import summarize_trials
from tools.bench_reazon_production import (
    CorpusItem,
    _add_authenticode_status,
    _base_request,
    _sha256,
    _windows_process_memory,
    _windows_thread_count,
    load_corpus_manifest,
    load_audio,
    percentile95,
    quality_with_correction,
    run_guarded_worker,
)
from src.config import ENGINE_REAZON_K2, RecognitionConfig
from src.transcriber import Transcriber

DEFAULT_MANIFEST = (
    _HERE / "bench_outputs" / "private" / "realwork-v1" / "manifest.json"
)
DEFAULT_OUTPUT = _HERE / "bench_outputs" / "private-comparison"
DEFAULT_ARTIFACTS = DEFAULT_OUTPUT / "sensevoice-artifacts.json"
SENSEVOICE_DIR = _HERE / "models" / "sensevoice-official"
RUNTIME_DIR = _HERE / "bin" / "funasr-llamacpp-v0.2.3"

SENSEVOICE = {
    "repo_id": "FunAudioLLM/SenseVoiceSmall-GGUF",
    "revision": "90c1c61912018b70ada0fcc024ea24aca62f2e63",
    "filename": "sensevoice-small-q8.gguf",
    "sha256": "4ae45c94422de949b387e2e0fb10d7e14e4c42c69db30c3444ecc7d4b844b7c5",
    "license": "FunASR Model Open Source License Agreement v1.1",
    "source_model": "FunAudioLLM/SenseVoiceSmall",
}
FSMN_VAD = {
    "repo_id": "FunAudioLLM/fsmn-vad-GGUF",
    "revision": "6840bae4c5c92ee8c04faaf4db23dd0105098d7f",
    "filename": "fsmn-vad.gguf",
    "sha256": "1270f2559c495f4e7b6e739541151027d360761a3fda43fc147034f5719f5479",
    "license": "Apache-2.0",
}
FUNASR_RUNTIME = {
    "version": "runtime-llamacpp-v0.2.3",
    "tag_object": "fc28b7e86e966b8ff63d39e7d0faf915bc0c7e64",
    "commit": "820f1c64a3123112a3099bc3fdc373dafa381768",
    "filename": "funasr-llamacpp-windows-x64-avx2.zip",
    "size": 5_195_078,
    "sha256": "55fa78e2a7522e54ead84532452afe40041a60014ba0261add611124897a7fe8",
    "url": (
        "https://github.com/modelscope/FunASR/releases/download/"
        "runtime-llamacpp-v0.2.3/funasr-llamacpp-windows-x64-avx2.zip"
    ),
    "license": "MIT",
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _verify_sha(path: Path, expected: str, *, size: int | None = None) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    if size is not None and path.stat().st_size != size:
        raise RuntimeError(f"{path}: size {path.stat().st_size} != {size}")
    actual = _sha256(path)
    if actual != expected:
        raise RuntimeError(f"{path}: SHA-256 {actual} != {expected}")


def _download_runtime(destination: Path) -> None:
    if destination.is_file():
        _verify_sha(
            destination,
            FUNASR_RUNTIME["sha256"],
            size=FUNASR_RUNTIME["size"],
        )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".partial")
    request = urllib.request.Request(
        FUNASR_RUNTIME["url"],
        headers={"User-Agent": "ZenWhisper-private-benchmark"},
    )
    digest = hashlib.sha256()
    written = 0
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open(
        "wb"
    ) as handle:
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
            digest.update(chunk)
            written += len(chunk)
    if (
        written != FUNASR_RUNTIME["size"]
        or digest.hexdigest() != FUNASR_RUNTIME["sha256"]
    ):
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"runtime verification failed: size={written}, sha256={digest.hexdigest()}"
        )
    os.replace(temporary, destination)


def _extract_runtime(archive: Path) -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    root = RUNTIME_DIR.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (RUNTIME_DIR / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"unsafe zip member: {member.filename}")
        bundle.extractall(RUNTIME_DIR)
    matches = list(RUNTIME_DIR.rglob("llama-funasr-sensevoice.exe"))
    if len(matches) != 1:
        raise RuntimeError(f"expected one SenseVoice executable, got {matches}")
    return matches[0]


def _copy_hf_artifact(spec: dict[str, Any]) -> Path:
    cached = Path(
        hf_hub_download(
            repo_id=spec["repo_id"],
            revision=spec["revision"],
            filename=spec["filename"],
        )
    )
    _verify_sha(cached, spec["sha256"])
    destination = SENSEVOICE_DIR / spec["filename"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file() or _sha256(destination) != spec["sha256"]:
        temporary = destination.with_suffix(destination.suffix + ".partial")
        shutil.copy2(cached, temporary)
        _verify_sha(temporary, spec["sha256"])
        os.replace(temporary, destination)
    return destination


def prepare_sensevoice(artifacts_path: Path) -> Path:
    archive = RUNTIME_DIR / FUNASR_RUNTIME["filename"]
    _download_runtime(archive)
    executable = _extract_runtime(archive)
    model = _copy_hf_artifact(SENSEVOICE)
    vad = _copy_hf_artifact(FSMN_VAD)
    runtime = {
        **FUNASR_RUNTIME,
        "archive": str(archive.resolve()),
        "executable": str(executable.resolve()),
        "executable_size": executable.stat().st_size,
        "executable_sha256": _sha256(executable),
    }
    signature = _add_authenticode_status(
        [{"path": str(executable), "signature": "not_collected"}]
    )[0]
    runtime["authenticode_status"] = signature.get(
        "signature", "not_collected"
    )
    runtime["authenticode_signer"] = signature.get("signer", "")
    payload = {
        "prepared_at": datetime.now(UTC).isoformat(),
        "sensevoice": {
            **SENSEVOICE,
            "path": str(model.resolve()),
            "size": model.stat().st_size,
        },
        "vad": {
            **FSMN_VAD,
            "path": str(vad.resolve()),
            "size": vad.stat().st_size,
        },
        "runtime": runtime,
        "license_policy": {
            "official_source_weights": (
                "Commercial use is eligible with attribution, source/model naming, "
                "and retained license obligations."
            ),
            "gguf_metadata": (
                "Apache-2.0 card observed, but source-weight v1.1 obligations "
                "retained conservatively."
            ),
        },
    }
    _write_json(artifacts_path, payload)
    return artifacts_path


def load_artifacts(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    model = Path(payload["sensevoice"]["path"])
    vad = Path(payload["vad"]["path"])
    runtime = payload["runtime"]
    executable = Path(runtime["executable"])
    archive = Path(runtime["archive"])
    _verify_sha(model, SENSEVOICE["sha256"])
    _verify_sha(vad, FSMN_VAD["sha256"])
    _verify_sha(
        archive,
        FUNASR_RUNTIME["sha256"],
        size=FUNASR_RUNTIME["size"],
    )
    _verify_sha(executable, runtime["executable_sha256"])
    return payload


def _start_cpu_load(process_count: int) -> list[subprocess.Popen[bytes]]:
    code = (
        "x=0x12345678\n"
        "while True:\n"
        " x=((x*1664525+1013904223)&0xffffffff)\n"
    )
    return [
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(process_count)
    ]


def _stop_cpu_load(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _run_reazon(
    items: list[CorpusItem],
    *,
    run_dir: Path,
    threads: int,
    runs: int,
    load_processes: int,
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> dict[str, Any]:
    request = _base_request(
        engine="reazon-k2",
        items=items,
        runs=runs,
        threads=threads,
        strategy="fixed",
        precision="int8-fp32",
        private_transcript_dir=None,
    )
    loaders = _start_cpu_load(load_processes)
    try:
        if loaders:
            time.sleep(0.5)
        condition = "cpu-load" if loaders else "normal"
        result = run_guarded_worker(
            request,
            run_dir=run_dir,
            label=f"reazon-t{threads}-{condition}",
            memory_ceiling_mb=memory_ceiling_mb,
            timeout_sec=timeout_sec,
        )
    finally:
        _stop_cpu_load(loaders)
    result["phase"] = f"reazon-{condition}"
    result["label"] = f"reazon-t{threads}-{condition}"
    result["cpu_load_processes"] = load_processes
    return result


def _run_cli_process(
    command: list[str],
    *,
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> tuple[str, float, int, int, int, float | None, int]:
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
    peak_process_tree_private = 0
    peak_threads = 0
    stop_reason = ""
    ceiling = memory_ceiling_mb * 1024 * 1024
    while process.poll() is None:
        memory = _windows_process_memory(process.pid)
        if memory:
            peak_working_set = max(peak_working_set, memory["working_set_bytes"])
            peak_private = max(peak_private, memory["private_bytes"])
            if max(memory.values()) > ceiling:
                stop_reason = f"memory ceiling exceeded ({memory_ceiling_mb} MiB)"
            parent_memory = _windows_process_memory(os.getpid())
            if parent_memory:
                peak_process_tree_private = max(
                    peak_process_tree_private,
                    memory["private_bytes"] + parent_memory["private_bytes"],
                )
        thread_count = _windows_thread_count(process.pid)
        if thread_count is not None:
            peak_threads = max(peak_threads, thread_count)
        if time.perf_counter() - started > timeout_sec:
            stop_reason = f"timeout exceeded ({timeout_sec}s)"
        if stop_reason:
            process.terminate()
            break
        time.sleep(0.025)
    stdout, stderr = process.communicate(timeout=10)
    elapsed = time.perf_counter() - started
    process_cpu_sec = _windows_process_cpu_seconds(process)
    if stop_reason:
        raise RuntimeError(stop_reason)
    if process.returncode != 0:
        raise RuntimeError(
            f"SenseVoice exited {process.returncode}: "
            f"{(stderr or stdout).strip()[:500]}"
        )
    return (
        stdout.strip(),
        elapsed,
        peak_working_set,
        peak_private,
        peak_threads,
        process_cpu_sec,
        peak_process_tree_private,
    )


def _windows_process_cpu_seconds(
    process: subprocess.Popen[Any],
) -> float | None:
    """Return user + kernel CPU time while Popen still owns its process handle."""
    if os.name != "nt" or not hasattr(process, "_handle"):
        return None

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    created = FileTime()
    exited = FileTime()
    kernel = FileTime()
    user = FileTime()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    ok = kernel32.GetProcessTimes(
        ctypes.c_void_p(int(process._handle)),
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    if not ok:
        return None

    def ticks(value: FileTime) -> int:
        return (int(value.high) << 32) | int(value.low)

    return (ticks(kernel) + ticks(user)) / 10_000_000


def _run_sensevoice(
    items: list[CorpusItem],
    *,
    artifacts: dict[str, Any],
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> dict[str, Any]:
    executable = artifacts["runtime"]["executable"]
    model = artifacts["sensevoice"]["path"]
    vad = artifacts["vad"]["path"]
    trials: list[dict[str, Any]] = []
    try:
        for item in items:
            (
                transcript,
                elapsed,
                peak_ws,
                peak_private,
                peak_threads,
                process_cpu_sec,
                peak_process_tree_private,
            ) = _run_cli_process(
                [
                    executable,
                    "-m",
                    model,
                    "--vad",
                    vad,
                    "-a",
                    str(item.audio),
                ],
                memory_ceiling_mb=memory_ceiling_mb,
                timeout_sec=timeout_sec,
            )
            audio_sec = item.duration_sec
            trials.append(
                {
                    "item_id": item.item_id,
                    "kind": item.kind,
                    "run_index": 1,
                    "audio_sec": audio_sec,
                    "decode_sec": elapsed,
                    "rtf": elapsed / audio_sec,
                    "transcript_chars": len(transcript),
                    "transcript_sha256": hashlib.sha256(
                        transcript.encode("utf-8")
                    ).hexdigest(),
                    "quality": quality_with_correction(
                        item.reference,
                        transcript,
                        corrected_text=item.corrected_text,
                        accepted_without_edit=item.accepted_without_edit,
                    ),
                    "peak_working_set_bytes": peak_ws,
                    "peak_private_bytes": peak_private,
                    "peak_process_threads": peak_threads,
                    "process_cpu_sec": process_cpu_sec,
                    "peak_process_tree_private_bytes": peak_process_tree_private,
                }
            )
    except Exception as exc:
        return {
            "status": "FAIL",
            "engine": "sensevoice-small-official-q8",
            "phase": "sensevoice-private",
            "message": f"{type(exc).__name__}: {exc}",
            "trials": trials,
        }
    return {
        "status": "OK",
        "engine": "sensevoice-small-official-q8",
        "phase": "sensevoice-private",
        "configured_inference_threads": "runtime-default",
        "strategy": (
            "official external CLI; cold process/model per clip; FSMN-VAD; "
            "runtime exposes no thread-count option"
        ),
        "model": artifacts["sensevoice"],
        "runtime": artifacts["runtime"],
        "trials": trials,
    }


def _latency_metrics(result: dict[str, Any]) -> dict[str, Any]:
    trials = result.get("trials", [])
    if not trials:
        return {"trial_count": 0}
    seconds = [float(trial["decode_sec"]) for trial in trials]
    metrics = summarize_trials(trials)
    metrics.update(
        {
            "median_decode_sec": statistics.median(seconds),
            "p95_decode_sec": percentile95(seconds),
            "failure_count": sum(
                not trial.get("transcript_sha256") for trial in trials
            ),
        }
    )
    return metrics


def _decision(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_label = {result.get("label"): result for result in results}
    t1 = by_label.get("reazon-t1-normal")
    t4 = by_label.get("reazon-t4-normal")
    l1 = by_label.get("reazon-t1-cpu-load")
    l4 = by_label.get("reazon-t4-cpu-load")
    if not all(result and result.get("status") == "OK" for result in (t1, t4, l1, l4)):
        return {"change_default_to_4": False, "reason": "required Reazon A/B failed"}
    m1 = _latency_metrics(t1)
    m4 = _latency_metrics(t4)
    lm1 = _latency_metrics(l1)
    lm4 = _latency_metrics(l4)
    normal_gain = 1 - m4["median_decode_sec"] / m1["median_decode_sec"]
    p95_gain = 1 - m4["p95_decode_sec"] / m1["p95_decode_sec"]
    load_gain = 1 - lm4["median_decode_sec"] / lm1["median_decode_sec"]
    quality_same = (
        abs(m4["mean_normalized_cer"] - m1["mean_normalized_cer"]) < 1e-12
        and {trial["transcript_sha256"] for trial in t1["trials"]}
        == {trial["transcript_sha256"] for trial in t4["trials"]}
    )
    ram_ratio = m4["peak_private_bytes"] / max(1, m1["peak_private_bytes"])
    accepted = (
        normal_gain >= 0.10
        and p95_gain >= 0.05
        and load_gain >= 0
        and quality_same
        and ram_ratio <= 1.10
    )
    return {
        "change_default_to_4": accepted,
        "normal_median_gain": normal_gain,
        "normal_p95_gain": p95_gain,
        "cpu_load_median_gain": load_gain,
        "quality_identical": quality_same,
        "peak_private_ratio": ram_ratio,
        "criteria": {
            "normal_median_gain_min": 0.10,
            "normal_p95_gain_min": 0.05,
            "cpu_load_median_gain_min": 0.0,
            "quality_identical": True,
            "peak_private_ratio_max": 1.10,
        },
    }


def _write_measurements(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "engine",
        "phase",
        "label",
        "threads",
        "cpu_load_processes",
        "item_id",
        "run_index",
        "audio_sec",
        "decode_sec",
        "rtf",
        "normalized_cer",
        "strict_cer",
        "transcript_chars",
        "transcript_sha256",
        "peak_working_set_bytes",
        "peak_private_bytes",
        "peak_process_threads",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            for trial in result.get("trials", []):
                quality = trial["quality"]
                writer.writerow(
                    {
                        "engine": result.get("engine"),
                        "phase": result.get("phase"),
                        "label": result.get("label"),
                        "threads": result.get("configured_inference_threads"),
                        "cpu_load_processes": result.get("cpu_load_processes", 0),
                        "item_id": trial["item_id"],
                        "run_index": trial["run_index"],
                        "audio_sec": trial["audio_sec"],
                        "decode_sec": trial["decode_sec"],
                        "rtf": trial["rtf"],
                        "normalized_cer": quality["normalized_cer"],
                        "strict_cer": quality["strict_cer"],
                        "transcript_chars": trial["transcript_chars"],
                        "transcript_sha256": trial["transcript_sha256"],
                        "peak_working_set_bytes": trial["peak_working_set_bytes"],
                        "peak_private_bytes": trial["peak_private_bytes"],
                        "peak_process_threads": trial["peak_process_threads"],
                    }
                )


def _write_summary(
    run_dir: Path,
    *,
    manifest: Path,
    artifacts: dict[str, Any],
    results: list[dict[str, Any]],
    decision: dict[str, Any],
) -> None:
    lines = [
        "# Private real-work ASR comparison",
        "",
        f"- Captured: {datetime.now(UTC).isoformat()}",
        f"- Corpus: {manifest.resolve()}",
        "- Raw audio/reference: private, local-only, retained for future comparisons",
        "- Transcript bodies: not retained",
        "",
        "## Results",
        "",
        "| Engine / condition | Trials | Threads | Median decode | p95 decode | Median RTF | Mean CER | Exact proxy | Peak private |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        if result.get("status") != "OK":
            lines.append(
                f"| {result.get('label') or result.get('engine')} (FAIL) | "
                f"{len(result.get('trials', []))} | - | - | - | - | - | - | - |"
            )
            continue
        metrics = _latency_metrics(result)
        lines.append(
            f"| {result.get('label') or result['engine']} | "
            f"{metrics['trial_count']} | "
            f"{result.get('configured_inference_threads', '-')} | "
            f"{metrics['median_decode_sec']:.3f}s | "
            f"{metrics['p95_decode_sec']:.3f}s | "
            f"{metrics['median_rtf']:.4f} | "
            f"{metrics['mean_normalized_cer']:.4f} | "
            f"{metrics['normalized_exact_match']:.1%} | "
            f"{metrics['peak_private_bytes'] / 1024 / 1024:.0f} MiB |"
        )
    lines.extend(
        [
            "",
            "Exact proxy means normalized exact match against the prescribed text; "
            "it is not a human acceptance judgment.",
            "",
            "## Thread decision",
            "",
            f"- Change Reazon default to 4: {decision.get('change_default_to_4')}",
            f"- Evidence: {json.dumps(decision, ensure_ascii=False)}",
            "",
            "## SenseVoice artifact policy",
            "",
            f"- Runtime: {artifacts['runtime']['version']}@{artifacts['runtime']['commit']}",
            f"- Model: {artifacts['sensevoice']['repo_id']}@{artifacts['sensevoice']['revision']}",
            "- The official GGUF card says Apache-2.0; this evaluation conservatively "
            "retains the official source-weight FunASR Model License v1.1 obligations.",
            "",
            "Machine-readable details: results.json, measurements.csv.",
        ]
    )
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> Path:
    artifacts = load_artifacts(args.artifacts)
    items = load_corpus_manifest(args.manifest)
    if len(items) != 20 or any(item.kind != "natural" for item in items):
        raise RuntimeError("realwork-v1 must contain exactly 20 natural clips")
    run_dir = args.out_dir / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    results = [
        _run_reazon(
            items,
            run_dir=run_dir,
            threads=threads,
            runs=args.normal_runs,
            load_processes=0,
            memory_ceiling_mb=args.memory_ceiling_mb,
            timeout_sec=args.timeout_sec,
        )
        for threads in (1, 4)
    ]
    results.extend(
        _run_reazon(
            items,
            run_dir=run_dir,
            threads=threads,
            runs=args.load_runs,
            load_processes=args.cpu_load_processes,
            memory_ceiling_mb=args.memory_ceiling_mb,
            timeout_sec=args.timeout_sec,
        )
        for threads in (1, 4)
    )
    sensevoice = _run_sensevoice(
        items,
        artifacts=artifacts,
        memory_ceiling_mb=args.memory_ceiling_mb,
        timeout_sec=args.timeout_sec,
    )
    sensevoice["label"] = "sensevoice-q8-cold-cli"
    results.append(sensevoice)
    decision = _decision(results)
    payload = {
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": _sha256(args.manifest),
        "settings": {
            "normal_runs": args.normal_runs,
            "load_runs": args.load_runs,
            "cpu_load_processes": args.cpu_load_processes,
            "memory_ceiling_mb": args.memory_ceiling_mb,
            "timeout_sec": args.timeout_sec,
        },
        "artifacts": artifacts,
        "results": results,
        "decision": decision,
    }
    _write_json(run_dir / "results.json", payload)
    _write_measurements(run_dir / "measurements.csv", results)
    _write_summary(
        run_dir,
        manifest=args.manifest,
        artifacts=artifacts,
        results=results,
        decision=decision,
    )
    return run_dir


def verify_app_path(manifest: Path, out_dir: Path) -> Path:
    """Run the real Transcriber -> Reazon backend path with production defaults."""
    items = load_corpus_manifest(manifest)
    run_dir = out_dir / datetime.now().strftime("app-run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    cfg = RecognitionConfig(
        engine=ENGINE_REAZON_K2,
        device="cpu",
    )
    transcriber = Transcriber()
    trials: list[dict[str, Any]] = []
    load_started = time.perf_counter()
    transcriber.load_model(cfg)
    load_sec = time.perf_counter() - load_started
    try:
        if not transcriber.is_ready or transcriber.engine_label != "reazon-k2":
            raise RuntimeError("production Transcriber did not load Reazon")
        for item in items:
            audio = load_audio(item.audio)
            started = time.perf_counter()
            transcript = transcriber.transcribe(audio, "ja", cfg)
            elapsed = time.perf_counter() - started
            trials.append(
                {
                    "item_id": item.item_id,
                    "audio_sec": item.duration_sec,
                    "decode_sec": elapsed,
                    "rtf": elapsed / item.duration_sec,
                    "transcript_chars": len(transcript),
                    "transcript_sha256": hashlib.sha256(
                        transcript.encode("utf-8")
                    ).hexdigest(),
                    "quality": quality_with_correction(
                        item.reference,
                        transcript,
                        corrected_text=item.corrected_text,
                        accepted_without_edit=item.accepted_without_edit,
                    ),
                }
            )
    finally:
        transcriber.unload()
    result = {
        "status": "OK",
        "engine": "reazon-k2",
        "phase": "production-transcriber",
        "configured_inference_threads": cfg.reazon_inference_threads,
        "load_sec": load_sec,
        "trials": trials,
    }
    metrics = _latency_metrics(result)
    _write_json(
        run_dir / "results.json",
        {
            "captured_at": datetime.now(UTC).isoformat(),
            "manifest": str(manifest.resolve()),
            "manifest_sha256": _sha256(manifest),
            "transcript_bodies_retained": False,
            "result": result,
            "metrics": metrics,
        },
    )
    (run_dir / "summary.md").write_text(
        "\n".join(
            [
                "# Production Transcriber verification",
                "",
                f"- Reazon inference threads: {cfg.reazon_inference_threads}",
                f"- Model load: {load_sec:.3f}s",
                f"- Trials: {metrics['trial_count']}",
                f"- Median decode: {metrics['median_decode_sec']:.3f}s",
                f"- p95 decode: {metrics['p95_decode_sec']:.3f}s",
                f"- Mean normalized CER: {metrics['mean_normalized_cer']:.4f}",
                f"- Exact proxy: {metrics['normalized_exact_match']:.1%}",
                "- Transcript bodies: not retained",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return run_dir


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def compare_transcripts(
    manifest: Path,
    artifacts_path: Path,
    out_dir: Path,
    *,
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> Path:
    """Retain transcript bodies only for the user's explicit private review."""
    artifacts = load_artifacts(artifacts_path)
    items = load_corpus_manifest(manifest)
    run_dir = out_dir / datetime.now().strftime(
        "transcript-comparison-%Y%m%d-%H%M%S"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    cfg = RecognitionConfig(
        engine=ENGINE_REAZON_K2,
        device="cpu",
        reazon_inference_threads=4,
    )
    transcriber = Transcriber()
    rows: list[dict[str, Any]] = []
    transcriber.load_model(cfg)
    try:
        for item in items:
            audio = load_audio(item.audio)
            reazon_text = transcriber.transcribe(audio, "ja", cfg)
            (
                sense_text,
                sense_elapsed,
                _peak_ws,
                _peak_private,
                _peak_threads,
                _sense_cpu,
                _peak_tree_private,
            ) = _run_cli_process(
                [
                    artifacts["runtime"]["executable"],
                    "-m",
                    artifacts["sensevoice"]["path"],
                    "--vad",
                    artifacts["vad"]["path"],
                    "-a",
                    str(item.audio),
                ],
                memory_ceiling_mb=memory_ceiling_mb,
                timeout_sec=timeout_sec,
            )
            quality_kwargs = {
                "corrected_text": item.corrected_text,
                "accepted_without_edit": item.accepted_without_edit,
            }
            reazon_quality = quality_with_correction(
                item.reference, reazon_text, **quality_kwargs
            )
            sense_quality = quality_with_correction(
                item.reference, sense_text, **quality_kwargs
            )
            reazon_cer = float(reazon_quality["normalized_cer"])
            sense_cer = float(sense_quality["normalized_cer"])
            winner = (
                "Reazon"
                if reazon_cer < sense_cer
                else "SenseVoice"
                if sense_cer < reazon_cer
                else "同点"
            )
            rows.append(
                {
                    "item_id": item.item_id,
                    "reference": item.reference,
                    "reazon": reazon_text,
                    "sensevoice": sense_text,
                    "reazon_normalized_cer": reazon_cer,
                    "sensevoice_normalized_cer": sense_cer,
                    "winner": winner,
                    "sensevoice_decode_sec": sense_elapsed,
                }
            )
    finally:
        transcriber.unload()

    payload = {
        "captured_at": datetime.now(UTC).isoformat(),
        "privacy": {
            "classification": "private-local-only",
            "transcript_bodies_retained": True,
            "authorization": "explicit user request for human model comparison",
            "training_use": False,
            "git_tracked": False,
        },
        "manifest": str(manifest.resolve()),
        "manifest_sha256": _sha256(manifest),
        "reazon_inference_threads": 4,
        "rows": rows,
    }
    _write_json(run_dir / "comparison.json", payload)
    lines = [
        "# Private transcript comparison",
        "",
        "> Private/local-only. Transcript bodies are retained here by explicit user request.",
        "",
        "| ID | 読み上げ原稿 | Reazon K2 (threads=4) | SenseVoiceSmall Q8 | CER (R / S) |",
        "|---|---|---|---|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['item_id']} | {_markdown_cell(row['reference'])} | "
            f"{_markdown_cell(row['reazon'])} | {_markdown_cell(row['sensevoice'])} | "
            f"{row['reazon_normalized_cer']:.3f} / "
            f"{row['sensevoice_normalized_cer']:.3f} ({row['winner']}) |"
        )
    (run_dir / "comparison.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return run_dir


def _timed_cpu_workers(
    worker_count: int,
    *,
    start_at: float,
    duration_sec: float,
) -> list[subprocess.Popen[str]]:
    code = (
        "import sys,time\n"
        "start=float(sys.argv[1]); duration=float(sys.argv[2])\n"
        "while time.perf_counter()<start: time.sleep(0.001)\n"
        "end=start+duration; x=0x12345678; count=0\n"
        "while time.perf_counter()<end:\n"
        " x=((x*1664525+1013904223)&0xffffffff); count+=1\n"
        "print(count)\n"
    )
    return [
        subprocess.Popen(
            [sys.executable, "-c", code, str(start_at), str(duration_sec)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for _ in range(worker_count)
    ]


def _collect_cpu_workers(processes: list[subprocess.Popen[str]]) -> int:
    total = 0
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode != 0:
            raise RuntimeError(f"CPU worker failed: {stderr.strip()[:300]}")
        total += int(stdout.strip())
    return total


def _wait_until(target: float) -> None:
    delay = target - time.perf_counter()
    if delay > 0:
        time.sleep(delay)


def _available_affinity_mask(logical_cpus: int) -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessAffinityMask.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.GetProcessAffinityMask.restype = ctypes.c_int
    process = kernel32.GetCurrentProcess()
    process_mask = ctypes.c_size_t()
    system_mask = ctypes.c_size_t()
    if not kernel32.GetProcessAffinityMask(
        process, ctypes.byref(process_mask), ctypes.byref(system_mask)
    ):
        raise OSError("GetProcessAffinityMask failed")
    selected = 0
    remaining = logical_cpus
    for bit in range(ctypes.sizeof(ctypes.c_size_t) * 8):
        candidate = 1 << bit
        if process_mask.value & candidate:
            selected |= candidate
            remaining -= 1
            if remaining == 0:
                break
    if remaining:
        raise RuntimeError(
            f"requested {logical_cpus} logical CPUs, but affinity has fewer"
        )
    return int(process_mask.value), selected


def _set_affinity(mask: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = ctypes.c_int
    if not kernel32.SetProcessAffinityMask(
        kernel32.GetCurrentProcess(), ctypes.c_size_t(mask)
    ):
        raise OSError("SetProcessAffinityMask failed")


def _background_window(worker_count: int, duration_sec: float) -> int:
    start_at = time.perf_counter() + 0.5
    processes = _timed_cpu_workers(
        worker_count, start_at=start_at, duration_sec=duration_sec
    )
    return _collect_cpu_workers(processes)


def _measure_reazon_contention(
    items: list[CorpusItem],
    *,
    threads: int,
    worker_count: int,
    duration_sec: float,
) -> dict[str, Any]:
    cfg = RecognitionConfig(
        engine=ENGINE_REAZON_K2,
        device="cpu",
        reazon_inference_threads=threads,
    )
    transcriber = Transcriber()
    transcriber.load_model(cfg)
    start_at = time.perf_counter() + 0.5
    workers = _timed_cpu_workers(
        worker_count, start_at=start_at, duration_sec=duration_sec
    )
    _wait_until(start_at)
    deadline = start_at + duration_sec
    decode_wall_sec = 0.0
    process_cpu_start = time.process_time()
    clip_count = 0
    audio_sec = 0.0
    item_index = 0
    try:
        while time.perf_counter() < deadline:
            item = items[item_index % len(items)]
            started = time.perf_counter()
            transcriber.transcribe(load_audio(item.audio), "ja", cfg)
            decode_wall_sec += time.perf_counter() - started
            clip_count += 1
            audio_sec += item.duration_sec
            item_index += 1
    finally:
        process_cpu_sec = time.process_time() - process_cpu_start
        transcriber.unload()
    background_iterations = _collect_cpu_workers(workers)
    return {
        "label": f"reazon-t{threads}",
        "threads": threads,
        "clip_count": clip_count,
        "audio_sec": audio_sec,
        "decode_wall_sec": decode_wall_sec,
        "process_cpu_sec": process_cpu_sec,
        "average_cores_during_decode": process_cpu_sec / decode_wall_sec,
        "background_iterations": background_iterations,
    }


def _measure_sensevoice_contention(
    items: list[CorpusItem],
    *,
    artifacts: dict[str, Any],
    worker_count: int,
    duration_sec: float,
    memory_ceiling_mb: int,
    timeout_sec: int,
) -> dict[str, Any]:
    start_at = time.perf_counter() + 0.5
    workers = _timed_cpu_workers(
        worker_count, start_at=start_at, duration_sec=duration_sec
    )
    _wait_until(start_at)
    deadline = start_at + duration_sec
    decode_wall_sec = 0.0
    process_cpu_sec = 0.0
    clip_count = 0
    audio_sec = 0.0
    item_index = 0
    while time.perf_counter() < deadline:
        item = items[item_index % len(items)]
        (
            _text,
            elapsed,
            _ws,
            _private,
            _threads,
            cpu_sec,
            _tree_private,
        ) = _run_cli_process(
            [
                artifacts["runtime"]["executable"],
                "-m",
                artifacts["sensevoice"]["path"],
                "--vad",
                artifacts["vad"]["path"],
                "-a",
                str(item.audio),
            ],
            memory_ceiling_mb=memory_ceiling_mb,
            timeout_sec=timeout_sec,
        )
        decode_wall_sec += elapsed
        process_cpu_sec += cpu_sec or 0.0
        clip_count += 1
        audio_sec += item.duration_sec
        item_index += 1
    background_iterations = _collect_cpu_workers(workers)
    return {
        "label": "sensevoice-q8-cold-cli",
        "threads": "runtime-default",
        "clip_count": clip_count,
        "audio_sec": audio_sec,
        "decode_wall_sec": decode_wall_sec,
        "process_cpu_sec": process_cpu_sec,
        "average_cores_during_decode": process_cpu_sec / decode_wall_sec,
        "background_iterations": background_iterations,
    }


def measure_resource_contention(args: argparse.Namespace) -> Path:
    artifacts = load_artifacts(args.artifacts)
    items = load_corpus_manifest(args.manifest)
    run_dir = args.out_dir / datetime.now().strftime(
        "resource-contention-%Y%m%d-%H%M%S"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    affinity = _available_affinity_mask(args.logical_cpus)
    original_mask = affinity[0] if affinity else None
    selected_mask = affinity[1] if affinity else None
    if selected_mask is not None:
        _set_affinity(selected_mask)
    try:
        baseline = _background_window(args.workers, args.duration_sec)
        results = [
            _measure_reazon_contention(
                items,
                threads=threads,
                worker_count=args.workers,
                duration_sec=args.duration_sec,
            )
            for threads in (1, 4)
        ]
        results.append(
            _measure_sensevoice_contention(
                items,
                artifacts=artifacts,
                worker_count=args.workers,
                duration_sec=args.duration_sec,
                memory_ceiling_mb=args.memory_ceiling_mb,
                timeout_sec=args.timeout_sec,
            )
        )
    finally:
        if original_mask is not None:
            _set_affinity(original_mask)
    for result in results:
        result["background_throughput_ratio"] = (
            result["background_iterations"] / baseline
        )
        result["background_slowdown"] = (
            1 - result["background_throughput_ratio"]
        )
    payload = {
        "captured_at": datetime.now(UTC).isoformat(),
        "method": (
            "Process affinity restricted to the selected logical CPUs; fixed-time "
            "deterministic CPU workers measured alone and while ASR ran continuously."
        ),
        "logical_cpu_count_host": os.cpu_count(),
        "logical_cpus_in_test": args.logical_cpus,
        "background_workers": args.workers,
        "duration_sec": args.duration_sec,
        "background_baseline_iterations": baseline,
        "results": results,
    }
    _write_json(run_dir / "results.json", payload)
    lines = [
        "# CPU resource contention",
        "",
        f"- Host logical CPUs: {os.cpu_count()}",
        f"- Test affinity: {args.logical_cpus} logical CPUs",
        f"- Background CPU workers: {args.workers}",
        f"- Window per condition: {args.duration_sec:.1f}s",
        "",
        "| Engine | ASR clips | ASR audio | Decode wall | Process CPU | Average cores | Background throughput | Slowdown |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['label']} | {result['clip_count']} | "
            f"{result['audio_sec']:.1f}s | {result['decode_wall_sec']:.2f}s | "
            f"{result['process_cpu_sec']:.2f}s | "
            f"{result['average_cores_during_decode']:.2f} | "
            f"{result['background_throughput_ratio']:.1%} | "
            f"{result['background_slowdown']:.1%} |"
        )
    (run_dir / "summary.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return run_dir


def measure_sensevoice_memory(args: argparse.Namespace) -> Path:
    """Measure SenseVoice child and parent+child private memory in a fresh run."""
    artifacts = load_artifacts(args.artifacts)
    items = load_corpus_manifest(args.manifest)
    run_dir = args.out_dir / datetime.now().strftime(
        "sensevoice-memory-%Y%m%d-%H%M%S"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    parent_before = _windows_process_memory(os.getpid()) or {}
    result = _run_sensevoice(
        items,
        artifacts=artifacts,
        memory_ceiling_mb=args.memory_ceiling_mb,
        timeout_sec=args.timeout_sec,
    )
    if result.get("status") != "OK":
        raise RuntimeError(result.get("message", "SenseVoice memory run failed"))
    parent_after = _windows_process_memory(os.getpid()) or {}
    child_peak = max(
        int(trial["peak_private_bytes"]) for trial in result["trials"]
    )
    tree_peak = max(
        int(trial["peak_process_tree_private_bytes"])
        for trial in result["trials"]
    )
    payload = {
        "captured_at": datetime.now(UTC).isoformat(),
        "method": "fresh benchmark parent plus external SenseVoice CLI child",
        "parent_before_private_bytes": parent_before.get("private_bytes"),
        "parent_after_private_bytes": parent_after.get("private_bytes"),
        "child_peak_private_bytes": child_peak,
        "process_tree_peak_private_bytes": tree_peak,
        "result": result,
    }
    _write_json(run_dir / "results.json", payload)
    (run_dir / "summary.md").write_text(
        "\n".join(
            [
                "# SenseVoice process-tree memory",
                "",
                f"- CLI child peak private: {child_peak / 1024 / 1024:.0f} MiB",
                f"- Parent + CLI peak private: {tree_peak / 1024 / 1024:.0f} MiB",
                f"- Parent idle private before CLI: "
                f"{int(parent_before.get('private_bytes', 0)) / 1024 / 1024:.0f} MiB",
                f"- Parent idle private after all CLIs exit: "
                f"{int(parent_after.get('private_bytes', 0)) / 1024 / 1024:.0f} MiB",
                "- Parent is a fresh benchmark process with the same Python app imports; "
                "this is a closer integration estimate than CLI-only memory.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return run_dir


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-sensevoice")
    prepare.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    measure = subparsers.add_parser("run")
    measure.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    measure.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    measure.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    measure.add_argument("--normal-runs", type=int, default=2)
    measure.add_argument("--load-runs", type=int, default=1)
    measure.add_argument("--cpu-load-processes", type=int, default=4)
    measure.add_argument("--memory-ceiling-mb", type=int, default=4096)
    measure.add_argument("--timeout-sec", type=int, default=600)
    verify = subparsers.add_parser("verify-app")
    verify.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    verify.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    compare = subparsers.add_parser("compare-text")
    compare.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    compare.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    compare.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    compare.add_argument("--memory-ceiling-mb", type=int, default=4096)
    compare.add_argument("--timeout-sec", type=int, default=600)
    contention = subparsers.add_parser("resource-contention")
    contention.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    contention.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    contention.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    contention.add_argument("--logical-cpus", type=int, default=4)
    contention.add_argument("--workers", type=int, default=4)
    contention.add_argument("--duration-sec", type=float, default=10.0)
    contention.add_argument("--memory-ceiling-mb", type=int, default=4096)
    contention.add_argument("--timeout-sec", type=int, default=600)
    sense_memory = subparsers.add_parser("sense-memory")
    sense_memory.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    sense_memory.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    sense_memory.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    sense_memory.add_argument("--memory-ceiling-mb", type=int, default=4096)
    sense_memory.add_argument("--timeout-sec", type=int, default=600)
    args = parser.parse_args(argv)
    for name in (
        "normal_runs",
        "load_runs",
        "cpu_load_processes",
        "memory_ceiling_mb",
        "timeout_sec",
        "logical_cpus",
        "workers",
        "duration_sec",
    ):
        if hasattr(args, name) and getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "prepare-sensevoice":
        path = prepare_sensevoice(args.artifacts.resolve())
        print(f"SenseVoice artifacts: {path}")
        return 0
    if args.command == "verify-app":
        run_dir = verify_app_path(args.manifest.resolve(), args.out_dir.resolve())
        print(f"app verification: {run_dir / 'summary.md'}")
        return 0
    if args.command == "compare-text":
        run_dir = compare_transcripts(
            args.manifest.resolve(),
            args.artifacts.resolve(),
            args.out_dir.resolve(),
            memory_ceiling_mb=args.memory_ceiling_mb,
            timeout_sec=args.timeout_sec,
        )
        print(f"transcript comparison: {run_dir / 'comparison.md'}")
        return 0
    if args.command == "resource-contention":
        run_dir = measure_resource_contention(args)
        print(f"resource contention: {run_dir / 'summary.md'}")
        return 0
    if args.command == "sense-memory":
        run_dir = measure_sensevoice_memory(args)
        print(f"SenseVoice memory: {run_dir / 'summary.md'}")
        return 0
    run_dir = run(args)
    print(f"private comparison: {run_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

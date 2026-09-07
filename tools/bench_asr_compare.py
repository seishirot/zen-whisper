"""Freeze and evaluate existing local ASR corpora through the application's backend.

All execution artifacts stay under tools/bench_outputs. No downloads, recording,
hotkeys, paste, or changes to the user's app/configuration are performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import statistics
import subprocess
import sys
import threading
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUTS = ROOT / "tools/bench_outputs"
PUBLIC = OUTPUTS / "reazon-production/corpus/public/manifest.json"
PRIVATE = OUTPUTS / "private/realwork-v1/manifest.json"
PROFILES = {
    "reazon-k2": {"engine": "reazon-k2"},
    "parakeet-0.6b-q8-tdt": {"engine": "crispasr", "crispasr_model": "parakeet-ja-0.6b-q8", "crispasr_decoder": "tdt"},
    "parakeet-0.6b-q8-ctc": {"engine": "crispasr", "crispasr_model": "parakeet-ja-0.6b-q8", "crispasr_decoder": "ctc"},
    "parakeet-0.6b-f16-tdt": {"engine": "crispasr", "crispasr_model": "parakeet-ja-0.6b-f16", "crispasr_decoder": "tdt"},
    "parakeet-1.1b-q8-ctc": {"engine": "crispasr", "crispasr_model": "parakeet-ctc-ja-1.1b-q8", "crispasr_decoder": "ctc"},
    "qwen-1.7b-q8": {"engine": "crispasr", "crispasr_model": "qwen3-1.7b-q8"},
    "qwen-1.7b-f16": {"engine": "crispasr", "crispasr_model": "qwen3-1.7b-f16"},
    "qwen-0.6b-q8": {"engine": "crispasr", "crispasr_model": "qwen3-0.6b-q8"},
    "qwen-hf-1.7b": {"engine": "qwen3-asr", "qwen3_model": "Qwen/Qwen3-ASR-1.7B-hf", "qwen3_max_new_tokens": 512, "qwen3_torch_compile": False, "qwen3_attn_implementation": "sdpa"},
    "qwen-hf-0.6b": {"engine": "qwen3-asr", "qwen3_model": "Qwen/Qwen3-ASR-0.6B-hf", "qwen3_max_new_tokens": 512, "qwen3_torch_compile": False, "qwen3_attn_implementation": "sdpa"},
    "whisper-turbo": {"engine": "whisper", "model_size": "large-v3-turbo", "compute_type": "float16", "beam_size": 5},
}


def local_output(path: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(OUTPUTS.resolve()):
        raise ValueError("Private evaluation artifacts must stay in tools/bench_outputs")
    return path


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, data: object) -> None:
    local_output(path).parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def retained_tokens(reference: str, hypothesis: str, pattern: str, *, fold: bool = False) -> dict:
    reference, hypothesis = (unicodedata.normalize("NFKC", text) for text in (reference, hypothesis))
    if fold:
        reference, hypothesis = reference.casefold(), hypothesis.casefold()
    expected, actual = Counter(re.findall(pattern, reference)), Counter(re.findall(pattern, hypothesis))
    return {"expected": sum(expected.values()), "retained": sum((expected & actual).values())}


def score(reference: str, hypothesis: str) -> dict:
    from tools.bench_crispasr import normalized, edit_distance
    from tools.bench_reazon_production import edit_breakdown

    expected, actual = normalized(reference), normalized(hypothesis)
    edits = edit_distance(expected, actual)
    breakdown = edit_breakdown(expected, actual)
    latin = r"[A-Za-z][A-Za-z0-9]*(?:[._+/-][A-Za-z0-9]+)*"
    numeric = r"[0-9]+(?:[.,][0-9]+)*|[〇零一二三四五六七八九十百千万億兆]+"
    return {
        "reference_chars": len(expected), "hypothesis_chars": len(actual), "edits": edits,
        "cer": edits / max(1, len(expected)), "exact": expected == actual,
        "operations": breakdown,
        "suspected_omission": breakdown["deletions"] >= max(5, len(expected) * 0.3),
        "latin_surface": retained_tokens(reference, hypothesis, latin),
        "latin_casefold": retained_tokens(reference, hypothesis, latin, fold=True),
        "numeric_surface": retained_tokens(reference, hypothesis, numeric),
    }


def summarize(rows: list[dict]) -> dict:
    from tools.bench_reazon_production import percentile95

    completed = [row for row in rows if row["status"] == "ok"]
    usable = [row for row in completed if not row["quality"]["suspected_omission"]]
    quality = [row["quality"] for row in rows]
    result = {
        "count": len(rows), "failures": len(rows) - len(completed),
        "suspected_omissions": len(completed) - len(usable),
        "corpus_cer": sum(q["edits"] for q in quality) / max(1, sum(q["reference_chars"] for q in quality)),
        "mean_clip_cer": statistics.mean(q["cer"] for q in quality) if quality else None,
        "exact_count": sum(q["exact"] for q in quality),
        "reference_chars": sum(q["reference_chars"] for q in quality),
        "edits": sum(q["edits"] for q in quality),
        "median_all_completed_sec": statistics.median(row["elapsed_sec"] for row in completed) if completed else None,
        "median_sec": statistics.median(row["elapsed_sec"] for row in usable) if usable else None,
        "p95_sec": percentile95(row["elapsed_sec"] for row in usable),
        "median_rtf": statistics.median(row["elapsed_sec"] / row["duration_sec"] for row in usable) if usable else None,
        "p95_rtf": percentile95(row["elapsed_sec"] / row["duration_sec"] for row in usable),
    }
    for name in ("latin_surface", "latin_casefold", "numeric_surface"):
        expected = sum(q[name]["expected"] for q in quality)
        retained = sum(q[name]["retained"] for q in quality)
        result[name] = {"expected": expected, "retained": retained, "rate": retained / expected if expected else None}
    return result


def freeze(path: Path) -> None:
    import numpy as np
    import soundfile as sf

    path = local_output(path)
    if path.exists():
        raise ValueError("Reuse the existing frozen manifest or choose a new path")
    items, sources = [], []
    for corpus_id, source, kind, count in (("public", PUBLIC, "public", 30), ("private", PRIVATE, "natural", 20)):
        if not source.exists() and corpus_id == "private":
            sources.append({"id": corpus_id, "available": False})
            continue
        raw = json.loads(source.read_text(encoding="utf-8"))
        selected = [row for row in raw["items"] if row["kind"] == kind]
        if len(selected) != count:
            raise ValueError(f"{corpus_id}: expected {count} fixed inputs")
        sources.append({"id": corpus_id, "available": True, "path": source.relative_to(ROOT).as_posix(), "sha256": digest(source), "count": len(selected)})
        for row in selected:
            audio_path = (source.parent / row["audio"]).resolve()
            if digest(audio_path) != row["wav_sha256"]:
                raise ValueError(f"{row['id']}: waveform checksum mismatch")
            info = sf.info(audio_path)
            if info.samplerate != 16000 or info.channels != 1:
                raise ValueError("Expected mono 16 kHz corpus")
            items.append({"id": row["id"], "corpus": corpus_id, "audio": audio_path.relative_to(ROOT).as_posix(),
                          "sha256": row["wav_sha256"], "reference": row["reference"], "duration_sec": info.frames / 16000})
    public = [row for row in items if row["corpus"] == "public"]
    boundaries = []
    path.parent.mkdir(parents=True, exist_ok=True)
    for seconds in (12, 25, 60):
        pieces, references, recipe, size = [], [], [], 0
        for row in public * 3:
            audio, _ = sf.read(ROOT / row["audio"], dtype="float32")
            if size + len(audio) + 1600 > seconds * 16000:
                continue
            pieces.extend((audio, np.zeros(1600, dtype=np.float32)))
            references.append(row["reference"])
            recipe.append({"id": row["id"], "start_sample": size, "end_sample": size + len(audio)})
            size += len(audio) + 1600
        pieces.append(np.zeros(seconds * 16000 - size, dtype=np.float32))
        audio_path = path.parent / f"boundary-{seconds}s.wav"
        sf.write(audio_path, np.concatenate(pieces), 16000, subtype="FLOAT")
        boundaries.append({"id": f"boundary-{seconds}s", "corpus": "boundary", "audio": audio_path.relative_to(ROOT).as_posix(),
                           "sha256": digest(audio_path), "reference": "".join(references), "last_reference": references[-1],
                           "recipe": recipe, "duration_sec": seconds})
    representatives = [public[i]["id"] for i in (0, 5, 6)]
    private = [row for row in items if row["corpus"] == "private"]
    representatives += [row["id"] for row in private[:2]] if private else [public[i]["id"] for i in (8, 15)]
    write_json(path, {"schema": 1, "sources": sources, "items": items + boundaries,
                     "selections": {"smoke": [row["id"] for row in public[:5]], "representative": representatives},
                     "privacy": "local-only references; worker inputs exclude reference text"})
    print(json.dumps({"manifest": str(path), "public_count": len(public), "private_count": len(private)}), flush=True)


class TreeMemory:
    def __init__(self, transcriber):
        self.transcriber = transcriber
        self.stop = threading.Event()
        self.peak_private = self.peak_working = self.samples = 0
        self.per_pid = {}
        self.thread = threading.Thread(target=self.poll, daemon=True)

    def snapshot(self):
        from tools.bench_reazon_production import _windows_process_memory

        pids = {os.getpid()}
        process = getattr(self.transcriber._backend, "_process", None)
        if process is not None and process.poll() is None:
            pids.add(process.pid)
        measurements = {pid: _windows_process_memory(pid) for pid in pids}
        if any(value is None for value in measurements.values()):
            return None
        private = sum(value["private_bytes"] for value in measurements.values())
        working = sum(value["working_set_bytes"] for value in measurements.values())
        self.peak_private, self.peak_working = max(self.peak_private, private), max(self.peak_working, working)
        self.per_pid.update(measurements)
        self.samples += 1
        return {"private_bytes": private, "working_set_bytes": working, "per_pid": measurements}

    def poll(self):
        while not self.stop.is_set():
            self.snapshot()
            self.stop.wait(0.05)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.snapshot()
        return {"samples": self.samples, "peak_private_bytes": self.peak_private,
                "peak_working_set_bytes": self.peak_working, "per_pid_last_sample": self.per_pid}


def worker(request_path: Path) -> int:
    from unittest.mock import patch
    import atexit
    import numpy as np
    import soundfile as sf
    from src.config import AppConfig, RecognitionConfig
    from tools.bench_private_asr import _available_affinity_mask, _set_affinity, _timed_cpu_workers, _collect_cpu_workers
    from tools.bench_crispasr import GPUProbe, process_snapshot
    import src.main as main_module

    request = json.loads(request_path.read_text(encoding="utf-8"))
    configuration = RecognitionConfig(**request["configuration"])
    affinity = _available_affinity_mask(request["affinity_cpus"]) if request["affinity_cpus"] else None
    if affinity:
        _set_affinity(affinity[1])
    app_cfg = AppConfig(recognition=configuration)
    app_cfg.feedback.sound_enabled = False
    app_cfg.logging.file = str(request_path.parent / "app.local.log")
    # Build the real App object without entering run(), registering hotkeys, or recording.
    with patch.object(main_module, "load_config", return_value=app_cfg), patch.object(main_module, "load_profiles", return_value={}), patch.object(main_module, "load_postprocessors", return_value={}), patch.object(main_module, "log_available_devices", return_value=None):
        app = main_module.App()
    atexit.unregister(app._cleanup)
    runner = app.transcriber
    monitor, probe = TreeMemory(runner), None
    result = {"status": "running", "outputs": [], "affinity_masks": affinity, "configuration": asdict(configuration)}
    monitor.thread.start()
    try:
        result["frontend_memory"] = monitor.snapshot()
        started = time.perf_counter()
        runner.load_model(configuration)
        result["cold_load_sec"] = time.perf_counter() - started
        result["resident_after_load"] = monitor.snapshot()
        backend = runner._backend
        native_pid = getattr(backend, "evidence", {}).get("pid", os.getpid())
        if affinity:
            import win32api, win32process
            handle = win32api.OpenProcess(0x0400, False, native_pid)
            try:
                result["native_affinity_mask"] = win32process.GetProcessAffinityMask(handle)[0]
            finally:
                handle.Close()
            if result["native_affinity_mask"] != affinity[1]:
                raise RuntimeError("Native worker did not inherit the requested CPU affinity")
        result["modules_after_load"] = {
            "host": process_snapshot(os.getpid())["nvidia_modules"],
            "worker": process_snapshot(native_pid)["nvidia_modules"] if native_pid != os.getpid() else [],
        }
        if configuration.engine == "whisper":
            result["actual_compute_type"] = backend._model.model.compute_type
            result["actual_device"] = backend._model.model.device
            if result["actual_device"] != configuration.device:
                raise RuntimeError("Whisper device differs from the requested device")
        if configuration.engine == "qwen3-asr":
            import torch, transformers
            result["actual_compute_type"] = str(backend._model.dtype)
            result["actual_device"] = str(backend._model.device)
            result["runtime_versions"] = {"torch": torch.__version__, "cuda": torch.version.cuda,
                                          "transformers": transformers.__version__}
            if backend._model.device.type != configuration.device:
                raise RuntimeError("Qwen device differs from the requested device")
        if configuration.device == "cuda":
            probe = GPUProbe(native_pid)
        jobs = request["jobs"]
        first, _ = sf.read(ROOT / jobs[0]["audio"], dtype="float32")
        started = time.perf_counter()
        runner.transcribe(first, "ja", configuration)
        result["first_recognition_sec"] = time.perf_counter() - started
        result["resident_after_warmup"] = monitor.snapshot()

        def recognize(job, repeat):
            audio, rate = sf.read(ROOT / job["audio"], dtype="float32")
            assert rate == 16000 and audio.ndim == 1
            started = time.perf_counter()
            text = runner.transcribe(audio, "ja", configuration)
            result["outputs"].append({"id": job["id"], "repeat": repeat, "text": text,
                                      "status": "ok" if text.strip() else "empty", "elapsed_sec": time.perf_counter() - started})

        if request["contention_seconds"]:
            seconds = request["contention_seconds"]
            baseline = _timed_cpu_workers(4, start_at=time.perf_counter() + 0.5, duration_sec=seconds)
            result["background_iterations_alone"] = _collect_cpu_workers(baseline)
            start = time.perf_counter() + 0.5
            competitors = _timed_cpu_workers(4, start_at=start, duration_sec=seconds)
            time.sleep(max(0, start - time.perf_counter()))
            index = 0
            while time.perf_counter() < start + seconds:
                recognize(jobs[index % len(jobs)], index // len(jobs))
                index += 1
            result["background_iterations_with_asr"] = _collect_cpu_workers(competitors)
            result["contention_window_sec"] = seconds
        else:
            for repeat in range(request["repeats"]):
                for job in jobs:
                    recognize(job, repeat)
        result["silence_text"] = runner.transcribe(np.zeros(16000, dtype=np.float32), "ja", configuration)
        result["empty_text"] = runner.transcribe(np.array([], dtype=np.float32), "ja", configuration)
        result["modules"] = {"host": process_snapshot(os.getpid())["nvidia_modules"],
                             "worker": process_snapshot(native_pid)["nvidia_modules"] if native_pid != os.getpid() else []}
        if configuration.device == "cpu" and (any(result["modules"].values()) or any(result["modules_after_load"].values())):
            raise RuntimeError("CPU-only process loaded NVIDIA modules")
        result["evidence"] = getattr(backend, "evidence", {})
        if configuration.device == "cuda":
            import shutil
            gpu_rows = subprocess.check_output([shutil.which("nvidia-smi"), "--query-compute-apps=pid,gpu_uuid", "--format=csv,noheader"], text=True)
            result["gpu_process_uuids"] = [row.split(",")[1].strip() for row in gpu_rows.splitlines() if row.split(",")[0].strip() == str(native_pid)]
            if not result["gpu_process_uuids"]:
                raise RuntimeError("The requested GPU process was not observable")
        result["status"] = "passed"
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, error=str(exc))
    finally:
        result["memory"] = monitor.close()
        if probe:
            result["gpu_counters"] = probe.close()
        runner.unload()
        write_json(request_path.parent / "inference.local.json", result)
    return 0 if result["status"] == "passed" else 1


def run(args) -> None:
    from src.config import RecognitionConfig
    from src.asr.crispasr import _ProcessJob
    from tools.bench_crispasr import normalized
    from tools.bench_reazon_production import _git_output, _physical_core_count, _total_ram_bytes

    if args.device == "cuda" and args.profile == "reazon-k2":
        raise ValueError("This Reazon K2 profile is CPU-only")
    plan = json.loads(args.manifest.read_text(encoding="utf-8"))
    selected = set(plan["selections"][args.selection]) if args.selection in ("smoke", "representative") else None
    items = [row for row in plan["items"] if (row["id"] in selected if selected else row["corpus"] in args.corpora)]
    if not items:
        raise ValueError("No selected inputs")
    for item in items:
        if digest(ROOT / item["audio"]) != item["sha256"]:
            raise ValueError("Frozen audio checksum changed")
    output = local_output(args.output)
    if (output / "summary.json").exists():
        raise ValueError("Choose a new output directory; existing result will not be overwritten")
    cfg = RecognitionConfig(**PROFILES[args.profile], device=args.device, language="ja", cpu_threads=args.threads,
                            reazon_inference_threads=args.threads, reazon_precision="int8-fp32",
                            reazon_chunk_sec=25, reazon_trailing_silence_sec=0.5, crispasr_gpu_device=args.gpu_device)
    admission = None
    if args.device == "cuda":
        import shutil
        raw = subprocess.check_output([shutil.which("nvidia-smi"), "--query-gpu=index,name,uuid,memory.free,utilization.gpu", "--format=csv,noheader,nounits"], text=True)
        selected_gpu = next(line for line in raw.splitlines() if int(line.split(",")[0]) == args.gpu_device)
        index, name, uuid, free, usage = [value.strip() for value in selected_gpu.split(",")]
        required = 6000 if args.profile == "qwen-hf-1.7b" else 4000 if args.profile == "qwen-hf-0.6b" else 12000 if args.profile == "qwen-1.7b-f16" else 8600 if args.profile == "qwen-1.7b-q8" else 4600 if args.profile == "qwen-0.6b-q8" else 3500 if args.profile == "whisper-turbo" else 2200
        admission = {"index": int(index), "name": name, "uuid": uuid, "free_mib": int(free), "required_free_mib": required, "utilization": int(usage)}
        if int(free) < required or int(usage) > 35:
            print(json.dumps({"status": "deferred-resource-busy", "gpu": admission}), flush=True)
            raise SystemExit(2)
        cfg.cuda_gpu_uuid = uuid
    request = {"configuration": asdict(cfg), "jobs": [{key: row[key] for key in ("id", "audio")} for row in items],
               "repeats": args.repeats, "affinity_cpus": args.affinity_cpus, "contention_seconds": args.contention_seconds}
    request_path = output / "request.local.json"
    write_json(request_path, request)
    env = dict(os.environ, HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1", CUDA_VISIBLE_DEVICES=admission["uuid"] if admission else "-1",
               OMP_NUM_THREADS=str(args.threads), OPENBLAS_NUM_THREADS=str(args.threads))
    job = _ProcessJob()
    timeout_error = None
    code = 1
    try:
        with (output / "worker.local.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "worker", str(request_path)],
                                       env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process.pid)
            code = process.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timeout_error = "Owned worker timed out and its Job was closed"
    finally:
        job.close()
    inference_path = output / "inference.local.json"
    result = json.loads(inference_path.read_text(encoding="utf-8")) if inference_path.exists() else {"status": "failed", "error": "Worker exited before checkpoint", "outputs": []}
    if timeout_error:
        result.update(status="failed", error=timeout_error)
    by_id = {row["id"]: row for row in items}
    rows = []
    for raw in result.pop("outputs"):
        item = by_id[raw["id"]]
        row = {key: value for key, value in raw.items() if key != "text"}
        row.update(corpus=item["corpus"], duration_sec=item["duration_sec"], quality=score(item["reference"], raw["text"]))
        if "last_reference" in item:
            row["last_sentence_exact"] = normalized(item["last_reference"]) in normalized(raw["text"])
        rows.append(row)
    completed_keys = {(row["id"], row["repeat"]) for row in rows}
    if not args.contention_seconds:
        for repeat in range(args.repeats):
            for item in items:
                if (item["id"], repeat) not in completed_keys:
                    rows.append({"id": item["id"], "repeat": repeat, "corpus": item["corpus"], "duration_sec": item["duration_sec"], "status": "missing", "elapsed_sec": None, "quality": score(item["reference"], "")})
                    result["status"] = "failed"
    if admission and set(result.get("gpu_process_uuids", [])) != {admission["uuid"]}:
        result.update(status="failed", error="GPU UUID differs from the admitted device")
    result.update(profile=args.profile, manifest_sha256=digest(args.manifest), rows=rows,
                  runner_sha256=digest(Path(__file__)),
                  model_manifest_sha256=digest(ROOT / "src/model_manifest.json"),
                  crispasr_manifest_sha256=digest(ROOT / "src/crispasr_manifest.json"),
                  environment={"git_head": _git_output("rev-parse", "HEAD"),
                               "python": sys.version, "platform": platform.platform(),
                               "cpu": platform.processor(), "physical_cores": _physical_core_count(),
                               "logical_cores": os.cpu_count(), "total_ram_bytes": _total_ram_bytes()},
                  gpu_admission=admission,
                  selection=args.selection, repeats=args.repeats,
                  aggregates={corpus: summarize([row for row in rows if row["corpus"] == corpus]) for corpus in sorted({row["corpus"] for row in rows})})
    for check in ("silence", "empty"):
        text = result.pop(f"{check}_text", None)
        result[f"{check}_returned_empty"] = None if text is None else not text.strip()
    write_json(output / "summary.json", result)
    if not args.retain_transcripts and inference_path.exists():
        local_output(inference_path).unlink()
    print(json.dumps({"profile": args.profile, "status": result["status"], "aggregates": result["aggregates"], "output": str(output)}, ensure_ascii=False), flush=True)
    if code or result["status"] != "passed":
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("freeze")
    prepare.add_argument("manifest", type=Path)
    execute = commands.add_parser("run")
    execute.add_argument("--manifest", type=Path, required=True)
    execute.add_argument("--profile", choices=PROFILES, required=True)
    execute.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    execute.add_argument("--gpu-device", type=int, default=0)
    execute.add_argument("--threads", type=int, choices=(1, 4), default=4)
    execute.add_argument("--selection", choices=("smoke", "representative", "full"), default="full")
    execute.add_argument("--corpora", nargs="+", choices=("public", "private", "boundary"), default=["public", "private"])
    execute.add_argument("--repeats", type=int, choices=(1, 2, 3), default=1)
    execute.add_argument("--affinity-cpus", type=int, choices=(0, 4), default=0)
    execute.add_argument("--contention-seconds", type=int, choices=(0, 10), default=0)
    execute.add_argument("--retain-transcripts", action="store_true")
    execute.add_argument("--timeout", type=int, default=900)
    execute.add_argument("--output", type=Path, required=True)
    child = commands.add_parser("worker")
    child.add_argument("request", type=Path)
    args = parser.parse_args()
    if args.command == "run" and args.contention_seconds and (args.affinity_cpus != 4 or args.device != "cpu"):
        parser.error("Contention requires --affinity-cpus 4 and --device cpu")
    if args.command == "freeze":
        freeze(args.manifest)
    elif args.command == "worker":
        raise SystemExit(worker(args.request))
    else:
        run(args)


if __name__ == "__main__":
    main()

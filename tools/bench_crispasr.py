"""Local-only resident CrispASR smoke/benchmark entry point; downloads nothing."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import threading
import time
import unicodedata

import numpy as np
import soundfile as sf

from src.asr.crispasr import _ProcessJob
from src.asr.crispasr_assets import manifest
from src.config import RecognitionConfig
from src.transcriber import Transcriber

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "tools/bench_outputs/reazon-production/corpus/public/manifest.json"
OUTPUTS = ROOT / "tools/bench_outputs/crispasr"


def normalized(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).lower()
    return "".join(c for c in value if unicodedata.category(c)[0] not in ("P", "Z", "S") and not c.isspace())


def edit_distance(left: str, right: str) -> int:
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        previous, row = row, [i]
        for j, b in enumerate(right, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
    return row[-1]


def process_snapshot(pid: int) -> dict:
    import win32api
    import win32process

    handle = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
    try:
        memory = win32process.GetProcessMemoryInfo(handle)
        modules = [Path(win32process.GetModuleFileNameEx(handle, item)).name for item in win32process.EnumProcessModules(handle)]
        return {
            "peak_working_set_bytes": memory["PeakWorkingSetSize"],
            "private_bytes": memory.get("PrivateUsage", memory["PagefileUsage"]),
            "nvidia_modules": sorted(name for name in modules if re.match(r"(?:nvcuda|nvml|cudart|cublas|cudnn|ggml-cuda)", name, re.I)),
        }
    finally:
        handle.Close()


class GPUProbe:
    """Sample Windows GPU counters by PID; WDDM nvidia-smi memory is often N/A."""

    def __init__(self, pid: int) -> None:
        self.samples: list[dict] = []
        self.process = None
        self.reader = None
        self.job = None
        self.sample_ready = threading.Event()
        shell = shutil.which("pwsh")
        if not shell:
            return
        script = r'''
$ErrorActionPreference = 'Stop'
while ($true) {
    $match = 'pid_' + $env:ZEN_CRISP_PROBE_PID + '_*'
    $mem = @(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory | Where-Object Name -Like $match)
    $eng = @(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine | Where-Object Name -Like $match)
    $bytes = ($mem | Measure-Object DedicatedUsage -Sum).Sum
    $usage = ($eng | Measure-Object UtilizationPercentage -Maximum).Maximum
    @{ dedicated_bytes = $bytes; engine_percent = $usage } | ConvertTo-Json -Compress
    Start-Sleep -Milliseconds 150
}
'''
        self.job = _ProcessJob()
        try:
            self.process = subprocess.Popen(
                [shell, "-NoProfile", "-NonInteractive", "-Command", script],
                env=dict(os.environ, ZEN_CRISP_PROBE_PID=str(pid)), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.job.assign(self.process.pid)
        except BaseException:
            self.close()
            raise

        def read() -> None:
            for line in self.process.stdout:
                try:
                    self.samples.append(json.loads(line))
                    self.sample_ready.set()
                except ValueError:
                    continue

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        # Start observing before timing a short utterance; PowerShell/CIM startup
        # can otherwise outlast the complete smoke run. This is outside ASR timing.
        self.sample_ready.wait(timeout=3)

    def close(self) -> dict:
        if self.job is not None:
            self.job.close()
            self.job = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
            if self.reader is not None:
                self.reader.join(timeout=2)
            self.process.stdout.close()
            self.process = None
        return {
            "samples": len(self.samples),
            "peak_dedicated_bytes": max((s.get("dedicated_bytes") or 0 for s in self.samples), default=None),
            "peak_engine_percent": max((s.get("engine_percent") or 0 for s in self.samples), default=None),
        }


def run(args) -> dict:
    args.output = args.output.resolve()
    if not args.output.is_relative_to((ROOT / "tools/bench_outputs").resolve()):
        raise ValueError("Evaluation artifacts must stay in tools/bench_outputs")
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    items = corpus["items"][: args.limit]
    cfg = RecognitionConfig(
        engine="crispasr", device=args.runtime, crispasr_root=args.root,
        crispasr_model=args.model, crispasr_decoder=args.decoder,
        crispasr_gpu_device=args.gpu_device, cpu_threads=4,
        crispasr_timeout_sec=args.timeout,
    )
    runner = Transcriber()
    probe = None
    details = []
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "corpus_revision": corpus.get("dataset", {}).get("resolved_revision"),
        "status": "failed", "error": None,
        "configuration": {"runtime": args.runtime, "model": args.model, "decoder": args.decoder, "threads": 4, "hints": False, "postprocessing": False},
    }
    try:
        runner.load_model(cfg)
        backend = runner._backend
        summary["evidence"] = backend.evidence
        summary["after_load"] = process_snapshot(backend.evidence["pid"])
        if args.runtime == "cuda":
            probe = GPUProbe(backend.evidence["pid"])
        warm_audio, rate = sf.read(args.corpus.parent / items[0]["audio"], dtype="float32")
        assert rate == 16000
        started = time.perf_counter()
        warm_text = runner.transcribe(warm_audio, "ja", cfg)
        summary["warmup_sec"] = time.perf_counter() - started
        summary["warmup_nonempty"] = bool(warm_text)
        for item in items:
            audio, rate = sf.read(args.corpus.parent / item["audio"], dtype="float32")
            assert rate == 16000 and audio.ndim == 1
            started = time.perf_counter()
            text = runner.transcribe(audio, "ja", cfg)
            elapsed = time.perf_counter() - started
            reference = item["reference"]
            expected, actual = normalized(reference), normalized(text)
            details.append({
                "id": item["id"], "duration_sec": len(audio) / rate,
                "elapsed_sec": elapsed, "rtf": elapsed / (len(audio) / rate),
                "cer": edit_distance(expected, actual) / max(1, len(expected)),
                "exact": expected == actual, "japanese": bool(re.search(r"[ぁ-んァ-ヶ一-龯]", text)),
                "reference": reference, "transcript": text,
            })
            print(json.dumps({k: v for k, v in details[-1].items() if k not in ("reference", "transcript")}), flush=True)
        assert runner.transcribe(np.zeros(16000, dtype=np.float32), "ja", cfg) == ""
        assert runner.transcribe(np.array([], dtype=np.float32), "ja", cfg) == ""
        summary["after_decode"] = process_snapshot(backend.evidence["pid"])
        summary["status"] = "passed" if all(item["japanese"] for item in details) else "failed"
        summary["cases"] = [{k: v for k, v in row.items() if k not in ("reference", "transcript")} for row in details]
        summary["median_warm_sec"] = statistics.median(row["elapsed_sec"] for row in details)
        summary["median_rtf"] = statistics.median(row["rtf"] for row in details)
        summary["mean_cer"] = statistics.mean(row["cer"] for row in details)
        # Long recordings are explicitly requested and constructed locally from public clips.
        if args.boundaries:
            summary["boundaries"] = []
            for seconds in args.boundaries:
                exact_case = next((item for item in corpus["items"] if item["id"] == f"public-duration-{seconds:g}s"), None)
                if exact_case:
                    audio, rate = sf.read(args.corpus.parent / exact_case["audio"], dtype="float32")
                    reference = exact_case["reference"]
                    source = exact_case["id"]
                else:
                    # Finish complete utterances, then pad with silence. Avoid
                    # cutting a word or repeating a single phrase for 150 s.
                    pieces, references, samples = [], [], 0
                    source_items = [item for item in corpus["items"] if item.get("kind") == "public"]
                    for item in source_items * 3:
                        part, rate = sf.read(args.corpus.parent / item["audio"], dtype="float32")
                        if samples + len(part) > seconds * 16000:
                            continue
                        pieces.append(part)
                        references.append(item["reference"])
                        samples += len(part)
                    pieces.append(np.zeros(int(seconds * 16000) - samples, dtype=np.float32))
                    audio = np.concatenate(pieces)
                    reference = "".join(references)
                    source = "complete-public-utterances-plus-silence"
                started = time.perf_counter()
                text = runner.transcribe(audio, "ja", cfg)
                expected = normalized(reference)
                elapsed = time.perf_counter() - started
                summary["boundaries"].append({
                    "seconds": len(audio) / 16000, "elapsed_sec": elapsed,
                    "characters": len(text), "nonempty": bool(text), "source": source,
                    "cer": edit_distance(expected, normalized(text)) / max(1, len(expected)),
                    "last_reference_sentence_exact_match": normalized(reference.split(".")[-2]) in normalized(text) if "." in reference else None,
                })
                details.append({"id": f"boundary-{seconds:g}s", "reference": reference, "transcript": text})
        if args.runtime == "cpu" and (summary["after_load"]["nvidia_modules"] or summary["after_decode"]["nvidia_modules"]):
            raise RuntimeError("CPU process unexpectedly loaded NVIDIA modules")
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = type(exc).__name__
        print(f"Smoke failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if probe is not None:
            summary["gpu_process_counters"] = probe.close()
        runner.unload()
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.output / "transcripts.local.json").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--model", choices=tuple(manifest()["models"]), required=True)
    parser.add_argument("--decoder", choices=("auto", "tdt", "ctc"), default="auto")
    parser.add_argument("--root", default="")
    parser.add_argument("--gpu-device", type=int, default=0)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--boundaries", nargs="*", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.boundaries and any(not 0 < value <= 300 for value in args.boundaries):
        parser.error("boundaries must be in (0,300]")
    if args.output is None:
        args.output = OUTPUTS / f"{args.runtime}-{args.model}-{args.decoder}"
    result = run(args)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()

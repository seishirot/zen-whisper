"""CPU ASR benchmark helper for local Japanese transcription.

This script is intentionally independent from the tray app. It compares
low-memory CPU-oriented ASR candidates on the same audio file and writes each
transcript as plain text.

Examples:
    .venv\\Scripts\\python.exe tools\\bench_cpu_asr.py --audio tools\\samples\\bench_sample_ja.wav
    .venv\\Scripts\\python.exe tools\\bench_cpu_asr.py --targets faster-whisper,fast-kotoba
    .venv\\Scripts\\python.exe tools\\bench_cpu_asr.py --targets whisper-cpp --whisper-cpp-exe tools\\bin\\whisper-cli.exe --whisper-cpp-model tools\\models\\ggml-large-v3-turbo-q5_0.bin
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import re
import subprocess
import sys
import threading
import time
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Iterator

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

DEFAULT_AUDIO = _HERE / "samples" / "bench_sample_ja.wav"
DEFAULT_OUT_DIR = _HERE / "bench_outputs"
DEFAULT_TARGETS = ("faster-whisper", "fast-kotoba", "whisper-cpp", "reazon-k2")
KOTOBA_FASTER_MODEL = "kotoba-tech/kotoba-whisper-v2.0-faster"


@dataclass
class BenchResult:
    target: str
    status: str
    model: str
    transcript_path: Path | None = None
    load_sec: float | None = None
    run_secs: list[float] | None = None
    audio_sec: float | None = None
    peak_rss_mb: float | None = None
    chars: int = 0
    message: str = ""

    @property
    def best_sec(self) -> float | None:
        if not self.run_secs:
            return None
        return min(self.run_secs)

    @property
    def mean_sec(self) -> float | None:
        if not self.run_secs:
            return None
        return mean(self.run_secs)

    @property
    def best_rtf(self) -> float | None:
        if not self.best_sec or not self.audio_sec:
            return None
        return self.best_sec / self.audio_sec

    @property
    def mean_rtf(self) -> float | None:
        if not self.mean_sec or not self.audio_sec:
            return None
        return self.mean_sec / self.audio_sec


class MemoryMonitor:
    """Poll process working-set memory without requiring psutil."""

    def __init__(self, pid: int | None = None, interval_sec: float = 0.05) -> None:
        self.pid = pid or os.getpid()
        self.interval_sec = interval_sec
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "MemoryMonitor":
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        value = get_working_set_bytes(self.pid)
        if value is not None:
            self.peak_bytes = max(self.peak_bytes, value)

    def _poll(self) -> None:
        while not self._stop.is_set():
            value = get_working_set_bytes(self.pid)
            if value is not None:
                self.peak_bytes = max(self.peak_bytes, value)
            self._stop.wait(self.interval_sec)

    @property
    def peak_mb(self) -> float | None:
        if self.peak_bytes <= 0:
            return None
        return self.peak_bytes / (1024 * 1024)


def get_working_set_bytes(pid: int) -> int | None:
    if platform.system() == "Windows":
        return _get_windows_working_set_bytes(pid)
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux reports KB, macOS reports bytes.
        factor = 1024 if sys.platform.startswith("linux") else 1
        return int(usage.ru_maxrss * factor)
    except Exception:
        return None


def _get_windows_working_set_bytes(pid: int) -> int | None:
    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
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

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        )
        if not ok:
            return None
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def audio_duration_sec(path: Path) -> float | None:
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                return frames / rate if rate else None
        except wave.Error:
            pass

    try:
        import soundfile as sf

        info = sf.info(str(path))
        return info.frames / info.samplerate if info.samplerate else None
    except Exception:
        return None


def safe_name(value: str) -> str:
    value = value.replace("\\", "_").replace("/", "_")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "transcript"


def write_transcript(out_dir: Path, label: str, text: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe_name(label)}.txt"
    path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")
    return path


@contextmanager
def timed_memory(pid: int | None = None) -> Iterator[MemoryMonitor]:
    with MemoryMonitor(pid=pid) as monitor:
        yield monitor


def transcribe_faster_whisper(
    *,
    audio: Path,
    out_dir: Path,
    target: str,
    model_name: str,
    language: str,
    task: str,
    beam_size: int,
    cpu_threads: int,
    runs: int,
    warmup: bool,
    vad_filter: bool,
    condition_on_previous_text: bool,
    audio_sec: float | None,
) -> BenchResult:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        return BenchResult(
            target=target,
            status="SKIP",
            model=model_name,
            audio_sec=audio_sec,
            message=f"faster-whisper is not installed: {exc}",
        )

    try:
        with timed_memory() as memory:
            t0 = time.perf_counter()
            model = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=cpu_threads,
                num_workers=1,
            )
            load_sec = time.perf_counter() - t0

            def run_once() -> str:
                segments, _info = model.transcribe(
                    str(audio),
                    language=language,
                    task=task,
                    beam_size=beam_size,
                    vad_filter=vad_filter,
                    condition_on_previous_text=condition_on_previous_text,
                )
                return "".join(segment.text for segment in segments).strip()

            if warmup:
                run_once()

            run_secs: list[float] = []
            text = ""
            for _ in range(runs):
                t0 = time.perf_counter()
                text = run_once()
                run_secs.append(time.perf_counter() - t0)

        transcript = write_transcript(out_dir, target, text)
        return BenchResult(
            target=target,
            status="OK",
            model=model_name,
            transcript_path=transcript,
            load_sec=load_sec,
            run_secs=run_secs,
            audio_sec=audio_sec,
            peak_rss_mb=memory.peak_mb,
            chars=len(text),
        )
    except Exception as exc:
        return BenchResult(
            target=target,
            status="FAIL",
            model=model_name,
            audio_sec=audio_sec,
            message=repr(exc),
        )


def transcribe_whisper_cpp(
    *,
    audio: Path,
    out_dir: Path,
    exe: Path | None,
    model: Path | None,
    language: str,
    beam_size: int,
    cpu_threads: int,
    runs: int,
    extra_args: list[str],
    audio_sec: float | None,
) -> BenchResult:
    target = "whisper-cpp"
    model_label = str(model) if model else ""
    if exe is None or not exe.exists():
        return BenchResult(
            target=target,
            status="SKIP",
            model=model_label,
            audio_sec=audio_sec,
            message="whisper.cpp executable is not configured",
        )
    if model is None or not model.exists():
        return BenchResult(
            target=target,
            status="SKIP",
            model=model_label,
            audio_sec=audio_sec,
            message="whisper.cpp model file is not configured",
        )

    run_secs: list[float] = []
    text = ""
    peak_mb: float | None = None
    label = f"whisper-cpp_{model.stem}"

    for index in range(runs):
        out_stem = out_dir / f"{safe_name(label)}_run{index + 1}"
        out_txt = out_stem.with_suffix(".txt")
        if out_txt.exists():
            out_txt.unlink()
        cmd = [
            str(exe),
            "-m",
            str(model),
            "-f",
            str(audio),
            "-l",
            language,
            "-t",
            str(cpu_threads),
            "-bs",
            str(beam_size),
            "-otxt",
            "-of",
            str(out_stem),
            "-np",
            *extra_args,
        ]
        out_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with timed_memory(proc.pid) as memory:
                stdout, stderr = proc.communicate()
            elapsed = time.perf_counter() - t0
            if memory.peak_mb is not None:
                peak_mb = max(peak_mb or 0, memory.peak_mb)
            if proc.returncode != 0:
                return BenchResult(
                    target=target,
                    status="FAIL",
                    model=str(model),
                    audio_sec=audio_sec,
                    message=(
                        f"whisper.cpp exited with {proc.returncode}: "
                        f"{(stderr or stdout).strip()[:500]}"
                    ),
                )
            if out_txt.exists():
                text = out_txt.read_text(encoding="utf-8").strip()
            else:
                text = stdout.strip()
            run_secs.append(elapsed)
        except Exception as exc:
            return BenchResult(
                target=target,
                status="FAIL",
                model=str(model),
                audio_sec=audio_sec,
                message=repr(exc),
            )

    transcript = write_transcript(out_dir, label, text)
    return BenchResult(
        target=target,
        status="OK",
        model=str(model),
        transcript_path=transcript,
        load_sec=None,
        run_secs=run_secs,
        audio_sec=audio_sec,
        peak_rss_mb=peak_mb,
        chars=len(text),
    )


def transcribe_reazon_k2(
    *,
    audio: Path,
    out_dir: Path,
    runs: int,
    audio_sec: float | None,
) -> BenchResult:
    target = "reazon-k2"
    try:
        from reazonspeech.k2.asr import audio_from_path, load_model, transcribe
    except ImportError as exc:
        return BenchResult(
            target=target,
            status="SKIP",
            model="ReazonSpeech K2",
            audio_sec=audio_sec,
            message=f"ReazonSpeech K2 is not installed or API changed: {exc}",
        )

    try:
        with timed_memory() as memory:
            t0 = time.perf_counter()
            model = load_model(device="cpu", precision="fp32", language="ja")
            load_sec = time.perf_counter() - t0
            speech = audio_from_path(str(audio))

            run_secs: list[float] = []
            text = ""
            for _ in range(runs):
                t0 = time.perf_counter()
                result = transcribe(model, speech)
                run_secs.append(time.perf_counter() - t0)
                text = getattr(result, "text", str(result)).strip()

        transcript = write_transcript(out_dir, target, text)
        return BenchResult(
            target=target,
            status="OK",
            model="ReazonSpeech K2",
            transcript_path=transcript,
            load_sec=load_sec,
            run_secs=run_secs,
            audio_sec=audio_sec,
            peak_rss_mb=memory.peak_mb,
            chars=len(text),
        )
    except Exception as exc:
        return BenchResult(
            target=target,
            status="FAIL",
            model="ReazonSpeech K2",
            audio_sec=audio_sec,
            message=repr(exc),
        )


def fmt_sec(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def fmt_float(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def fmt_mb(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def print_results(results: list[BenchResult]) -> None:
    headers = [
        "target",
        "status",
        "load_s",
        "best_s",
        "best_rtf",
        "mean_rtf",
        "peak_mb",
        "chars",
        "text",
    ]
    rows = []
    for result in results:
        rows.append(
            [
                result.target,
                result.status,
                fmt_sec(result.load_sec),
                fmt_sec(result.best_sec),
                fmt_float(result.best_rtf),
                fmt_float(result.mean_rtf),
                fmt_mb(result.peak_rss_mb),
                str(result.chars),
                str(result.transcript_path or result.message),
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    print()
    print(" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def split_targets(value: str) -> tuple[str, ...]:
    aliases = {
        "all": DEFAULT_TARGETS,
        "kotoba": ("fast-kotoba",),
        "kotoba-faster": ("fast-kotoba",),
    }
    targets: list[str] = []
    for part in (part.strip() for part in value.split(",") if part.strip()):
        expanded = aliases.get(part, (part,))
        for target in expanded:
            if target not in targets:
                targets.append(target)
    valid = set(DEFAULT_TARGETS)
    unknown = [target for target in targets if target not in valid]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown target(s): {', '.join(unknown)}; valid: {', '.join(DEFAULT_TARGETS)}"
        )
    return tuple(targets)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark CPU-oriented Japanese ASR candidates.",
    )
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--targets",
        type=split_targets,
        default=DEFAULT_TARGETS,
        help=(
            "Comma-separated targets. Valid: faster-whisper, fast-kotoba, "
            "whisper-cpp, reazon-k2, all. Aliases: kotoba, kotoba-faster."
        ),
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--task", default="transcribe")
    parser.add_argument("--beam-size", type=int, default=1)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--no-vad-filter", action="store_true")
    parser.add_argument("--condition-on-previous-text", action="store_true")
    parser.add_argument("--faster-model", default="large-v3-turbo")
    parser.add_argument("--kotoba-model", default=KOTOBA_FASTER_MODEL)
    parser.add_argument(
        "--whisper-cpp-exe",
        type=Path,
        default=Path(os.environ["WHISPER_CPP_EXE"])
        if os.environ.get("WHISPER_CPP_EXE")
        else _HERE / "bin" / "whisper-cli.exe",
    )
    parser.add_argument(
        "--whisper-cpp-model",
        type=Path,
        default=Path(os.environ["WHISPER_CPP_MODEL"])
        if os.environ.get("WHISPER_CPP_MODEL")
        else None,
    )
    parser.add_argument(
        "--whisper-cpp-extra-arg",
        action="append",
        default=[],
        help="Extra argument passed to whisper-cli.exe. Repeat for multiple args.",
    )
    args = parser.parse_args(argv)

    if args.runs <= 0:
        parser.error("--runs must be positive")
    if args.beam_size <= 0:
        parser.error("--beam-size must be positive")
    if args.cpu_threads <= 0:
        parser.error("--cpu-threads must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    audio = args.audio.resolve()
    out_dir = args.out_dir.resolve()
    if not audio.exists():
        print(f"audio file not found: {audio}", file=sys.stderr)
        return 2

    audio_sec = audio_duration_sec(audio)
    print(f"audio: {audio}")
    print(f"duration: {fmt_sec(audio_sec)}s")
    print(f"targets: {', '.join(args.targets)}")
    print(
        "settings: "
        f"language={args.language}, beam={args.beam_size}, "
        f"threads={args.cpu_threads}, runs={args.runs}, "
        f"faster_vad={not args.no_vad_filter}"
    )
    if args.whisper_cpp_extra_arg:
        print(f"whisper_cpp_extra: {' '.join(args.whisper_cpp_extra_arg)}")

    results: list[BenchResult] = []
    for target in args.targets:
        print(f"\n[{target}] running...")
        if target == "faster-whisper":
            result = transcribe_faster_whisper(
                audio=audio,
                out_dir=out_dir,
                target=target,
                model_name=args.faster_model,
                language=args.language,
                task=args.task,
                beam_size=args.beam_size,
                cpu_threads=args.cpu_threads,
                runs=args.runs,
                warmup=args.warmup,
                vad_filter=not args.no_vad_filter,
                condition_on_previous_text=args.condition_on_previous_text,
                audio_sec=audio_sec,
            )
        elif target == "fast-kotoba":
            result = transcribe_faster_whisper(
                audio=audio,
                out_dir=out_dir,
                target=target,
                model_name=args.kotoba_model,
                language=args.language,
                task=args.task,
                beam_size=args.beam_size,
                cpu_threads=args.cpu_threads,
                runs=args.runs,
                warmup=args.warmup,
                vad_filter=not args.no_vad_filter,
                condition_on_previous_text=args.condition_on_previous_text,
                audio_sec=audio_sec,
            )
        elif target == "whisper-cpp":
            result = transcribe_whisper_cpp(
                audio=audio,
                out_dir=out_dir,
                exe=args.whisper_cpp_exe.resolve()
                if args.whisper_cpp_exe is not None
                else None,
                model=args.whisper_cpp_model.resolve()
                if args.whisper_cpp_model is not None
                else None,
                language=args.language,
                beam_size=args.beam_size,
                cpu_threads=args.cpu_threads,
                runs=args.runs,
                extra_args=args.whisper_cpp_extra_arg,
                audio_sec=audio_sec,
            )
        elif target == "reazon-k2":
            result = transcribe_reazon_k2(
                audio=audio,
                out_dir=out_dir,
                runs=args.runs,
                audio_sec=audio_sec,
            )
        else:
            raise AssertionError(target)

        results.append(result)
        if result.status == "OK":
            print(f"  OK: {result.transcript_path}")
        else:
            print(f"  {result.status}: {result.message}")

    print_results(results)
    return 1 if any(result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

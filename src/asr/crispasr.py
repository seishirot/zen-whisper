"""Optional Windows CrispASR adapter with one authenticated, resident server."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import http.client
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np

from src.asr.base import RecognitionHints
from src.asr.crispasr_assets import (
    decoder_for, installation_root, manifest, profile_spec, resolve_installation,
)
from src.config import ASR_SAMPLE_RATE, RecognitionConfig
from src.gpu import GPUSelectionError, admit_crispasr

logger = logging.getLogger(__name__)
HINTS_NOTICE = "CrispASR: このadapterは文脈・hotwordsを認識に反映しません（後処理は別設定）"
MAX_AUDIO_SECONDS = 300
MAX_RESPONSE_BYTES = 1024 * 1024


def configuration_error(cfg: RecognitionConfig) -> str:
    try:
        if sys.platform != "win32" or struct.calcsize("P") != 8:
            raise ValueError("CrispASRはWindows x64専用です")
        for name in ("cpu_threads", "crispasr_max_tokens", "crispasr_timeout_sec", "crispasr_gpu_device"):
            value = getattr(cfg, name)
            minimum = 0 if name == "crispasr_gpu_device" else 1
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"CrispASR: {name} は {minimum} 以上の整数で指定してください")
        timeout = cfg.model_load_timeout_sec
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("CrispASR: モデル読込期限は有限の正数で指定してください")
        profile = profile_spec(cfg.crispasr_model)
        decoder_for(profile, cfg.crispasr_decoder)
        if cfg.language not in profile["languages"]:
            raise ValueError("CrispASR: 選択モデルはこの言語に対応していません")
        resolve_installation(installation_root(cfg.crispasr_root), cfg.device, cfg.crispasr_model, verify=False)
    except (ValueError, OSError, TypeError) as exc:
        return str(exc)
    return ""


def float_wav(audio: np.ndarray) -> bytes:
    """Encode 16 kHz mono IEEE float WAV without quantizing the input."""
    data = audio.astype("<f4", copy=False).tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, ASR_SAMPLE_RATE, ASR_SAMPLE_RATE * 4, 4, 32)
    return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data


def multipart_audio(audio: np.ndarray, fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = "zen-crisp-" + secrets.token_hex(24)
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        for key, value in fields.items()
    ]
    parts += [
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode("ascii"),
        float_wav(audio), f"\r\n--{boundary}--\r\n".encode("ascii"),
    ]
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


class _ProcessJob:
    """Windows closes the whole owned process tree even if the app is killed."""

    def __init__(self) -> None:
        import win32job

        self._api = win32job
        self._handle = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(self._handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self._handle, win32job.JobObjectExtendedLimitInformation, info)

    def assign(self, pid: int) -> None:
        import win32api

        handle = win32api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        try:
            self._api.AssignProcessToJobObject(self._handle, handle)
        finally:
            handle.Close()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.Close()
            self._handle = None


class CrispASRBackend:
    name = "crispasr"
    supports_hints = False

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._job: _ProcessJob | None = None
        self._reader: threading.Thread | None = None
        self._logs: deque[str] = deque(maxlen=500)
        self._ready = False
        self._port = 0
        self._token = ""
        self._hint_warned = False
        self._profile: dict = {}
        self._decoder = ""
        self._cuda_selected = False
        self._oom = threading.Event()
        self._cuda_computed = threading.Event()
        self._ctc_selected = False
        self._device_name = ""
        self._build_verified = False
        self.evidence: dict = {}

    @property
    def is_ready(self) -> bool:
        return self._ready and self._process is not None and self._process.poll() is None

    def _request(self, path: str, *, timeout: float, body: bytes | None = None, content_type: str = "") -> dict:
        # HTTPConnection has no proxy lookup, redirect following, or remote URL input.
        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=timeout)
        try:
            headers = {"Authorization": "Bearer " + self._token}
            if content_type:
                headers["Content-Type"] = content_type
            connection.request("POST" if body is not None else "GET", path, body, headers)
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200 or len(data) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"CrispASR request failed: HTTP {response.status}")
            result = json.loads(data)
            if not isinstance(result, dict) or "error" in result:
                raise RuntimeError("CrispASR returned an invalid result")
            return result
        finally:
            connection.close()

    def _read_logs(self, pipe) -> None:
        try:
            for line in pipe:
                self._logs.append(line.rstrip()[:2000])
                if any(marker in line.casefold() for marker in ("out of memory", "cuda_error_out_of_memory", "cudaerrormemoryallocation")):
                    self._oom.set()
                if "crispasr_init_gpu_backend: using preferred GPU backend: CUDA0" in line:
                    self._cuda_selected = True
                if "ggml_backend_cuda_graph_compute: CUDA graph warmup complete" in line:
                    self._cuda_computed.set()
                if "crispasr[parakeet]: using CTC decoder" in line:
                    self._ctc_selected = True
                match = re.search(r"Device 0: (.+?), compute capability", line)
                if match:
                    self._device_name = match.group(1)
                if f"crispasr {manifest()['version']} (git {manifest()['commit'][:8]}," in line:
                    self._build_verified = True
        except (OSError, ValueError):
            pass  # The pipe is closed by unload after the child is reaped.

    def load(self, cfg: RecognitionConfig, on_timeout: Callable[[str], None] | None = None) -> None:
        self.unload()
        error = configuration_error(cfg)
        if error:
            raise ValueError(error)
        gpu = admit_crispasr(cfg) if cfg.device == "cuda" else None
        started = time.perf_counter()
        executable, model = resolve_installation(
            installation_root(cfg.crispasr_root), cfg.device, cfg.crispasr_model, verify=True,
        )
        self._profile = profile_spec(cfg.crispasr_model)
        self._decoder = decoder_for(self._profile, cfg.crispasr_decoder)
        self._token = secrets.token_urlsafe(32)
        self._logs.clear()
        self._oom.clear()
        self._cuda_selected = False
        self._cuda_computed.clear()
        self._ctc_selected = False
        self._device_name = ""
        self._build_verified = False
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self._port = reservation.getsockname()[1]
        # Avoid inherited CrispASR tuning, proxy, key, and device overrides.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CRISPASR_", "GGML_", "CUDA_"))}
        env.update({
            "CRISPASR_API_KEYS": self._token,
            "CUDA_VISIBLE_DEVICES": gpu.uuid if gpu is not None else "-1",
            "OMP_NUM_THREADS": str(cfg.cpu_threads), "OPENBLAS_NUM_THREADS": str(cfg.cpu_threads),
            "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
            "PATH": os.pathsep.join([str(executable.parent), str(Path(os.environ["SystemRoot"]) / "System32")]),
        })
        # The v0.8.32 CLI uses narrow fopen on Windows. Resolve the Unicode
        # directory through CreateProcessW's cwd, and pass only ASCII to fopen.
        model_argument = "./" + model.name
        args = [
            str(executable), "--server", "--host", "127.0.0.1", "--port", str(self._port),
            "--server-workers", "1", "--no-warmup", "--backend", self._profile["backend"],
            "--model", model_argument, "--language", cfg.language, "--threads", str(cfg.cpu_threads),
            "--gpu-backend", cfg.device, "--lid-backend", "off", "--no-auto-aligner",
            "--chunk-seconds", "10" if self._profile["backend"] == "parakeet" else "30",
            "--max-new-tokens", str(cfg.crispasr_max_tokens),
        ]
        if cfg.device == "cpu":
            args.append("--no-gpu")
        if self._profile["backend"] == "parakeet":
            args += ["--parakeet-decoder", self._decoder]
        try:
            self._job = _ProcessJob()
            self._process = subprocess.Popen(
                args, cwd=model.parent, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                shell=False, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._job.assign(self._process.pid)
            # NUL is a character device: MSVCRT _isatty(NUL) is true. The
            # upstream resolver treats its EOF as "yes" to a download prompt.
            # An EOF pipe is non-interactive and refuses missing-model downloads.
            self._process.stdin.close()
            self._reader = threading.Thread(target=self._read_logs, args=(self._process.stdout,), daemon=True)
            self._reader.start()
            deadline = time.monotonic() + cfg.model_load_timeout_sec
            entries = None
            while True:
                if self._process.poll() is not None:
                    raise RuntimeError(f"CrispASR worker exited during load (code={self._process.returncode})")
                if time.monotonic() >= deadline:
                    message = "CrispASRモデル読込がタイムアウトしました。workerを終了します"
                    if on_timeout:
                        on_timeout(message)
                    raise TimeoutError(message)
                if entries is None:
                    try:
                        models = self._request("/v1/models", timeout=min(0.5, max(0.01, deadline - time.monotonic())))
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.05)
                        continue
                    entries = models.get("data", [])
                    if len(entries) != 1 or entries[0].get("id") != model_argument:
                        raise RuntimeError("CrispASR worker identity/model mismatch")
                # HTTP readiness and stdout consumption are independent. Wait
                # for required evidence within the same startup deadline.
                if (
                    self._build_verified
                    and (cfg.device != "cuda" or self._cuda_selected)
                    and (self._decoder != "ctc" or self._profile["backend"] != "parakeet" or self._ctc_selected)
                ):
                    break
                if not self._reader.is_alive():
                    raise RuntimeError("CrispASR startup log ended before build/device/decoder verification")
                time.sleep(0.01)
            self._ready = True
            self.evidence = {
                "version": manifest()["version"], "commit": manifest()["commit"],
                "runtime": cfg.device, "model": cfg.crispasr_model, "precision": self._profile["precision"],
                "backend": entries[0].get("backend"), "decoder": self._decoder,
                "gpu_device_index": gpu.index if gpu is not None else None,
                "gpu_uuid": gpu.uuid if gpu is not None else None,
                "gpu_name": gpu.name if gpu is not None else None,
                "pid": self._process.pid, "cold_load_sec": time.perf_counter() - started,
                "hints_supported": False, "requests_completed": 0,
                "actual_device": self._device_name if cfg.device == "cuda" else "CPU",
                "cuda_backend_selected": self._cuda_selected,
                "cuda_graph_compute_logged": False,
            }
            logger.info("CrispASR ready: device=%s model=%s decoder=%s", cfg.device, cfg.crispasr_model, self._decoder)
        except BaseException as exc:
            self.unload()
            if isinstance(exc, Exception) and self._oom.is_set():
                raise GPUSelectionError("GPUのVRAM確保に失敗したためCrispASRを停止しました。空きを増やすか小さいモデルを選び、再読み込みしてください") from exc
            raise

    def transcribe(self, audio: np.ndarray, language: str, cfg: RecognitionConfig, hints: RecognitionHints | None = None) -> str:
        if not self.is_ready:
            raise RuntimeError("CrispASR workerが停止しています。モデルを再読み込みしてください")
        if language not in self._profile["languages"]:
            raise ValueError("CrispASR: 選択モデルはこの言語に対応していません")
        waveform = np.asarray(audio)
        if waveform.ndim != 1 or not np.issubdtype(waveform.dtype, np.floating) or not np.isfinite(waveform).all():
            raise ValueError("CrispASR expects finite, mono floating-point audio at 16 kHz")
        if len(waveform) > ASR_SAMPLE_RATE * MAX_AUDIO_SECONDS:
            raise ValueError("CrispASR: 音声は300秒以内にしてください")
        if hints and (hints.context or hints.hotwords) and not self._hint_warned:
            logger.warning(HINTS_NOTICE)
            self._hint_warned = True
        if not waveform.size or not np.any(waveform):
            return ""
        fields = {
            "language": language, "response_format": "json", "temperature": "0", "seed": "1",
            "max_new_tokens": str(cfg.crispasr_max_tokens), "beam_size": "1",
            "vad": "false", "detect_language": "false", "lid_backend": "off",
            "no_auto_aligner": "true", "stream": "false", "strict_pipeline": "true",
            "chunk_seconds": "10" if self._profile["backend"] == "parakeet" else "30",
        }
        if self._profile["backend"] == "parakeet":
            fields["parakeet_decoder"] = self._decoder
        body, content_type = multipart_audio(waveform, fields)
        try:
            result = self._request("/v1/audio/transcriptions", timeout=cfg.crispasr_timeout_sec, body=body, content_type=content_type)
            text = result.get("text")
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("CrispASR returned an empty or invalid transcript for non-silent audio")
            if self._process is None or self._process.poll() is not None:
                raise RuntimeError("CrispASR worker exited during transcription")
            if self.evidence["runtime"] == "cuda":
                # Some Qwen graphs are ineligible for CUDA graph capture. The
                # selected backend is still a real CUDA handle; report capture
                # separately, without equating missing capture with CPU fallback.
                self.evidence["cuda_graph_compute_logged"] = self._cuda_computed.is_set()
            self.evidence["requests_completed"] += 1
            return text.strip()
        except BaseException as exc:
            self.unload()
            if isinstance(exc, Exception) and self._oom.is_set():
                raise GPUSelectionError("GPUのVRAM確保に失敗したためCrispASRを停止しました。空きを増やすか小さいモデルを選び、再読み込みしてください") from exc
            raise

    def unload(self) -> None:
        self._ready = False
        process, job = self._process, self._job
        close_error = None
        if job is not None:
            try:
                job.close()
            except Exception as exc:
                close_error = exc  # Still attempt to terminate/reap the worker below.
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if self._reader is not None:
                self._reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
        self._token = ""
        if close_error is not None:
            raise close_error
        self._process = self._job = self._reader = None

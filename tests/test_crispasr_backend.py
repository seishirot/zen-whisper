"""CrispASR boundaries, failure cleanup, and resident-worker contract."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import numpy as np
import pytest

from src.asr.base import RecognitionHints
from src.asr import crispasr as module
from src.asr.crispasr import CrispASRBackend, float_wav
from src.asr.crispasr_assets import decoder_for, installation_root, manifest, profile_spec, verify_hash
from src.config import AppConfig, ENGINE_CRISPASR, RecognitionConfig, load_config, save_config
from src.transcriber import Transcriber, available_recognition_engines
from tools.setup_crispasr import extract_archive


def test_config_roundtrip_and_default_off(tmp_path):
    cfg = AppConfig()
    assert cfg.recognition.engine == "whisper"
    cfg.recognition.engine = ENGINE_CRISPASR
    cfg.recognition.device = "cpu"
    cfg.recognition.crispasr_root = str(tmp_path / "音声 model")
    cfg.recognition.crispasr_model = "qwen3-1.7b-q8"
    cfg.recognition.crispasr_timeout_sec = 37
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    assert load_config(path) == cfg


def test_mac_choices_and_defaults_unchanged(monkeypatch):
    import src.transcriber as transcriber
    import src.config as config

    monkeypatch.setattr(transcriber, "is_mac", lambda: True)
    monkeypatch.setattr(config.sys, "platform", "darwin")
    assert available_recognition_engines() == ("whisper",)
    assert RecognitionConfig().device == "mlx"
    assert RecognitionConfig().qwen3_model == "Qwen/Qwen3-ASR-1.7B-hf"
    assert "Windows" in module.configuration_error(RecognitionConfig(engine=ENGINE_CRISPASR))


def test_missing_runtime_refuses_without_spawning_or_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    cfg = RecognitionConfig(engine=ENGINE_CRISPASR, device="cpu", crispasr_root=str(tmp_path))
    transcriber = Transcriber()
    with pytest.raises(ValueError, match="未配置"):
        transcriber.load_model(cfg)
    assert not transcriber.is_ready


def test_allowlist_decoder_hash_and_unicode(tmp_path):
    with pytest.raises(ValueError, match="allowlist"):
        profile_spec("arbitrary.gguf")
    parakeet = profile_spec("parakeet-ja-0.6b-q8")
    assert decoder_for(parakeet, "auto") == "tdt"
    assert decoder_for(parakeet, "ctc") == "ctc"
    with pytest.raises(ValueError, match="decoder"):
        decoder_for(profile_spec("qwen3-1.7b-q8"), "ctc")
    path = tmp_path / "日本語 model.gguf"
    path.write_bytes(b"GGUF")
    spec = {"size": 4, "sha256": hashlib.sha256(b"GGUF").hexdigest()}
    verify_hash(path, spec)
    path.write_bytes(b"gguf")
    with pytest.raises(ValueError, match="SHA-256"):
        verify_hash(path, spec)
    assert installation_root(str(tmp_path)) == tmp_path


def test_setup_rejects_zip_traversal_and_preserves_changed_files(tmp_path):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("../escape.dll", b"unsafe")
    with pytest.raises(ValueError, match="Unsafe"):
        extract_archive(archive, tmp_path / "runtime")
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("bin/a.dll", b"verified")
    extract_archive(archive, tmp_path / "runtime")
    existing = tmp_path / "runtime/bin/a.dll"
    existing.write_bytes(b"local change")
    with pytest.raises(FileExistsError, match="Changed installed"):
        extract_archive(archive, tmp_path / "runtime")
    assert existing.read_bytes() == b"local change"


def test_float_transport_is_lossless():
    import soundfile as sf

    original = np.array([0.1, -0.75, 0.000003, 0.99], dtype=np.float32)
    decoded, rate = sf.read(io.BytesIO(float_wav(original)), dtype="float32")
    assert rate == 16000
    np.testing.assert_array_equal(decoded, original)


FAKE_SERVER = r'''
import http.server, json, os, sys, time
args=sys.argv
port=int(args[args.index('--port')+1])
model=args[args.index('--model')+1]
mode=os.environ.get('ZEN_CRISP_TEST_MODE','ok')
print('crispasr 0.8.32 (git e2a35614, Release) [backends: cpu,cuda]', flush=True)
if mode=='load_oom':
 print('CUDA error: out of memory',flush=True); sys.exit(17)
if mode=='load_crash': sys.exit(17)
if mode=='load_hang': time.sleep(30)
if '--gpu-backend' in args and args[args.index('--gpu-backend')+1]=='cuda' and mode!='cpu_fallback':
 print('crispasr_init_gpu_backend: using preferred GPU backend: CUDA0',flush=True)
 print('  Device 0: Test NVIDIA, compute capability 12.0',flush=True)
if '--parakeet-decoder' in args and args[args.index('--parakeet-decoder')+1]=='ctc' and mode!='tdt_fallback':
 print('crispasr[parakeet]: using CTC decoder',flush=True)
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def send(self,code,value):
  body=json.dumps(value,ensure_ascii=False).encode('utf-8'); self.send_response(code)
  self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
 def authorized(self):
  if self.headers.get('Authorization')!='Bearer '+os.environ['CRISPASR_API_KEYS']:
   self.send(401,{'error':'unauthorized'}); return False
  return True
 def do_GET(self):
  if not self.authorized(): return
  self.send(200,{'data':[{'id':model,'backend':'parakeet'}]})
 def do_POST(self):
  if not self.authorized(): return
  body=self.rfile.read(int(self.headers['Content-Length']))
  assert b'RIFF' in body and b'WAVEfmt ' in body
  assert b'PRIVATE HINT' not in body
  if mode=='decode_oom':
   print('CUDA error: out of memory',flush=True); os._exit(19)
  if mode=='crash': os._exit(19)
  if mode=='hang': time.sleep(30)
  print('ggml_backend_cuda_graph_compute: CUDA graph warmup complete',flush=True)
  self.send(200,{'text':'' if mode=='empty' else ('日本語の認識結果。' if mode!='invalid' else 1)})
http.server.HTTPServer(('127.0.0.1',port),Handler).serve_forever()
'''


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows Job Object integration")
    script = tmp_path / "worker.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    model_dir = tmp_path / "日本語 runtime"
    model_dir.mkdir()
    model = model_dir / "model.gguf"
    model.write_bytes(b"GGUF")
    executable = tmp_path / "crispasr.exe"
    from src.gpu import NvidiaGPU
    monkeypatch.setattr(module, "admit_crispasr", lambda cfg: NvidiaGPU(1, "GPU-00000000-0000-0000-0000-000000000001", "Test NVIDIA", 8192, 8000))
    monkeypatch.setattr(module, "configuration_error", lambda cfg: "")
    monkeypatch.setattr(module, "resolve_installation", lambda *a, **k: (executable, model))
    original_popen = subprocess.Popen
    calls = []
    processes = []

    def launch(args, **kwargs):
        calls.append((args, kwargs))
        process = original_popen([sys.executable, "-X", "utf8", "-u", str(script), *args[1:]], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    cfg = RecognitionConfig(engine=ENGINE_CRISPASR, device="cpu", model_load_timeout_sec=2)
    backend = CrispASRBackend()
    yield backend, cfg, calls, processes
    backend.unload()


def test_resident_worker_authentication_hints_and_restart(fake_server, caplog, monkeypatch):
    backend, cfg, calls, processes = fake_server
    monkeypatch.setenv("CRISPASR_N_GPU_LAYERS", "0")
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.invalid")
    backend.load(cfg)
    original_pid = backend.evidence["pid"]
    connection = http.client.HTTPConnection("127.0.0.1", backend._port, timeout=1)
    try:
        connection.request("GET", "/v1/models")
        assert connection.getresponse().status == 401
    finally:
        connection.close()
    for _ in range(2):
        assert backend.transcribe(np.ones(1600, dtype=np.float32), "ja", cfg, RecognitionHints(context="PRIVATE HINT")) == "日本語の認識結果。"
    assert len(calls) == 1
    assert backend.evidence["requests_completed"] == 2
    assert backend.evidence["pid"] == original_pid
    assert "反映しません" in caplog.text
    assert "PRIVATE HINT" not in caplog.text
    args, kwargs = calls[0]
    assert "--no-gpu" in args and "--no-warmup" in args
    assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "-1"
    assert "CRISPASR_N_GPU_LAYERS" not in kwargs["env"]
    assert kwargs["env"]["CRISPASR_API_KEYS"] not in " ".join(args)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == subprocess.PIPE
    assert processes[0].stdin.closed
    assert args[args.index("--model") + 1].startswith("./")
    assert args[args.index("--model") + 1].isascii()
    backend.unload()
    assert processes[0].poll() is not None
    backend.load(cfg)
    assert len(calls) == 2 and backend.evidence["pid"] != original_pid


@pytest.mark.parametrize("mode", ["empty", "invalid", "crash", "hang"])
def test_failure_discards_output_and_reaps_worker(fake_server, monkeypatch, mode):
    backend, cfg, _, processes = fake_server
    cfg.crispasr_timeout_sec = 0.15
    monkeypatch.setenv("ZEN_CRISP_TEST_MODE", mode)
    backend.load(cfg)
    started = time.monotonic()
    with pytest.raises((RuntimeError, OSError, http.client.HTTPException)):
        backend.transcribe(np.ones(1600, dtype=np.float32), "ja", cfg)
    assert time.monotonic() - started < 3
    assert not backend.is_ready
    assert processes[0].poll() is not None


@pytest.mark.parametrize("mode", ["load_crash", "load_hang", "cpu_fallback", "tdt_fallback"])
def test_load_failure_and_fallback_are_not_success(fake_server, monkeypatch, mode):
    backend, cfg, _, processes = fake_server
    monkeypatch.setenv("ZEN_CRISP_TEST_MODE", mode)
    cfg.model_load_timeout_sec = 0.2
    if mode == "cpu_fallback":
        cfg.device = "cuda"
    if mode == "tdt_fallback":
        cfg.crispasr_decoder = "ctc"
    with pytest.raises((RuntimeError, TimeoutError)):
        backend.load(cfg)
    assert not backend.is_ready
    assert processes[0].poll() is not None


def test_silence_bad_input_and_language_do_not_invoke_native_decode(fake_server):
    backend, cfg, _, _ = fake_server
    backend.load(cfg)
    assert backend.transcribe(np.array([], dtype=np.float32), "ja", cfg) == ""
    assert backend.transcribe(np.zeros(1600, dtype=np.float32), "ja", cfg) == ""
    for audio in (np.ones((5, 2)), np.array([float("nan")]), np.array([1], dtype=np.int16)):
        with pytest.raises(ValueError):
            backend.transcribe(audio, "ja", cfg)
    with pytest.raises(ValueError, match="言語"):
        backend.transcribe(np.ones(1600), "en", cfg)
    assert backend.evidence["requests_completed"] == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object integration")
def test_process_job_reaps_child_when_owner_exits():
    import win32api
    import win32process
    import pywintypes

    script = (
        "import os,subprocess,sys; from src.asr.crispasr import _ProcessJob; "
        "job=_ProcessJob(); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, "
        "creationflags=subprocess.CREATE_NO_WINDOW); "
        "job.assign(child.pid); print(child.pid,flush=True); os._exit(0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=5, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    pid = int(result.stdout.strip())
    try:
        handle = win32api.OpenProcess(0x1000, False, pid)
    except pywintypes.error as exc:
        assert exc.winerror == 87  # already reaped / no such PID
        return
    try:
        deadline = time.monotonic() + 2
        while win32process.GetExitCodeProcess(handle) == 259 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert win32process.GetExitCodeProcess(handle) != 259
    finally:
        handle.Close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native artifact smoke")
def test_installed_cli_refuses_missing_model_with_eof_pipe(tmp_path):
    root = installation_root("")
    asset = manifest()["runtimes"]["cpu"]["asset"].removesuffix(".zip")
    executable = root / "cpu" / asset / "crispasr.exe"
    if not executable.is_file():
        pytest.skip("explicit setup has not been run; this test never downloads")
    result = subprocess.run(
        [
            str(executable), "--server", "--backend", "parakeet",
            "--model", "./parakeet-tdt-0.6b-ja-q8_0.gguf",
            "--cache-dir", "./empty-cache", "--no-gpu", "--gpu-backend", "cpu",
        ],
        cwd=tmp_path, input=b"", capture_output=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode != 0
    assert b"Use --auto-download" in result.stderr
    assert b"downloading via" not in result.stderr
    assert not list(tmp_path.rglob("*.gguf"))


@pytest.mark.parametrize("mode", ["load_oom", "decode_oom"])
def test_oom_reports_reason_and_reaps_worker(fake_server, monkeypatch, mode):
    from src.gpu import GPUSelectionError
    backend, cfg, calls, processes = fake_server
    cfg.device = "cuda"
    monkeypatch.setenv("ZEN_CRISP_TEST_MODE", mode)
    with pytest.raises(GPUSelectionError, match="VRAM"):
        backend.load(cfg)
        backend.transcribe(np.ones(1600, dtype=np.float32), "ja", cfg)
    assert len(calls) == 1
    assert calls[0][1]["env"]["CUDA_VISIBLE_DEVICES"] == "GPU-00000000-0000-0000-0000-000000000001"
    assert not backend.is_ready
    assert backend._process is None and backend._job is None
    assert processes[0].poll() is not None


def test_insufficient_vram_does_not_spawn_worker(fake_server, monkeypatch):
    from src.gpu import GPUSelectionError
    backend, cfg, calls, processes = fake_server
    cfg.device = "cuda"
    def denied(cfg):
        raise GPUSelectionError("空きVRAM不足")
    monkeypatch.setattr(module, "admit_crispasr", denied)
    with pytest.raises(GPUSelectionError, match="VRAM"):
        backend.load(cfg)
    assert not calls and not processes and not backend.is_ready


@pytest.mark.parametrize(("device", "decoder"), [("cpu", "auto"), ("cuda", "ctc")])
def test_load_waits_for_delayed_log_evidence(fake_server, monkeypatch, device, decoder):
    import threading
    backend, cfg, _, processes = fake_server
    cfg.device, cfg.crispasr_decoder = device, decoder
    release_reader = threading.Event()
    models_ready = threading.Event()
    original_reader, original_request = backend._read_logs, backend._request
    errors = []

    def delayed_reader(pipe):
        release_reader.wait(timeout=3)
        original_reader(pipe)

    def request(*args, **kwargs):
        result = original_request(*args, **kwargs)
        models_ready.set()
        return result

    def load():
        try:
            backend.load(cfg)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(backend, "_read_logs", delayed_reader)
    monkeypatch.setattr(backend, "_request", request)
    thread = threading.Thread(target=load)
    thread.start()
    try:
        assert models_ready.wait(timeout=2)
        # HTTP readiness must not turn delayed stdout into a fallback failure.
        time.sleep(0.05)
    finally:
        release_reader.set()
        thread.join(timeout=4)
    assert not thread.is_alive()
    assert errors == []
    assert backend.is_ready and processes[0].poll() is None

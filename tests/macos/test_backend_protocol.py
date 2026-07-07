from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "macos/backend/src"
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"
sys.path.insert(0, str(BACKEND_SRC))

from zen_whisper_mac_backend.adapters import (  # noqa: E402
    AdapterError,
    DummyAdapter,
    MlxQwen3AsrAdapter,
    MlxWhisperAdapter,
    make_adapter,
)
from zen_whisper_mac_backend.protocol import MAX_LINE_BYTES, ProtocolError, decode_line, encode_message  # noqa: E402
from zen_whisper_mac_backend.registry import (  # noqa: E402
    ModelRegistry,
    RegistryError,
    _freeze,
    _validate_registry,
    load_registry,
)
from zen_whisper_mac_backend.service import BackendService  # noqa: E402


def test_protocol_round_trip() -> None:
    line = encode_message({"type": "health", "request_id": "r1"})
    assert decode_line(line) == {"type": "health", "request_id": "r1"}


def test_protocol_rejects_oversized_lines() -> None:
    with pytest.raises(ProtocolError, match="too large"):
        decode_line(b"x" * (MAX_LINE_BYTES + 1))


def test_protocol_rejects_empty_request_identity() -> None:
    with pytest.raises(ProtocolError, match="request_id"):
        decode_line(b'{"type":"health","request_id":""}\n')
    with pytest.raises(ProtocolError, match="type"):
        decode_line(b'{"type":"","request_id":"r1"}\n')


def test_protocol_rejects_invalid_utf8_as_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="Invalid UTF-8"):
        decode_line(b"\xff\n")


def test_swift_backend_client_uses_per_launch_auth_token() -> None:
    client = (SWIFT_SRC / "BackendClient.swift").read_text(encoding="utf-8")
    server = (BACKEND_SRC / "zen_whisper_mac_backend/server.py").read_text(encoding="utf-8")

    assert "private let authToken =" in client
    assert '"--auth-token-stdin"' in client
    assert "let authPipe = Pipe()" in client
    assert "process.standardInput = authPipe" in client
    assert "authPipe.fileHandleForWriting.write" in client
    assert "case authTokenWriteFailed(String)" in client
    assert "throw BackendClientError.authTokenWriteFailed" in client
    assert "ZEN_WHISPER_BACKEND_AUTH_TOKEN" not in client
    assert 'request["auth_token"] = authToken' in client
    assert "authorized(BackendRequest.shutdown())" in client
    assert 'parser.add_argument("--auth-token-stdin", action="store_true")' in server
    assert "sys.stdin.readline().rstrip" in server
    assert "ZEN_WHISPER_BACKEND_AUTH_TOKEN" not in server
    assert 'if not auth_token:' in server
    assert 'return 2' in server
    assert "threading.Thread(" in server
    assert "server.listen(16)" in server
    assert "server.settimeout(0.5)" in server
    assert "MAX_HANDLER_THREADS = 8" in server
    assert "threading.BoundedSemaphore(MAX_HANDLER_THREADS)" in server
    assert "handler_slots.acquire(blocking=False)" in server
    assert "handler_slots.release()" in server
    assert "connection.settimeout(1.0)" in server
    assert "reader.readline(MAX_LINE_BYTES + 1)" in server
    assert '"--parent-pid"' in client
    assert 'parser.add_argument("--parent-pid", type=int)' in server
    assert "_start_parent_monitor(service, args.parent_pid)" in server


def test_backend_socket_server_enforces_auth_and_cleans_up_socket(tmp_path: Path) -> None:
    token = "socket-secret"
    socket_path = _short_socket_path()
    process = _start_backend_server(tmp_path, socket_path, token)
    try:
        missing = _socket_request(socket_path, {"type": "health", "request_id": "missing"})
        assert missing["type"] == "error"
        assert missing["code"] == "AUTH_FAILED"

        wrong = _socket_request(
            socket_path,
            {"type": "health", "request_id": "wrong", "auth_token": "wrong"},
        )
        assert wrong["type"] == "error"
        assert wrong["code"] == "AUTH_FAILED"

        health = _socket_request(
            socket_path,
            {"type": "health", "request_id": "ok", "auth_token": token},
        )
        assert health["type"] == "health_result"
        assert health["request_id"] == "ok"

        malformed = _raw_socket_request(socket_path, b"{not-json}\n")
        assert malformed["type"] == "error"
        assert malformed["code"] == "PROTOCOL_ERROR"

        oversized = _raw_socket_request(socket_path, b"x" * (MAX_LINE_BYTES + 1) + b"\n")
        assert oversized["type"] == "error"
        assert oversized["code"] == "PROTOCOL_ERROR"

        shutdown = _socket_request(
            socket_path,
            {"type": "shutdown", "request_id": "bye", "auth_token": token},
        )
        assert shutdown["type"] == "shutdown_ack"
        process.wait(timeout=5)
        assert not socket_path.exists()
    finally:
        _terminate_process(process)
        socket_path.unlink(missing_ok=True)


def test_backend_socket_server_exits_when_parent_pid_is_gone(tmp_path: Path) -> None:
    token = "socket-secret"
    socket_path = _short_socket_path()
    process = _start_backend_server(
        tmp_path,
        socket_path,
        token,
        extra_args=["--parent-pid", "999999999"],
        wait_for_socket=False,
    )
    try:
        process.wait(timeout=5)
        stderr = _stderr_text(process)
        if process.returncode != 0 and "PermissionError" in stderr and "Operation not permitted" in stderr:
            pytest.skip("Unix socket bind is not permitted in this sandbox")
        assert process.returncode == 0
    finally:
        _terminate_process(process)
        socket_path.unlink(missing_ok=True)


def test_backend_socket_e2e_uses_cross_platform_short_socket_path() -> None:
    source = Path(__file__).read_text(encoding="utf-8")

    assert 'return Path("/tmp")' in source


def test_service_invalid_request_fields_are_not_recoverable() -> None:
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "bad-request",
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "INVALID_REQUEST"
    assert result["recoverable"] is False

    bad_model = service.handle(
        {
            "type": "preload",
            "request_id": "bad-model",
            "engine": "mlx-whisper",
            "model": "",
            "language": "ja",
        }
    )
    assert bad_model["type"] == "error"
    assert bad_model["code"] == "INVALID_REQUEST"
    assert bad_model["recoverable"] is False

    bad_language = service.handle(
        {
            "type": "preload",
            "request_id": "bad-language",
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "",
        }
    )
    assert bad_language["type"] == "error"
    assert bad_language["code"] == "INVALID_REQUEST"
    assert bad_language["recoverable"] is False


def test_registry_data_is_immutable() -> None:
    registry = load_registry()

    with pytest.raises(TypeError):
        registry.data["default_engine"] = "dummy"  # type: ignore[index]

    with pytest.raises(TypeError):
        registry.data["languages"]["ja"]["engines"]["mlx-whisper"] = "English"  # type: ignore[index]


def test_registry_validation_rejects_unsupported_version_and_duplicates() -> None:
    base = {
        "version": 1,
        "default_engine": "mlx-whisper",
        "default_language": "ja",
        "languages": {"ja": {"label": "Japanese", "engines": {"mlx-whisper": "ja"}}},
        "engines": [
            {
                "id": "mlx-whisper",
                "label": "MLX Whisper",
                "default_model": "model-a",
                "models": [{"id": "model-a", "label": "A"}],
            }
        ],
    }
    unsupported = dict(base, version=2)
    with pytest.raises(RegistryError, match="version"):
        _validate_registry(ModelRegistry(_freeze(unsupported), "hash"))

    duplicate_engine = dict(
        base,
        engines=[
            base["engines"][0],
            base["engines"][0],
        ],
    )
    with pytest.raises(RegistryError, match="duplicate engine"):
        _validate_registry(ModelRegistry(_freeze(duplicate_engine), "hash"))

    unknown_language_engine = dict(
        base,
        languages={"ja": {"label": "Japanese", "engines": {"missing": "ja"}}},
    )
    with pytest.raises(RegistryError, match="unknown engine"):
        _validate_registry(ModelRegistry(_freeze(unknown_language_engine), "hash"))


def test_registry_validation_rejects_non_string_ids_defaults_and_labels() -> None:
    base = {
        "version": 1,
        "default_engine": "mlx-whisper",
        "default_language": "ja",
        "languages": {"ja": {"label": "Japanese", "engines": {"mlx-whisper": "ja"}}},
        "engines": [
            {
                "id": "mlx-whisper",
                "label": "MLX Whisper",
                "default_model": "model-a",
                "models": [{"id": "model-a", "label": "A"}],
            }
        ],
    }

    invalid_default = dict(base, default_engine=123)
    with pytest.raises(RegistryError, match="default_engine"):
        _validate_registry(ModelRegistry(_freeze(invalid_default), "hash"))

    invalid_engine_id = dict(
        base,
        engines=[dict(base["engines"][0], id=123)],
    )
    with pytest.raises(RegistryError, match="engine id"):
        _validate_registry(ModelRegistry(_freeze(invalid_engine_id), "hash"))

    invalid_model_id = dict(
        base,
        engines=[
            dict(
                base["engines"][0],
                models=[{"id": 123, "label": "A"}],
            )
        ],
    )
    with pytest.raises(RegistryError, match="model id"):
        _validate_registry(ModelRegistry(_freeze(invalid_model_id), "hash"))

    invalid_label = dict(
        base,
        languages={"ja": {"label": "", "engines": {"mlx-whisper": "ja"}}},
    )
    with pytest.raises(RegistryError, match="label"):
        _validate_registry(ModelRegistry(_freeze(invalid_label), "hash"))

    invalid_only_key = dict(
        base,
        languages={123: {"label": "Japanese", "engines": {"mlx-whisper": "ja"}}},
    )
    frozen = _freeze(invalid_only_key)
    assert 123 in frozen["languages"]
    with pytest.raises(RegistryError, match="default_language"):
        _validate_registry(ModelRegistry(frozen, "hash"))

    invalid_extra_key = dict(
        base,
        languages={
            "ja": {"label": "Japanese", "engines": {"mlx-whisper": "ja"}},
            123: {"label": "Numeric", "engines": {"mlx-whisper": "ja"}},
        },
    )
    with pytest.raises(RegistryError, match="language id"):
        _validate_registry(ModelRegistry(_freeze(invalid_extra_key), "hash"))

    invalid_empty_key = dict(
        base,
        languages={
            "ja": {"label": "Japanese", "engines": {"mlx-whisper": "ja"}},
            "": {"label": "Blank", "engines": {"mlx-whisper": "ja"}},
        },
    )
    with pytest.raises(RegistryError, match="language id"):
        _validate_registry(ModelRegistry(_freeze(invalid_empty_key), "hash"))

    default_language_missing_default_engine = dict(
        base,
        languages={"ja": {"label": "Japanese", "engines": {}}},
    )
    with pytest.raises(RegistryError, match="default_language.*default_engine"):
        _validate_registry(ModelRegistry(_freeze(default_language_missing_default_engine), "hash"))

    engine_without_language = dict(
        base,
        engines=[
            base["engines"][0],
            {
                "id": "mlx-qwen3-asr",
                "label": "MLX Qwen3-ASR",
                "default_model": "model-q",
                "models": [{"id": "model-q", "label": "Q"}],
            },
        ],
    )
    with pytest.raises(RegistryError, match="no language mapping"):
        _validate_registry(ModelRegistry(_freeze(engine_without_language), "hash"))


def test_registry_language_mapping() -> None:
    registry = load_registry()
    assert registry.language_for_engine("auto", "mlx-whisper") == "auto"
    assert registry.language_for_engine("ja", "mlx-whisper") == "ja"
    assert registry.language_for_engine("auto", "mlx-qwen3-asr") == "auto"
    assert registry.language_for_engine("ja", "mlx-qwen3-asr") == "Japanese"
    assert registry.language_for_engine("en", "mlx-qwen3-asr") == "English"
    assert "mlx-qwen3-asr" in registry.engine_ids()


def _start_backend_server(
    tmp_path: Path,
    socket_path: Path,
    token: str,
    *,
    extra_args: list[str] | None = None,
    wait_for_socket: bool = True,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_SRC)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-P",
            "-m",
            "zen_whisper_mac_backend.server",
            "--socket-path",
            str(socket_path),
            "--log-dir",
            str(tmp_path / "logs"),
            "--auth-token-stdin",
            *(extra_args or []),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None
    process.stdin.write((token + "\n").encode("utf-8"))
    process.stdin.close()
    if wait_for_socket:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = _stderr_text(process)
                if "PermissionError" in stderr and "Operation not permitted" in stderr:
                    pytest.skip("Unix socket bind is not permitted in this sandbox")
                raise AssertionError(
                    f"backend server exited early: {process.returncode}\n{stderr}"
                )
            if socket_path.exists():
                return process
            time.sleep(0.05)
        raise AssertionError("backend server socket was not created")
    return process


def _socket_request(socket_path: Path, payload: dict[str, object]) -> dict[str, object]:
    return _raw_socket_request(
        socket_path,
        (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"),
    )


def _raw_socket_request(socket_path: Path, payload: bytes) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(socket_path))
        client.sendall(payload)
        response = client.makefile("rb").readline()
    assert response
    decoded = json.loads(response.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _short_socket_path() -> Path:
    return Path("/tmp") / f"zwb-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"


def _stderr_text(process: subprocess.Popen[bytes]) -> str:
    if process.stderr is None:
        return ""
    try:
        return process.stderr.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def test_qwen_adapter_is_shipped_but_lazily_imported() -> None:
    adapter = make_adapter("mlx-qwen3-asr")

    assert isinstance(adapter, MlxQwen3AsrAdapter)
    assert "mlx_audio" not in sys.modules


def test_service_health_and_dummy_transcribe(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    service = BackendService(adapters={"mlx-whisper": DummyAdapter("mlx-whisper", "hello")})

    health = service.handle({"type": "health", "request_id": "h1"})
    assert health["type"] == "health_result"
    assert health["protocol_version"] == 1

    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "t1",
            "audio_path": str(audio),
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )
    assert result["type"] == "result"
    assert result["text"] == "hello"


def test_service_busy_guard_rejects_concurrent_work_then_recovers() -> None:
    started = threading.Event()
    release = threading.Event()
    first_result: dict[str, object] = {}

    class BlockingAdapter(DummyAdapter):
        def preload(self, model_id: str, language: str) -> None:
            started.set()
            assert release.wait(timeout=5)
            super().preload(model_id, language)

    service = BackendService(
        adapters={"mlx-whisper": BlockingAdapter("mlx-whisper", "hello")}
    )
    first_request = {
        "type": "preload",
        "request_id": "first",
        "engine": "mlx-whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "ja",
    }

    worker = threading.Thread(
        target=lambda: first_result.update(service.handle(first_request))
    )
    worker.start()
    assert started.wait(timeout=5)

    busy = service.handle(dict(first_request, request_id="busy"))
    assert busy["type"] == "error"
    assert busy["code"] == "BACKEND_BUSY"
    assert busy["recoverable"] is True

    release.set()
    worker.join(timeout=5)
    assert first_result["type"] == "ready"

    recovered = service.handle(dict(first_request, request_id="recovered"))
    assert recovered["type"] == "ready"


def test_service_logs_sanitized_context_for_unexpected_errors(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class ExplodingAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise RuntimeError(f"private audio path {audio_path}")

    service = BackendService(
        adapters={"mlx-whisper": ExplodingAdapter("mlx-whisper", "unused")}
    )
    with caplog.at_level(logging.ERROR, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "transcribe",
                "request_id": "explode",
                "audio_path": str(audio),
                "engine": "mlx-whisper",
                "model": "mlx-community/whisper-large-v3-turbo",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "BACKEND_ERROR"
    assert result["message"] == "Unexpected backend error"
    assert "class=RuntimeError" in caplog.text
    assert "request_id=explode" in caplog.text
    assert "engine=mlx-whisper" in caplog.text
    assert "audio_path" not in caplog.text
    assert str(audio) not in caplog.text


def test_service_requires_auth_token_when_configured() -> None:
    service = BackendService(auth_token="launch-secret")

    missing = service.handle({"type": "health", "request_id": "missing"})
    assert missing["type"] == "error"
    assert missing["code"] == "AUTH_FAILED"
    assert missing["recoverable"] is False

    wrong = service.handle(
        {"type": "health", "request_id": "wrong", "auth_token": "wrong-secret"}
    )
    assert wrong["type"] == "error"
    assert wrong["code"] == "AUTH_FAILED"

    ok = service.handle(
        {"type": "health", "request_id": "ok", "auth_token": "launch-secret"}
    )
    assert ok["type"] == "health_result"


def test_service_maps_qwen_language_before_calling_adapter(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    seen: list[str] = []

    class RecordingAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            seen.append(language)
            return super().transcribe(audio_path, model_id, language)

    service = BackendService(
        adapters={"mlx-qwen3-asr": RecordingAdapter("mlx-qwen3-asr", "qwen hello")}
    )

    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "q1",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "result"
    assert seen == ["Japanese"]


def test_backend_import_does_not_import_mlx_modules() -> None:
    for name in list(sys.modules):
        assert not name.startswith("mlx_whisper")
        assert not name.startswith("mlx_audio")


def test_mlx_whisper_adapter_passes_audio_arrays_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[np.ndarray] = []

    def fake_transcribe(audio: object, **kwargs: object) -> dict[str, str]:
        assert isinstance(audio, np.ndarray)
        assert audio.dtype == np.float32
        calls.append(audio)
        return {"text": "array path"}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxWhisperAdapter()
    text = adapter.transcribe(audio, "mlx-community/whisper-large-v3-turbo", "ja")

    assert text == "array path"
    assert len(calls) == 2


def test_mlx_whisper_auto_language_omits_language_kwarg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    kwargs_calls: list[dict[str, object]] = []

    def fake_transcribe(audio: object, **kwargs: object) -> dict[str, str]:
        kwargs_calls.append(dict(kwargs))
        return {"text": "auto path"}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxWhisperAdapter()
    text = adapter.transcribe(audio, "mlx-community/whisper-large-v3-turbo", "auto")

    assert text == "auto path"
    assert len(kwargs_calls) == 2
    assert all("language" not in kwargs for kwargs in kwargs_calls)


def test_qwen_adapter_maps_languages_and_uses_mlx_audio_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeQwenModel:
        def generate(self, audio: str, **kwargs: object) -> object:
            calls.append((audio, kwargs.get("language")))
            return types.SimpleNamespace(text="qwen path")

    def fake_load(model_id: str) -> FakeQwenModel:
        assert model_id == "mlx-community/Qwen3-ASR-0.6B-8bit"
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    assert adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese") == "qwen path"
    assert adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "auto") == "qwen path"
    assert calls == [(str(audio), "Japanese"), (str(audio), None)]


def test_qwen_preload_validates_generate_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(model_id: str) -> object:
        return object()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)

    with pytest.raises(AdapterError, match="generate"):
        MlxQwen3AsrAdapter().preload("mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")


def test_qwen_adapter_load_failure_is_recoverable_model_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(model_id: str) -> object:
        raise RuntimeError("offline")

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)

    service = BackendService()
    result = service.handle(
        {
            "type": "preload",
            "request_id": "q2",
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True


def test_audio_read_error_is_not_reported_as_model_unavailable(tmp_path: Path) -> None:
    audio = tmp_path / "private-name.wav"
    audio.write_bytes(b"not a wav")
    adapter = MlxWhisperAdapter()
    adapter._loaded_model = "mlx-community/whisper-large-v3-turbo"  # noqa: SLF001
    service = BackendService(adapters={"mlx-whisper": adapter})

    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "badwav",
            "audio_path": str(audio),
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False
    assert "private-name" not in result["message"]
    assert str(tmp_path) not in result["message"]


def test_missing_audio_path_is_nonrecoverable_without_private_path(tmp_path: Path) -> None:
    audio = tmp_path / "secret-missing.wav"
    service = BackendService()

    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "missing-audio",
            "audio_path": str(audio),
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_NOT_FOUND"
    assert result["recoverable"] is False
    assert result["message"] == "Audio file not found"
    assert "secret-missing" not in result["message"]
    assert str(tmp_path) not in result["message"]


def test_backend_os_error_is_nonrecoverable_without_private_paths(tmp_path: Path) -> None:
    audio = tmp_path / "private-os-error.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class OSErrorAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise PermissionError(str(audio_path))

    service = BackendService(
        adapters={"mlx-whisper": OSErrorAdapter("mlx-whisper", "unused")}
    )
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "io",
            "audio_path": str(audio),
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False
    assert "private-os-error" not in result["message"]
    assert str(tmp_path) not in result["message"]


def test_mlx_dependency_error_does_not_return_or_log_audio_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "dependency-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    def fake_transcribe(audio_input: object, **kwargs: object) -> dict[str, str]:
        raise RuntimeError(f"failed near {audio}")

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )
    adapter = MlxWhisperAdapter()
    adapter._loaded_model = "mlx-community/whisper-large-v3-turbo"  # noqa: SLF001
    service = BackendService(adapters={"mlx-whisper": adapter})
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "transcribe",
                "request_id": "mlx-path",
                "audio_path": str(audio),
                "engine": "mlx-whisper",
                "model": "mlx-community/whisper-large-v3-turbo",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["message"] == "MLX Whisper transcribe failed"
    assert "RuntimeError: failed near <path>" in caplog.text
    assert "dependency-secret" not in result["message"]
    assert "dependency-secret" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_qwen_dependency_error_does_not_return_or_log_audio_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(f"failed near {audio_path}")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "transcribe",
                "request_id": "qwen-path",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["message"] == "Qwen3-ASR transcribe failed"
    assert "RuntimeError: failed near <path>" in caplog.text
    assert "qwen-secret" not in result["message"]
    assert "qwen-secret" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_duration_metadata_failure_is_logged_without_audio_path(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from zen_whisper_mac_backend.service import _duration_sec

    audio = tmp_path / "duration-secret.wav"
    audio.write_bytes(b"not a wav")

    with caplog.at_level(logging.WARNING, logger="zen_whisper_mac_backend.service"):
        assert _duration_sec(audio) == 0.0

    assert "Could not read audio duration metadata" in caplog.text
    assert "duration-secret" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_unexpected_backend_error_does_not_log_exception_message(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "secret-user-audio.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class ExplodingAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise RuntimeError(f"boom {audio_path}")

    service = BackendService(adapters={"mlx-whisper": ExplodingAdapter("mlx-whisper")})
    with caplog.at_level(logging.ERROR, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "transcribe",
                "request_id": "explode",
                "audio_path": str(audio),
                "engine": "mlx-whisper",
                "model": "mlx-community/whisper-large-v3-turbo",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["message"] == "Unexpected backend error"
    assert "secret-user-audio" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_unknown_engine_still_raises_adapter_error() -> None:
    with pytest.raises(AdapterError):
        make_adapter("missing")


def test_error_response_has_no_transcript_content() -> None:
    payload = json.loads(
        encode_message(
            {
                "type": "error",
                "request_id": "e1",
                "code": "MODEL_NOT_AVAILABLE",
                "message": "model failed",
                "recoverable": True,
            }
        )
    )
    assert "text" not in payload

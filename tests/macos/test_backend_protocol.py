from __future__ import annotations

import json
import logging
import sys
import types
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
from zen_whisper_mac_backend.registry import RegistryError, load_registry  # noqa: E402
from zen_whisper_mac_backend.service import BackendService  # noqa: E402


def test_protocol_round_trip() -> None:
    line = encode_message({"type": "health", "request_id": "r1"})
    assert decode_line(line) == {"type": "health", "request_id": "r1"}


def test_protocol_rejects_oversized_lines() -> None:
    with pytest.raises(ProtocolError, match="too large"):
        decode_line(b"x" * (MAX_LINE_BYTES + 1))


def test_swift_backend_client_uses_per_launch_auth_token() -> None:
    client = (SWIFT_SRC / "BackendClient.swift").read_text(encoding="utf-8")
    server = (BACKEND_SRC / "zen_whisper_mac_backend/server.py").read_text(encoding="utf-8")

    assert "private let authToken =" in client
    assert '"--auth-token-stdin"' in client
    assert "let authPipe = Pipe()" in client
    assert "process.standardInput = authPipe" in client
    assert "authPipe.fileHandleForWriting.write" in client
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


def test_registry_language_mapping() -> None:
    registry = load_registry()
    assert registry.language_for_engine("auto", "mlx-whisper") == "auto"
    assert registry.language_for_engine("ja", "mlx-whisper") == "ja"
    assert registry.language_for_engine("auto", "mlx-qwen3-asr") == "auto"
    assert registry.language_for_engine("ja", "mlx-qwen3-asr") == "Japanese"
    assert registry.language_for_engine("en", "mlx-qwen3-asr") == "English"
    assert "mlx-qwen3-asr" in registry.engine_ids()


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


def test_corrupt_wav_error_does_not_return_audio_path(tmp_path: Path) -> None:
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
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert "private-name" not in result["message"]
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
    assert "qwen-secret" not in result["message"]
    assert "qwen-secret" not in caplog.text
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

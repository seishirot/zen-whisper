from __future__ import annotations

import errno
import io
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
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "macos/backend/src"
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"
sys.path.insert(0, str(BACKEND_SRC))

import zen_whisper_mac_backend.adapters as adapters_module  # noqa: E402
import zen_whisper_mac_backend.server as server_module  # noqa: E402
import zen_whisper_mac_backend.service as service_module  # noqa: E402
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


@pytest.fixture(autouse=True)
def _close_backend_services(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    created: list[BackendService] = []
    original = BackendService

    def make_service(*args: object, **kwargs: object) -> BackendService:
        service = original(*args, **kwargs)
        created.append(service)
        return service

    monkeypatch.setattr(sys.modules[__name__], "BackendService", make_service)
    yield
    for service in reversed(created):
        service.close(wait=False)


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

        private_path = tmp_path / "Application Support" / "protocol secret.wav"
        invalid_type = _raw_socket_request(
            socket_path,
            json.dumps(
                {
                    "type": "",
                    "request_id": f"bad near {private_path}",
                    "auth_token": token,
                }
            ).encode("utf-8") + b"\n",
        )
        assert invalid_type["type"] == "error"
        assert invalid_type["code"] == "PROTOCOL_ERROR"
        assert str(private_path) not in invalid_type["message"]
        assert "Application Support" not in invalid_type["message"]
        assert "protocol secret" not in invalid_type["message"]

        oversized = _raw_socket_request(socket_path, b"x" * (MAX_LINE_BYTES + 1) + b"\n")
        assert oversized["type"] == "error"
        assert oversized["code"] == "PROTOCOL_ERROR"

        shutdown = _socket_request(
            socket_path,
            {"type": "shutdown", "request_id": "bye", "auth_token": token},
        )
        assert shutdown["type"] == "shutdown_ack"
        process.wait(timeout=5)
        assert process.returncode == 0
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


def test_parent_monitor_closes_asr_when_parent_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeService:
        should_shutdown = False

        def shutdown(self, *, wait: bool, timeout: float | None = None) -> bool:
            self.should_shutdown = True
            events.append(("shutdown", wait, timeout))
            return True

    monkeypatch.setattr(server_module, "_parent_is_alive", lambda parent_pid: False)

    service = FakeService()
    server_module._start_parent_monitor(service, 12345)  # noqa: SLF001

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not events:
        time.sleep(0.01)

    assert events == [("shutdown", False, None)]


def test_protocol_error_response_and_log_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "Application Support" / "protocol secret.wav"
    logs: list[str] = []

    def fake_decode_line(line: bytes) -> dict[str, object]:
        raise ProtocolError(f"bad near {private_path}")

    class UnusedService:
        should_shutdown = False

        def handle(self, request: dict[str, object]) -> dict[str, object]:
            raise AssertionError("service should not handle protocol errors")

    monkeypatch.setattr(server_module, "decode_line", fake_decode_line)
    monkeypatch.setattr(
        server_module.logger,
        "info",
        lambda message, *args: logs.append(message % args),
    )
    client, server = socket.socketpair()
    try:
        client.settimeout(5)
        client.sendall(b'{"request_id":"x","type":"health"}\n')
        slots = threading.BoundedSemaphore(1)
        assert slots.acquire(blocking=False)
        server_module._handle_connection(  # noqa: SLF001
            server,
            UnusedService(),  # type: ignore[arg-type]
            slots,
        )
        response = client.makefile("rb").readline()
    finally:
        client.close()

    decoded = json.loads(response.decode("utf-8"))
    assert decoded["type"] == "error"
    assert decoded["code"] == "PROTOCOL_ERROR"
    assert decoded["message"] == "bad near <audio>"
    assert decoded["recoverable"] is False
    assert str(private_path) not in "\n".join(logs)
    assert "Application Support" not in "\n".join(logs)
    assert "protocol secret" not in "\n".join(logs)


@pytest.mark.parametrize(
    "sanitize",
    [
        adapters_module._sanitize_diagnostic_text,  # noqa: SLF001
        service_module._sanitize_log_text,  # noqa: SLF001
        server_module._sanitize_log_text,  # noqa: SLF001
    ],
)
@pytest.mark.parametrize(
    "suffix",
    [
        "/private model cache/tokenizer",
        "/cache/tokenizer",
        "/snapshots/private-hash/config.json",
        "\\private model cache\\tokenizer",
    ],
)
def test_public_model_id_path_suffixes_are_sanitized(
    sanitize: Callable[[str], str],
    suffix: str,
) -> None:
    model_id = "mlx-community/Qwen3-ASR-0.6B-8bit"

    result = sanitize(f"failed near {model_id}{suffix}")

    assert result == f"failed near {model_id}/<path>"
    assert "private model cache" not in result
    assert "cache/tokenizer" not in result
    assert "snapshots" not in result
    assert "private-hash" not in result


@pytest.mark.parametrize(
    "sanitize",
    [
        adapters_module._sanitize_diagnostic_text,  # noqa: SLF001
        service_module._sanitize_log_text,  # noqa: SLF001
        server_module._sanitize_log_text,  # noqa: SLF001
    ],
)
@pytest.mark.parametrize(
    "message",
    [
        "failed near private model cache/tokenizer",
        "failed near cache\\tokenizer",
        "loaded from snapshots/private-hash/config: denied",
        "private model cache/tokenizer",
    ],
)
def test_extensionless_relative_sensitive_paths_are_sanitized(
    sanitize: Callable[[str], str],
    message: str,
) -> None:
    result = sanitize(message)

    assert "private model cache" not in result
    assert "cache/tokenizer" not in result
    assert "cache\\tokenizer" not in result
    assert "snapshots" not in result
    assert "private-hash" not in result
    assert "tokenizer" not in result
    assert "<path>" in result


def test_decode_exception_response_hides_raw_message_and_log_sanitizes_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "Application Support" / "protocol secret.wav"
    logs: list[str] = []

    def fake_decode_line(line: bytes) -> dict[str, object]:
        raise RuntimeError(f"bad near {private_path}")

    class UnusedService:
        should_shutdown = False

        def handle(self, request: dict[str, object]) -> dict[str, object]:
            raise AssertionError("service should not handle protocol errors")

    monkeypatch.setattr(server_module, "decode_line", fake_decode_line)
    monkeypatch.setattr(
        server_module.logger,
        "error",
        lambda message, *args: logs.append(message % args),
    )
    client, server = socket.socketpair()
    try:
        client.settimeout(5)
        client.sendall(b'{"request_id":"x","type":"health"}\n')
        slots = threading.BoundedSemaphore(1)
        assert slots.acquire(blocking=False)
        server_module._handle_connection(  # noqa: SLF001
            server,
            UnusedService(),  # type: ignore[arg-type]
            slots,
        )
        response = client.makefile("rb").readline()
    finally:
        client.close()

    decoded = json.loads(response.decode("utf-8"))
    assert decoded["type"] == "error"
    assert decoded["code"] == "BACKEND_ERROR"
    assert decoded["recoverable"] is False
    assert decoded["message"] == "Unexpected backend error"
    assert "Backend error before request dispatch: class=RuntimeError" in "\n".join(logs)
    assert "message=bad near <audio>" in "\n".join(logs)
    assert str(private_path) not in "\n".join(logs)
    assert "Application Support" not in "\n".join(logs)
    assert "protocol secret" not in "\n".join(logs)


def test_service_exception_escaping_server_boundary_is_backend_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "Application Support" / "service secret.wav"
    logs: list[str] = []

    class ExplodingService:
        should_shutdown = False

        def handle(self, request: dict[str, object]) -> dict[str, object]:
            raise RuntimeError(f"bad near {private_path}")

    monkeypatch.setattr(
        server_module.logger,
        "error",
        lambda message, *args: logs.append(message % args),
    )
    client, server = socket.socketpair()
    try:
        client.settimeout(5)
        client.sendall(b'{"request_id":"service-escape","type":"health"}\n')
        slots = threading.BoundedSemaphore(1)
        assert slots.acquire(blocking=False)
        server_module._handle_connection(  # noqa: SLF001
            server,
            ExplodingService(),  # type: ignore[arg-type]
            slots,
        )
        response = client.makefile("rb").readline()
    finally:
        client.close()

    decoded = json.loads(response.decode("utf-8"))
    assert decoded["type"] == "error"
    assert decoded["request_id"] == "service-escape"
    assert decoded["code"] == "BACKEND_ERROR"
    assert decoded["message"] == "Unexpected backend error"
    assert decoded["recoverable"] is False
    assert "Backend error escaped service boundary" in "\n".join(logs)
    assert "stack=" in "\n".join(logs)
    assert "test_backend_protocol.py" in "\n".join(logs)
    assert str(private_path) not in "\n".join(logs)
    assert "Application Support" not in "\n".join(logs)
    assert "service secret" not in "\n".join(logs)


def test_backend_server_closes_service_and_unlinks_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeService:
        should_shutdown = True

        def __init__(self, *, auth_token: str) -> None:
            events.append(("init", auth_token))

        def close(self, *, wait: bool, timeout: float | None = None) -> bool:
            events.append(("close", wait, timeout))
            return True

        def shutdown(self, *, wait: bool, timeout: float | None = None) -> bool:
            self.should_shutdown = True
            events.append(("shutdown", wait, timeout))
            return True

    socket_path = _short_socket_path()
    monkeypatch.setattr(server_module, "BackendService", FakeService)
    monkeypatch.setattr(sys, "stdin", io.StringIO("server-token\n"))
    try:
        result = server_module.main(
            [
                "--socket-path",
                str(socket_path),
                "--log-dir",
                str(tmp_path / "logs"),
                "--auth-token-stdin",
            ]
        )

        assert result == 0
        assert not socket_path.exists()
        assert events == [("init", "server-token"), ("close", True, 0.2)]
    finally:
        socket_path.unlink(missing_ok=True)


def test_backend_server_closes_service_and_unlinks_socket_after_bind_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeService:
        should_shutdown = False

        def __init__(self, *, auth_token: str) -> None:
            events.append(("init", auth_token))

        def close(self, *, wait: bool, timeout: float | None = None) -> bool:
            events.append(("close", wait, timeout))
            return True

        def shutdown(self, *, wait: bool, timeout: float | None = None) -> bool:
            self.should_shutdown = True
            events.append(("shutdown", wait, timeout))
            return True

    class FailingSocket:
        def __enter__(self) -> FailingSocket:
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def bind(self, path: str) -> None:
            Path(path).touch()
            raise OSError("bind failed")

    socket_path = _short_socket_path()
    monkeypatch.setattr(server_module, "BackendService", FakeService)
    monkeypatch.setattr(server_module.socket, "socket", lambda *args, **kwargs: FailingSocket())
    monkeypatch.setattr(sys, "stdin", io.StringIO("server-token\n"))
    try:
        with pytest.raises(OSError, match="bind failed"):
            server_module.main(
                [
                    "--socket-path",
                    str(socket_path),
                    "--log-dir",
                    str(tmp_path / "logs"),
                    "--auth-token-stdin",
                ]
            )

        assert not socket_path.exists()
        assert events == [("init", "server-token"), ("close", True, 0.2)]
    finally:
        socket_path.unlink(missing_ok=True)


def test_backend_server_closes_service_when_socket_unlink_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []
    warnings: list[str] = []

    class FakeService:
        should_shutdown = True

        def __init__(self, *, auth_token: str) -> None:
            events.append(("init", auth_token))

        def close(self, *, wait: bool, timeout: float | None = None) -> bool:
            events.append(("close", wait, timeout))
            return True

        def shutdown(self, *, wait: bool, timeout: float | None = None) -> bool:
            self.should_shutdown = True
            events.append(("shutdown", wait, timeout))
            return True

    socket_path = _short_socket_path()
    original_unlink = Path.unlink
    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == socket_path and self.exists() and events == [("init", "server-token")]:
            raise OSError("unlink failed")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(server_module, "BackendService", FakeService)
    monkeypatch.setattr(
        server_module.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    monkeypatch.setattr(Path, "unlink", fake_unlink)
    monkeypatch.setattr(sys, "stdin", io.StringIO("server-token\n"))
    try:
        result = server_module.main(
            [
                "--socket-path",
                str(socket_path),
                "--log-dir",
                str(tmp_path / "logs"),
                "--auth-token-stdin",
            ]
        )

        assert result == 1
        assert events == [("init", "server-token"), ("close", True, 0.2)]
        assert any(
            warning.startswith("Could not remove backend socket: class=OSError errno=")
            for warning in warnings
        )
        assert str(socket_path) not in "\n".join(warnings)
    finally:
        monkeypatch.setattr(Path, "unlink", original_unlink)
        socket_path.unlink(missing_ok=True)


def test_backend_server_returns_warning_status_when_service_close_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeService:
        should_shutdown = True

        def __init__(self, *, auth_token: str) -> None:
            events.append(("init", auth_token))

        def close(self, *, wait: bool, timeout: float | None = None) -> bool:
            events.append(("close", wait, timeout))
            return False

        def shutdown(self, *, wait: bool, timeout: float | None = None) -> bool:
            self.should_shutdown = True
            events.append(("shutdown", wait, timeout))
            return False

    socket_path = _short_socket_path()
    monkeypatch.setattr(server_module, "BackendService", FakeService)
    monkeypatch.setattr(sys, "stdin", io.StringIO("server-token\n"))
    try:
        result = server_module.main(
            [
                "--socket-path",
                str(socket_path),
                "--log-dir",
                str(tmp_path / "logs"),
                "--auth-token-stdin",
            ]
        )

        assert result == 1
        assert not socket_path.exists()
        assert events == [("init", "server-token"), ("close", True, 0.2)]
    finally:
        socket_path.unlink(missing_ok=True)


def test_backend_server_returns_warning_status_when_service_close_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []
    warnings: list[str] = []
    private_path = tmp_path / "Application Support" / "backend close secret.txt"

    class FakeService:
        should_shutdown = True

        def __init__(self, *, auth_token: str) -> None:
            events.append(("init", auth_token))

        def close(self, *, wait: bool, timeout: float | None = None) -> bool:
            events.append(("close", wait, timeout))
            raise RuntimeError(
                f"close failed near {private_path}; qwen secret file.wav; private spaced dir/qwen secret file.wav"
            )

        def shutdown(self, *, wait: bool, timeout: float | None = None) -> bool:
            self.should_shutdown = True
            events.append(("shutdown", wait, timeout))
            return False

    socket_path = _short_socket_path()
    monkeypatch.setattr(server_module, "BackendService", FakeService)
    monkeypatch.setattr(
        server_module.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("server-token\n"))
    try:
        result = server_module.main(
            [
                "--socket-path",
                str(socket_path),
                "--log-dir",
                str(tmp_path / "logs"),
                "--auth-token-stdin",
            ]
        )

        assert result == 1
        assert not socket_path.exists()
        assert events == [("init", "server-token"), ("close", True, 0.2)]
        assert any(
            warning.startswith("Backend service close failed: class=RuntimeError errno=None message=close failed near <")
            for warning in warnings
        )
        assert str(private_path) not in "\n".join(warnings)
        assert "Application Support" not in "\n".join(warnings)
        assert "qwen secret file" not in "\n".join(warnings)
        assert "private spaced dir" not in "\n".join(warnings)
    finally:
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


def test_invalid_request_log_redacts_private_request_identity(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "Application Support" / "invalid request secret.wav"
    service = BackendService()
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "transcribe",
                "request_id": f"bad near {private_path}",
                "engine": "mlx-whisper",
                "model": "mlx-community/whisper-large-v3-turbo",
                "language": "ja",
            }
    )

    assert result["type"] == "error"
    assert result["code"] == "INVALID_REQUEST"
    assert "Invalid request bad near <audio>/transcribe" in caplog.text
    assert str(private_path) not in caplog.text
    assert "Application Support" not in caplog.text
    assert "invalid request secret" not in caplog.text


def test_invalid_request_log_sanitizes_before_truncating_long_audio_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_audio_name = f"private recording {'x' * 180}.wav"
    service = BackendService()
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "transcribe",
                "request_id": f"bad near {private_audio_name}",
                "engine": "mlx-whisper",
                "model": "mlx-community/whisper-large-v3-turbo",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "INVALID_REQUEST"
    assert "Invalid request bad near <audio>/transcribe" in caplog.text
    assert "private recording" not in caplog.text
    assert "x" * 40 not in caplog.text


def test_unknown_request_type_response_does_not_echo_private_text(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "Application Support" / "unknown request secret.wav"
    service = BackendService()
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": f"bad near {private_path}",
                "request_id": "unknown-request-private-type",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "UNKNOWN_REQUEST"
    assert result["message"] == "Unsupported request type"
    assert result["recoverable"] is False
    assert "Unknown request type: request_id=unknown-request-private-type type=bad near <audio>" in caplog.text
    assert str(private_path) not in result["message"]
    assert str(private_path) not in caplog.text
    assert "Application Support" not in result["message"]
    assert "Application Support" not in caplog.text
    assert "unknown request secret" not in result["message"]
    assert "unknown request secret" not in caplog.text


def test_unknown_request_type_log_redacts_extensionless_sensitive_relative_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = BackendService()
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "bad near private model cache/tokenizer",
                "request_id": "unknown-request-relative-private-type",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "UNKNOWN_REQUEST"
    assert result["recoverable"] is False
    assert (
        "Unknown request type: request_id=unknown-request-relative-private-type type=bad near <path>"
        in caplog.text
    )
    assert "private model cache" not in caplog.text
    assert "tokenizer" not in caplog.text


def test_service_rejects_asr_work_after_close(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    service = BackendService(adapters={"mlx-whisper": DummyAdapter("mlx-whisper", "hello")})

    service.close()
    request = {
        "type": "transcribe",
        "request_id": "after-close",
        "audio_path": str(audio),
        "engine": "mlx-whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "ja",
    }

    result = service.handle(request)
    second = service.handle(dict(request, request_id="after-close-again"))
    health = service.handle({"type": "health", "request_id": "after-close-health"})

    assert result["type"] == "error"
    assert result["code"] == "BACKEND_SHUTTING_DOWN"
    assert result["recoverable"] is False
    assert second["type"] == "error"
    assert second["code"] == "BACKEND_SHUTTING_DOWN"
    assert second["recoverable"] is False
    assert health["type"] == "error"
    assert health["code"] == "BACKEND_SHUTTING_DOWN"
    assert health["recoverable"] is False


def test_service_shutdown_request_closes_asr_to_new_work(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    service = BackendService(adapters={"mlx-whisper": DummyAdapter("mlx-whisper", "hello")})

    shutdown = service.handle({"type": "shutdown", "request_id": "shutdown"})
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "after-shutdown",
            "audio_path": str(audio),
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )

    assert shutdown["type"] == "shutdown_ack"
    assert result["type"] == "error"
    assert result["code"] == "BACKEND_SHUTTING_DOWN"
    assert result["recoverable"] is False
    health = service.handle({"type": "health", "request_id": "after-shutdown-health"})
    assert health["type"] == "error"
    assert health["code"] == "BACKEND_SHUTTING_DOWN"
    assert health["recoverable"] is False


def test_service_close_wait_can_follow_nonwaiting_close() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingAdapter(DummyAdapter):
        def preload(self, model_id: str, language: str) -> None:
            started.set()
            assert release.wait(timeout=5)

    service = BackendService(adapters={"mlx-whisper": BlockingAdapter("mlx-whisper")})
    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(
            service.handle(
                {
                    "type": "preload",
                    "request_id": "close-wait",
                    "engine": "mlx-whisper",
                    "model": "mlx-community/whisper-large-v3-turbo",
                    "language": "ja",
                }
            )
        )
    )
    worker.start()
    assert started.wait(timeout=5)

    assert service.close(wait=False) is False
    blocked = service.handle(
        {
            "type": "preload",
            "request_id": "close-wait-second",
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )
    assert blocked["code"] == "BACKEND_SHUTTING_DOWN"
    release.set()
    worker.join(timeout=5)
    assert result["type"] == "ready"
    assert service.close(wait=True, timeout=5) is True


def test_service_close_wait_timeout_reports_unfinished_worker() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingAdapter(DummyAdapter):
        def preload(self, model_id: str, language: str) -> None:
            started.set()
            assert release.wait(timeout=5)

    service = BackendService(adapters={"mlx-whisper": BlockingAdapter("mlx-whisper")})
    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(
            service.handle(
                {
                    "type": "preload",
                    "request_id": "close-timeout",
                    "engine": "mlx-whisper",
                    "model": "mlx-community/whisper-large-v3-turbo",
                    "language": "ja",
                }
            )
        )
    )
    worker.start()
    assert started.wait(timeout=5)

    assert service.close(wait=True, timeout=0.01) is False
    release.set()
    worker.join(timeout=5)
    assert result["type"] == "ready"
    assert service.close(wait=True, timeout=5) is True


def test_adapter_error_kind_controls_protocol_error_code(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class AudioAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise AdapterError("arbitrary wording", kind="audio_unreadable")

    class ModelAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise AdapterError("audio file words in model failure", kind="model_unavailable")

    audio_service = BackendService(adapters={"mlx-whisper": AudioAdapter("mlx-whisper")})
    model_service = BackendService(adapters={"mlx-whisper": ModelAdapter("mlx-whisper")})
    request = {
        "type": "transcribe",
        "request_id": "kind",
        "audio_path": str(audio),
        "engine": "mlx-whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "ja",
    }

    audio_result = audio_service.handle(request)
    model_result = model_service.handle(dict(request, request_id="kind-model"))

    assert audio_result["code"] == "AUDIO_UNREADABLE"
    assert audio_result["recoverable"] is False
    assert model_result["code"] == "MODEL_NOT_AVAILABLE"
    assert model_result["recoverable"] is True


def test_model_unavailable_message_does_not_return_private_path(tmp_path: Path) -> None:
    audio = tmp_path / "private model failure.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class LeakyModelAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise AdapterError(f"model failed near {audio}", kind="model_unavailable")

    service = BackendService(adapters={"mlx-whisper": LeakyModelAdapter("mlx-whisper")})
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "model-message-path",
            "audio_path": str(audio),
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["message"] == "model failed near <audio>"
    assert str(audio) not in result["message"]
    assert "private model failure" not in result["message"]


def test_registry_error_message_does_not_return_or_log_private_path(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    private_model = str(tmp_path / "Application Support" / "private model cache" / "model.bin")
    service = BackendService()
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "preload",
                "request_id": "private-invalid-model",
                "engine": "mlx-whisper",
                "model": private_model,
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "INVALID_MODEL"
    assert "<path>" in result["message"]
    assert private_model not in result["message"]
    assert "Application Support" not in result["message"]
    assert "private model cache" not in result["message"]
    assert private_model not in caplog.text
    assert "Application Support" not in caplog.text
    assert "private model cache" not in caplog.text


def test_adapter_error_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="Unknown adapter error kind"):
        AdapterError("bad", kind="typo")  # type: ignore[arg-type]


def test_adapter_error_kind_is_read_only() -> None:
    error = AdapterError("model")

    with pytest.raises(AttributeError):
        error.kind = "audio_unreadable"  # type: ignore[misc]


def _private_model_registry(model_id: str) -> ModelRegistry:
    return ModelRegistry(
        {
            "version": 1,
            "default_engine": "mlx-whisper",
            "default_language": "ja",
            "languages": {"ja": {"label": "Japanese", "engines": {"mlx-whisper": "ja"}}},
            "engines": [
                {
                    "id": "mlx-whisper",
                    "label": "MLX Whisper",
                    "default_model": model_id,
                    "models": [{"id": model_id, "label": "Private model"}],
                }
            ],
        },
        "hash",
    )


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


def test_service_runs_adapter_work_on_one_stable_worker_thread(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    caller_thread = threading.get_ident()
    adapter_threads: list[int] = []

    class ThreadRecordingAdapter(DummyAdapter):
        def preload(self, model_id: str, language: str) -> None:
            adapter_threads.append(threading.get_ident())
            super().preload(model_id, language)

        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            adapter_threads.append(threading.get_ident())
            return super().transcribe(audio_path, model_id, language)

    service = BackendService(
        adapters={"mlx-qwen3-asr": ThreadRecordingAdapter("mlx-qwen3-asr", "hello")}
    )

    preload = service.handle(
        {
            "type": "preload",
            "request_id": "preload-thread",
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-1.7B-8bit",
            "language": "ja",
        }
    )
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "transcribe-thread",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-1.7B-8bit",
            "language": "ja",
        }
    )
    service.close()

    assert preload["type"] == "ready"
    assert result["type"] == "result"
    assert len(set(adapter_threads)) == 1
    assert adapter_threads[0] != caller_thread


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


def test_service_close_timeout_logs_current_asr_job_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = threading.Event()
    release = threading.Event()
    first_result: dict[str, object] = {}

    class BlockingAdapter(DummyAdapter):
        def preload(self, model_id: str, language: str) -> None:
            started.set()
            assert release.wait(timeout=5)
            super().preload(model_id, language)

    service = BackendService(
        adapters={"mlx-qwen3-asr": BlockingAdapter("mlx-qwen3-asr", "hello")}
    )
    request = {
        "type": "preload",
        "request_id": "timeout-context",
        "engine": "mlx-qwen3-asr",
        "model": "mlx-community/Qwen3-ASR-1.7B-8bit",
        "language": "ja",
    }

    worker = threading.Thread(
        target=lambda: first_result.update(service.handle(request))
    )
    worker.start()
    assert started.wait(timeout=5)

    try:
        with caplog.at_level(logging.WARNING, logger="zen_whisper_mac_backend.service"):
            assert service.close(wait=True, timeout=0.01) is False
    finally:
        release.set()
        worker.join(timeout=5)
        service.close(wait=True, timeout=1)

    assert first_result["type"] == "ready"
    assert "ASR worker did not stop before shutdown timeout" in caplog.text
    assert "request_id=timeout-context" in caplog.text
    assert "type=preload" in caplog.text
    assert "engine=mlx-qwen3-asr" in caplog.text
    assert "model=mlx-community/Qwen3-ASR-1.7B-8bit" in caplog.text
    assert "audio_path" not in caplog.text


def test_service_close_timeout_redacts_private_model_path_in_job_context(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = threading.Event()
    release = threading.Event()
    first_result: dict[str, object] = {}
    model_id = str(tmp_path / "Application Support" / "qwen model cache" / "model.bin")
    registry = ModelRegistry(
        {
            "version": 1,
            "default_engine": "mlx-qwen3-asr",
            "default_language": "ja",
            "languages": {"ja": {"label": "Japanese", "engines": {"mlx-qwen3-asr": "Japanese"}}},
            "engines": [
                {
                    "id": "mlx-qwen3-asr",
                    "label": "MLX Qwen3-ASR",
                    "default_model": model_id,
                    "models": [{"id": model_id, "label": "Private model"}],
                }
            ],
        },
        "hash",
    )

    class BlockingAdapter(DummyAdapter):
        def preload(self, model_id: str, language: str) -> None:
            started.set()
            assert release.wait(timeout=5)
            super().preload(model_id, language)

    service = BackendService(
        registry=registry,
        adapters={"mlx-qwen3-asr": BlockingAdapter("mlx-qwen3-asr", "hello")},
    )
    request = {
        "type": "preload",
        "request_id": "timeout-private-model",
        "engine": "mlx-qwen3-asr",
        "model": model_id,
        "language": "ja",
    }

    worker = threading.Thread(
        target=lambda: first_result.update(service.handle(request))
    )
    worker.start()
    assert started.wait(timeout=5)

    try:
        with caplog.at_level(logging.WARNING, logger="zen_whisper_mac_backend.service"):
            assert service.close(wait=True, timeout=0.01) is False
    finally:
        release.set()
        worker.join(timeout=5)
        service.close(wait=True, timeout=1)

    assert first_result["type"] == "ready"
    assert "model=<path>" in caplog.text
    assert model_id not in caplog.text
    assert "Application Support" not in caplog.text
    assert "qwen model cache" not in caplog.text


def test_service_asr_worker_recovers_after_adapter_error(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    calls = 0

    class FlakyAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AdapterError("first model failure")
            return "second success"

    service = BackendService(adapters={"mlx-whisper": FlakyAdapter("mlx-whisper")})
    request = {
        "type": "transcribe",
        "request_id": "flaky",
        "audio_path": str(audio),
        "engine": "mlx-whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "ja",
    }

    first = service.handle(request)
    second = service.handle(dict(request, request_id="flaky-second"))
    service.close()

    assert first["type"] == "error"
    assert first["code"] == "MODEL_NOT_AVAILABLE"
    assert second["type"] == "result"
    assert second["text"] == "second success"


def test_service_asr_worker_recovers_after_unexpected_adapter_error(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    calls = 0

    class FlakyAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("first unexpected failure")
            return "second success"

    service = BackendService(adapters={"mlx-whisper": FlakyAdapter("mlx-whisper")})
    request = {
        "type": "transcribe",
        "request_id": "runtime-flaky",
        "audio_path": str(audio),
        "engine": "mlx-whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "ja",
    }

    first = service.handle(request)
    second = service.handle(dict(request, request_id="runtime-flaky-second"))
    service.close()

    assert first["type"] == "error"
    assert first["code"] == "BACKEND_ERROR"
    assert second["type"] == "result"
    assert second["text"] == "second success"


def test_service_asr_worker_rejects_after_fatal_adapter_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    calls = 0

    class FlakyAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SystemExit(f"fatal dependency near {audio}")
            return "second success"

    service = BackendService(adapters={"mlx-whisper": FlakyAdapter("mlx-whisper")})
    request = {
        "type": "transcribe",
        "request_id": "fatal-flaky",
        "audio_path": str(audio),
        "engine": "mlx-whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "ja",
    }

    with caplog.at_level(logging.ERROR, logger="zen_whisper_mac_backend.service"):
        first = service.handle(request)
    second = service.handle(dict(request, request_id="fatal-flaky-second"))
    health = service.handle({"type": "health", "request_id": "fatal-health"})
    service.close()

    assert first["type"] == "error"
    assert first["code"] == "BACKEND_ERROR"
    assert second["type"] == "error"
    assert second["code"] == "BACKEND_SHUTTING_DOWN"
    assert second["recoverable"] is False
    assert health["type"] == "error"
    assert health["code"] == "BACKEND_SHUTTING_DOWN"
    assert health["recoverable"] is False
    assert service.should_shutdown is True
    assert "Fatal ASR worker error: class=SystemExit" in caplog.text
    assert "cause=SystemExit: fatal dependency near <audio>" in caplog.text
    assert "job=request_id=fatal-flaky type=transcribe engine=mlx-whisper" in caplog.text
    assert "model=mlx-community/whisper-large-v3-turbo" in caplog.text
    assert str(audio) not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "sample.wav" not in caplog.text


def test_fatal_asr_worker_job_context_sanitizes_before_truncating(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    private_audio_name = f"private recording {'x' * 180}.wav"

    class FatalAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise SystemExit("fatal dependency")

    service = BackendService(adapters={"mlx-whisper": FatalAdapter("mlx-whisper")})
    request = {
        "type": "transcribe",
        "request_id": f"bad near {private_audio_name}",
        "audio_path": str(audio),
        "engine": "mlx-whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "ja",
    }

    with caplog.at_level(logging.ERROR, logger="zen_whisper_mac_backend.service"):
        result = service.handle(request)
    service.close()

    assert result["type"] == "error"
    assert result["code"] == "BACKEND_ERROR"
    assert "job=request_id=bad near <audio> type=transcribe" in caplog.text
    assert "private recording" not in caplog.text
    assert "x" * 40 not in caplog.text


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


def _install_fake_mlx_core(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    clear_calls: list[str] = []
    mlx = types.ModuleType("mlx")
    mlx.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("mlx.core")

    def clear_cache() -> None:
        clear_calls.append("clear")

    core.clear_cache = clear_cache  # type: ignore[attr-defined]
    mlx.core = core  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    return clear_calls


def _install_failing_mlx_core(
    monkeypatch: pytest.MonkeyPatch,
    message: str = "cache failure near qwen-secret.wav",
) -> None:
    mlx = types.ModuleType("mlx")
    mlx.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("mlx.core")

    def clear_cache() -> None:
        raise RuntimeError(message)

    core.clear_cache = clear_cache  # type: ignore[attr-defined]
    mlx.core = core  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)


def test_qwen_cache_clear_does_not_import_mlx_core_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mlx = types.ModuleType("mlx")
    mlx.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.delitem(sys.modules, "mlx.core", raising=False)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError("model failure")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    with pytest.raises(AdapterError):
        adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")

    assert "mlx.core" not in sys.modules


def test_qwen_adapter_maps_languages_and_uses_mlx_audio_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object, object]] = []

    class FakeQwenModel:
        def generate(
            self,
            audio: str,
            *,
            language: object = None,
            verbose: bool = True,
        ) -> object:
            calls.append((audio, language, verbose))
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
    assert calls == [(str(audio), "Japanese", False), (str(audio), None, False)]


def test_qwen_adapter_filters_generate_kwargs_like_mlx_audio_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class FakeQwenModel:
        def generate(self, audio: str) -> object:
            calls.append(audio)
            return types.SimpleNamespace(text="qwen path")

    def fake_load(model_id: str) -> FakeQwenModel:
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
    assert calls == [str(audio)]


def test_qwen_adapter_passes_known_kwargs_to_variadic_generate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeQwenModel:
        def generate(self, audio: str, **kwargs: object) -> object:
            calls.append(dict(kwargs))
            return types.SimpleNamespace(text=f"qwen path {Path(audio).name}")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    assert adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")
    assert calls == [{"language": "Japanese", "verbose": False}]


def test_qwen_adapter_does_not_pass_positional_only_generate_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[object, bool]] = []

    class FakeQwenModel:
        def generate(
            self,
            audio: str,
            language: object = None,
            verbose: bool = True,
            /,
        ) -> object:
            calls.append((language, verbose))
            return types.SimpleNamespace(text=f"qwen path {Path(audio).name}")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    assert adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")
    assert calls == [(None, True)]


def test_qwen_adapter_falls_back_to_language_when_signature_is_opaque(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeQwenModel:
        def generate(self, audio: str, **kwargs: object) -> object:
            calls.append(dict(kwargs))
            return types.SimpleNamespace(text="qwen path")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    def fake_signature(generate: object) -> object:
        raise ValueError("opaque callable")

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    monkeypatch.setattr(adapters_module.inspect, "signature", fake_signature)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    assert adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")
    assert calls == [{"language": "Japanese"}]


def test_qwen_adapter_releases_previous_model_before_loading_next(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_calls = _install_fake_mlx_core(monkeypatch)
    load_calls: list[str] = []

    class FakeQwenModel:
        def generate(self, audio: str, *, language: object = None) -> object:
            return types.SimpleNamespace(text="qwen path")

    def fake_load(model_id: str) -> FakeQwenModel:
        load_calls.append(model_id)
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    adapter.transcribe(audio, "model-a", "Japanese")
    adapter.transcribe(audio, "model-a", "Japanese")
    adapter.transcribe(audio, "model-b", "Japanese")
    adapter.transcribe(audio, "model-b", "Japanese")

    assert load_calls == ["model-a", "model-b"]
    assert clear_calls == ["clear"]


def test_qwen_adapter_model_switch_continues_when_cache_clear_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / "Application Support" / "qwen model cache" / "secret.wav"
    _install_failing_mlx_core(monkeypatch, f"cache failure near {cache_file}")
    load_calls: list[str] = []

    class FakeQwenModel:
        def __init__(self, model_id: str) -> None:
            self.model_id = model_id

        def generate(self, audio: str, *, language: object = None) -> object:
            return types.SimpleNamespace(text=self.model_id)

    def fake_load(model_id: str) -> FakeQwenModel:
        load_calls.append(model_id)
        return FakeQwenModel(model_id)

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    assert adapter.transcribe(audio, "model-a", "Japanese") == "model-a"
    with caplog.at_level(logging.WARNING, logger="zen_whisper_mac_backend.adapters"):
        assert adapter.transcribe(audio, "model-b", "Japanese") == "model-b"

    assert load_calls == ["model-a", "model-b"]
    assert "Could not clear MLX cache" in caplog.text
    assert str(cache_file) not in caplog.text
    assert "Application Support" not in caplog.text
    assert "qwen model cache" not in caplog.text


def test_qwen_adapter_drops_cached_model_before_failed_model_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_calls = _install_fake_mlx_core(monkeypatch)
    load_calls: list[str] = []

    class FakeQwenModel:
        def __init__(self, model_id: str) -> None:
            self.model_id = model_id

        def generate(self, audio: str, *, language: object = None) -> object:
            return types.SimpleNamespace(text=self.model_id)

    def fake_load(model_id: str) -> FakeQwenModel:
        load_calls.append(model_id)
        if model_id == "model-b":
            raise RuntimeError("offline")
        return FakeQwenModel(model_id)

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    assert adapter.transcribe(audio, "model-a", "Japanese") == "model-a"
    with pytest.raises(AdapterError, match="Qwen3-ASR unavailable"):
        adapter.transcribe(audio, "model-b", "Japanese")
    assert adapter.transcribe(audio, "model-a", "Japanese") == "model-a"

    assert load_calls == ["model-a", "model-b", "model-a"]
    assert clear_calls == ["clear"]


def test_qwen_adapter_clears_cache_on_generate_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_calls = _install_fake_mlx_core(monkeypatch)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError("failed near qwen-secret.wav")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    with pytest.raises(AdapterError) as exc_info:
        adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")

    assert str(exc_info.value) == "Qwen3-ASR transcribe failed"
    assert exc_info.value.diagnostic == "RuntimeError: failed near <audio>"
    assert clear_calls == ["clear"]


def test_qwen_adapter_preserves_original_error_when_cache_clear_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _install_failing_mlx_core(monkeypatch)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError("failed near qwen-secret.wav")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    with caplog.at_level(logging.WARNING, logger="zen_whisper_mac_backend.adapters"):
        with pytest.raises(AdapterError) as exc_info:
            adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")

    assert str(exc_info.value) == "Qwen3-ASR transcribe failed"
    assert exc_info.value.kind == "model_unavailable"
    assert "Could not clear MLX cache" in caplog.text
    assert "qwen-secret" not in caplog.text
    assert "RuntimeError: cache failure near <audio>" in caplog.text


def test_qwen_adapter_cache_clear_failure_redacts_non_audio_path_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / "Application Support" / "qwen;secret model cache" / "tokenizer.json"
    _install_failing_mlx_core(monkeypatch, f"cache failure near {cache_file}")

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError("model failure")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    with caplog.at_level(logging.WARNING, logger="zen_whisper_mac_backend.adapters"):
        with pytest.raises(AdapterError):
            adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")

    assert "Could not clear MLX cache" in caplog.text
    assert "RuntimeError: cache failure near <path>" in caplog.text
    assert str(cache_file) not in caplog.text
    assert "Application Support" not in caplog.text
    assert "qwen;secret model cache" not in caplog.text
    assert "secret model cache" not in caplog.text


def test_qwen_adapter_clears_cache_on_audio_generate_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_calls = _install_fake_mlx_core(monkeypatch)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError("failed to read sample.wav")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    adapter = MlxQwen3AsrAdapter()

    with pytest.raises(AdapterError) as exc_info:
        adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")

    assert str(exc_info.value) == "Qwen3-ASR audio file could not be read"
    assert exc_info.value.kind == "audio_unreadable"
    assert clear_calls == ["clear"]


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
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / "Application Support" / "qwen model cache" / "tokenizer.json"

    def fake_load(model_id: str) -> object:
        raise RuntimeError(f"offline cache at {cache_file}")

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)

    service = BackendService()
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
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
    assert result["message"] == "Qwen3-ASR unavailable"
    assert str(cache_file) not in result["message"]
    assert "Application Support" not in result["message"]
    assert "qwen model cache" not in result["message"]
    assert "RuntimeError: offline cache at <path>" in caplog.text
    assert str(cache_file) not in caplog.text
    assert "Application Support" not in caplog.text
    assert "qwen model cache" not in caplog.text


def test_qwen_import_failure_is_recoverable_model_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", None)
    service = BackendService()
    result = service.handle(
        {
            "type": "preload",
            "request_id": "qwen-import",
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


def test_wrong_sample_rate_audio_is_not_reported_as_model_unavailable(tmp_path: Path) -> None:
    audio = tmp_path / "wrong-rate.wav"
    sf.write(audio, np.zeros(800, dtype=np.float32), 8000)
    adapter = MlxWhisperAdapter()
    adapter._loaded_model = "mlx-community/whisper-large-v3-turbo"  # noqa: SLF001
    service = BackendService(adapters={"mlx-whisper": adapter})

    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "wrong-rate",
            "audio_path": str(audio),
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False


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


def test_backend_os_error_is_nonrecoverable_without_private_paths(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "private-os-error.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class OSErrorAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise PermissionError(f"could not read {audio_path}")

    service = BackendService(
        adapters={"mlx-whisper": OSErrorAdapter("mlx-whisper", "unused")}
    )
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
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
    assert "private-os-error" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_backend_os_error_log_redacts_private_model_path(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    model_id = str(tmp_path / "Application Support" / "private model cache" / "model.bin")

    class OSErrorAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise PermissionError("model path log boundary")

    service = BackendService(
        registry=_private_model_registry(model_id),
        adapters={"mlx-whisper": OSErrorAdapter("mlx-whisper", "unused")},
    )
    with caplog.at_level(logging.INFO, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "transcribe",
                "request_id": "io-private-model",
                "audio_path": str(audio),
                "engine": "mlx-whisper",
                "model": model_id,
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert "model=<path>" in caplog.text
    assert model_id not in caplog.text
    assert "Application Support" not in caplog.text
    assert "private model cache" not in caplog.text


def test_preload_file_not_found_is_backend_io_error_not_audio_missing(tmp_path: Path) -> None:
    class MissingModelAdapter(DummyAdapter):
        def preload(self, model_id: str, language: str) -> None:
            raise FileNotFoundError(model_id)

    service = BackendService(adapters={"mlx-whisper": MissingModelAdapter("mlx-whisper")})
    result = service.handle(
        {
            "type": "preload",
            "request_id": "preload-missing-model-file",
            "engine": "mlx-whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "BACKEND_IO_ERROR"
    assert result["recoverable"] is False
    assert result["message"] == "Backend I/O error"


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
    assert "RuntimeError: failed near <audio>" in caplog.text
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
            raise RuntimeError("failed near qwen-secret.wav")

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
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True
    assert result["message"] == "Qwen3-ASR transcribe failed"
    assert "RuntimeError: failed near <audio>" in caplog.text
    assert "qwen-secret" not in result["message"]
    assert "qwen-secret" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_qwen_dependency_error_keeps_huggingface_model_id_in_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    model_id = "mlx-community/Qwen3-ASR-0.6B-8bit"
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(f"failed near {model_id}")

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
                "request_id": "qwen-model-id-diagnostic",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": model_id,
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert f"RuntimeError: failed near {model_id}" in caplog.text
    assert "failed near <path>" not in caplog.text


def test_qwen_dependency_error_keeps_semicolon_delimited_huggingface_model_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    model_id = "mlx-community/Qwen3-ASR-0.6B-8bit"
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(f"failed; {model_id} unavailable")

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
                "request_id": "qwen-semicolon-model-id-diagnostic",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": model_id,
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert f"RuntimeError: failed; {model_id} unavailable" in caplog.text
    assert "failed; <path> unavailable" not in caplog.text


@pytest.mark.parametrize(
    "suffix",
    [
        "private model cache",
        "secret snapshot",
        "private model cache/tokenizer",
    ],
)
def test_qwen_dependency_error_redacts_private_suffix_after_huggingface_model_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    suffix: str,
) -> None:
    model_id = "mlx-community/Qwen3-ASR-0.6B-8bit"
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(f"failed; {model_id}; {suffix}")

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
                "request_id": "qwen-model-id-private-suffix",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": model_id,
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert f"RuntimeError: failed; {model_id}; <path>" in caplog.text
    assert "private model cache" not in caplog.text
    assert "secret snapshot" not in caplog.text
    assert "tokenizer" not in caplog.text


@pytest.mark.parametrize(
    "suffix",
    [
        "/private model cache/tokenizer",
        "/cache/tokenizer",
        "/snapshots/private-hash/config.json",
    ],
)
def test_qwen_dependency_error_redacts_slash_suffix_after_huggingface_model_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    suffix: str,
) -> None:
    model_id = "mlx-community/Qwen3-ASR-0.6B-8bit"
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(f"failed near {model_id}{suffix}")

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
                "request_id": "qwen-model-id-slash-suffix",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": model_id,
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert f"RuntimeError: failed near {model_id}/<path>" in caplog.text
    assert "private model cache" not in caplog.text
    assert "cache/tokenizer" not in caplog.text
    assert "snapshots" not in caplog.text
    assert "private-hash" not in caplog.text


def test_qwen_dependency_error_does_not_log_audio_path_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio_dir = tmp_path / "private spaced dir"
    audio_dir.mkdir()
    audio = audio_dir / "qwen secret file.wav"
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
                "request_id": "qwen-path-spaces",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["message"] == "Qwen3-ASR transcribe failed"
    assert "RuntimeError: failed near <audio>" in caplog.text
    assert str(audio) not in caplog.text
    assert "private spaced dir" not in caplog.text
    assert "qwen secret file" not in caplog.text


def test_qwen_dependency_error_does_not_log_relative_audio_path_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    audio_dir = Path("private spaced dir")
    audio_dir.mkdir()
    audio = audio_dir / "qwen secret file.wav"
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
                "request_id": "qwen-relative-path-spaces",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["message"] == "Qwen3-ASR transcribe failed"
    assert "<audio>" in caplog.text
    assert str(audio) not in caplog.text
    assert "private spaced dir" not in caplog.text
    assert "qwen secret file" not in caplog.text


def test_qwen_dependency_error_does_not_log_audio_filename_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen secret file.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError("failed to read qwen secret file.wav")

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
                "request_id": "qwen-filename-spaces",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
                "language": "ja",
            }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False
    assert "RuntimeError: failed to read <audio>" in caplog.text
    assert "qwen secret file" not in caplog.text


def test_qwen_dependency_error_does_not_log_bare_audio_filename_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen secret file.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError("qwen secret file.wav: permission denied")

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
                "request_id": "qwen-bare-filename-spaces",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
                "language": "ja",
            }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False
    assert "RuntimeError: <audio>: permission denied" in caplog.text
    assert "qwen secret file" not in caplog.text


def test_qwen_dependency_error_redacts_pathless_semicolon_model_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError("failed near qwen-secret.wav;private model cache/tokenizer.json")

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
                "request_id": "qwen-pathless-semicolon-model-path",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True
    assert "RuntimeError: <path>" in caplog.text
    assert "qwen-secret" not in caplog.text
    assert "private model cache" not in caplog.text
    assert "tokenizer.json" not in caplog.text


@pytest.mark.parametrize(
    "message",
    [
        "qwen-secret.wav;private model cache/tokenizer.json: permission denied",
        "qwen;secret model cache/tokenizer.json: permission denied",
        "qwen-secret.wav; private model cache/tokenizer: permission denied",
        "qwen;secret model cache/tokenizer: permission denied",
        "qwen-secret.wav;private model cache: permission denied",
        "qwen-secret.wav; private model cache: permission denied",
    ],
)
def test_qwen_dependency_error_redacts_bare_pathless_semicolon_model_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    message: str,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(message)

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
                "request_id": "qwen-bare-pathless-semicolon-model-path",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True
    assert "RuntimeError: <path>: permission denied" in caplog.text
    assert "qwen-secret" not in caplog.text
    assert "private model cache" not in caplog.text
    assert "secret model cache" not in caplog.text
    assert "tokenizer.json" not in caplog.text


def test_qwen_dependency_error_does_not_log_bare_relative_audio_path_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    audio_dir = Path("private spaced dir")
    audio_dir.mkdir()
    audio = audio_dir / "qwen secret file.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(f"{audio_path}: permission denied")

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
                "request_id": "qwen-bare-relative-spaces",
                "audio_path": str(audio),
                "engine": "mlx-qwen3-asr",
                "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
                "language": "ja",
            }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False
    assert "RuntimeError: <audio>: permission denied" in caplog.text
    assert str(audio) not in caplog.text
    assert "private spaced dir" not in caplog.text
    assert "qwen secret file" not in caplog.text


def test_qwen_audio_error_is_not_reported_as_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FilenameOnlyPermissionError(OSError):
        def __init__(self, filename: Path) -> None:
            super().__init__(errno.EPERM, "Operation not permitted")
            self.filename = str(filename)

        def __str__(self) -> str:
            return "Operation not permitted"

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise FilenameOnlyPermissionError(audio)

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-audio",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False
    assert result["message"] == "Audio file could not be read"
    assert "qwen-secret" not in result["message"]


def test_qwen_wrapped_audio_oserror_is_not_reported_as_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            try:
                raise PermissionError(errno.EACCES, "Permission denied", str(audio))
            except PermissionError as exc:
                raise RuntimeError("failed to read audio file") from exc

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-wrapped-audio",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False
    assert result["message"] == "Audio file could not be read"


def test_qwen_path_referencing_oserror_without_errno_is_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError(str(audio))

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-oserror-no-errno",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False


def test_qwen_path_referencing_generic_oserror_is_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise OSError(str(audio))

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-generic-oserror",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False


def test_qwen_model_path_with_same_audio_basename_is_not_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    model_file = tmp_path / "model cache" / "qwen-secret.wav"
    model_file.parent.mkdir()
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError(str(model_file))

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-same-basename-model-file",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True


@pytest.mark.parametrize(
    "suffix",
    ["-cache/tokenizer.json", ".bak", ";cache/tokenizer.json", "; cache/tokenizer"],
)
def test_qwen_model_path_with_audio_path_prefix_is_not_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    model_file = Path(f"{audio}{suffix}")
    model_file.parent.mkdir(parents=True, exist_ok=True)
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError(errno.EACCES, "Permission denied", str(model_file))

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-audio-prefix-model-file",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True


@pytest.mark.parametrize(
    "message",
    [
        "qwen-secret.wav-cache/tokenizer.json: permission denied",
        "qwen-secret.wav.bak: permission denied",
        "qwen-secret.wav;cache/tokenizer.json: permission denied",
        "qwen-secret.wav;cache/tokenizer: permission denied",
        "qwen-secret.wav; private model cache/tokenizer: permission denied",
        "qwen-secret.wav;private model cache: permission denied",
        "qwen-secret.wav; private model cache: permission denied",
    ],
)
def test_qwen_pathless_model_name_with_audio_prefix_is_not_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError(message)

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-pathless-audio-prefix-model-name",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True


def test_qwen_model_filename_attr_with_same_audio_basename_is_not_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    model_file = tmp_path / "model cache" / "qwen-secret.wav"
    model_file.parent.mkdir()
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError(errno.EACCES, "Permission denied", str(model_file))

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-same-basename-model-file-attr",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True


def test_qwen_relative_audio_path_does_not_match_model_path_with_same_basename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = Path("qwen-secret.wav")
    model_file = tmp_path / "model cache" / "qwen-secret.wav"
    model_file.parent.mkdir()

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError(errno.EACCES, "Permission denied", str(model_file))

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)

    adapter = MlxQwen3AsrAdapter()

    with pytest.raises(AdapterError) as exc_info:
        adapter.transcribe(audio, "mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")

    assert str(exc_info.value) == "Qwen3-ASR transcribe failed"
    assert exc_info.value.kind == "model_unavailable"


def test_qwen_relative_audio_path_with_directory_is_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    audio_dir = Path("private-dir")
    audio_dir.mkdir()
    audio = audio_dir / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(f"{audio}: permission denied")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-relative-audio-dir",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False


def test_qwen_decoder_opening_error_is_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(f"Error opening '{audio}': Format not recognised.")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-decoder-format",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False


@pytest.mark.parametrize(
    "message",
    [
        "Format not recognised.",
        "Format not recognized.",
        "Unknown format.",
    ],
)
def test_qwen_pathless_decoder_format_error_is_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError(message)

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-pathless-format",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True


def test_qwen_pathless_audio_decoder_context_is_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise RuntimeError("Audio file format not recognized.")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-pathless-audio-format",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False


def test_qwen_audio_error_can_be_classified_from_oserror_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError(13, "Permission denied", str(audio))

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-audio-filename",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False


def test_qwen_missing_audio_race_is_reported_as_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "qwen-secret.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise FileNotFoundError(2, "No such file or directory", str(audio))

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-audio-missing-race",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "AUDIO_UNREADABLE"
    assert result["recoverable"] is False


def test_qwen_model_permission_error_is_not_reported_as_audio_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)

    class FakeQwenModel:
        def generate(self, audio_path: str, **kwargs: object) -> object:
            raise PermissionError("failed to read audio model cache")

    def fake_load(model_id: str) -> FakeQwenModel:
        return FakeQwenModel()

    mlx_audio = types.ModuleType("mlx_audio")
    stt = types.ModuleType("mlx_audio.stt")
    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)
    service = BackendService()
    result = service.handle(
        {
            "type": "transcribe",
            "request_id": "qwen-model-permission",
            "audio_path": str(audio),
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "language": "ja",
        }
    )

    assert result["type"] == "error"
    assert result["code"] == "MODEL_NOT_AVAILABLE"
    assert result["recoverable"] is True
    assert result["message"] == "Qwen3-ASR transcribe failed"


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


def test_unexpected_backend_error_log_redacts_private_model_path(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    model_id = str(tmp_path / "Application Support" / "private model cache" / "model.bin")

    class ExplodingAdapter(DummyAdapter):
        def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
            raise RuntimeError("unexpected model path boundary")

    service = BackendService(
        registry=_private_model_registry(model_id),
        adapters={"mlx-whisper": ExplodingAdapter("mlx-whisper")},
    )
    with caplog.at_level(logging.ERROR, logger="zen_whisper_mac_backend.service"):
        result = service.handle(
            {
                "type": "transcribe",
                "request_id": "explode-private-model",
                "audio_path": str(audio),
                "engine": "mlx-whisper",
                "model": model_id,
                "language": "ja",
            }
        )

    assert result["type"] == "error"
    assert result["message"] == "Unexpected backend error"
    assert "model=<path>" in caplog.text
    assert model_id not in caplog.text
    assert "Application Support" not in caplog.text
    assert "private model cache" not in caplog.text


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

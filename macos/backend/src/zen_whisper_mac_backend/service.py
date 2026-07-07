"""Synchronous backend service used by the Unix socket server and tests."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from zen_whisper_mac_backend import BACKEND_VERSION, PROTOCOL_VERSION
from zen_whisper_mac_backend.adapters import AdapterError, ASRAdapter, make_adapter
from zen_whisper_mac_backend.protocol import JsonDict, error_response
from zen_whisper_mac_backend.registry import ModelRegistry, RegistryError, load_registry

logger = logging.getLogger(__name__)


class BackendService:
    def __init__(
        self,
        registry: ModelRegistry | None = None,
        adapters: dict[str, ASRAdapter] | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.registry = registry or load_registry()
        self._adapters = adapters or {}
        self._auth_token = auth_token or None
        self._busy = threading.Lock()
        self.should_shutdown = False

    def handle(self, request: JsonDict) -> JsonDict:
        request_id = str(request["request_id"])
        request_type = str(request["type"])
        try:
            if self._auth_token and request.get("auth_token") != self._auth_token:
                return error_response(
                    request_id,
                    "AUTH_FAILED",
                    "Backend authentication failed",
                    recoverable=False,
                )
            if request_type == "health":
                return self._health(request_id)
            if request_type == "shutdown":
                self.should_shutdown = True
                return {"type": "shutdown_ack", "request_id": request_id}
            if request_type == "preload":
                return self._with_busy(request_id, lambda: self._preload(request))
            if request_type == "transcribe":
                return self._with_busy(request_id, lambda: self._transcribe(request))
            return error_response(
                request_id,
                "UNKNOWN_REQUEST",
                f"Unsupported request type: {request_type}",
                recoverable=True,
            )
        except InvalidRequestError as exc:
            logger.info("Invalid request %s/%s: %s", request_id, request_type, exc)
            return error_response(
                request_id,
                "INVALID_REQUEST",
                str(exc),
                recoverable=False,
            )
        except AdapterError as exc:
            code = _error_code_for(exc)
            logger.info("Request failed: %s", _error_message_for(exc))
            return error_response(
                request_id,
                code,
                _error_message_for(exc),
                recoverable=code == "MODEL_NOT_AVAILABLE",
            )
        except RegistryError as exc:
            logger.info("Request failed: %s", _error_message_for(exc))
            return error_response(
                request_id,
                _error_code_for(exc),
                _error_message_for(exc),
                recoverable=True,
            )
        except OSError as exc:
            logger.info(
                "Backend I/O error: class=%s errno=%s request_id=%s type=%s engine=%s model=%s",
                exc.__class__.__name__,
                getattr(exc, "errno", None),
                _public_request_field(request, "request_id"),
                _public_request_field(request, "type"),
                _public_request_field(request, "engine"),
                _public_request_field(request, "model"),
            )
            return error_response(
                request_id,
                _os_error_code_for(exc, request_type),
                _os_error_message_for(exc, request_type),
                recoverable=False,
            )
        except Exception as exc:  # pragma: no cover - defensive crash boundary
            logger.error(
                "Unexpected backend error: class=%s request_id=%s type=%s engine=%s model=%s stack=%s",
                exc.__class__.__name__,
                _public_request_field(request, "request_id"),
                _public_request_field(request, "type"),
                _public_request_field(request, "engine"),
                _public_request_field(request, "model"),
                _sanitized_stack(exc),
            )
            return error_response(
                request_id,
                "BACKEND_ERROR",
                "Unexpected backend error",
                recoverable=False,
            )

    def _with_busy(self, request_id: str, fn: Any) -> JsonDict:
        if not self._busy.acquire(blocking=False):
            return error_response(
                request_id,
                "BACKEND_BUSY",
                "Backend is already processing a request",
                recoverable=True,
            )
        try:
            return fn()
        finally:
            self._busy.release()

    def _health(self, request_id: str) -> JsonDict:
        return {
            "type": "health_result",
            "request_id": request_id,
            "protocol_version": PROTOCOL_VERSION,
            "backend_version": BACKEND_VERSION,
            "registry_hash": self.registry.sha256,
        }

    def _preload(self, request: JsonDict) -> JsonDict:
        request_id = str(request["request_id"])
        engine_id = _required_str(request, "engine")
        model_id = self.registry.validate_engine_model(
            engine_id, _optional_str_field(request, "model")
        )
        language_id = _optional_str_field(request, "language")
        language = self.registry.language_for_engine(
            language_id if language_id is not None else self.registry.default_language,
            engine_id,
        )
        adapter = self._adapter(engine_id)
        started = time.monotonic()
        adapter.preload(model_id, language)
        return {
            "type": "ready",
            "request_id": request_id,
            "engine": engine_id,
            "model": model_id,
            "registry_hash": self.registry.sha256,
            "elapsed_sec": round(time.monotonic() - started, 3),
        }

    def _transcribe(self, request: JsonDict) -> JsonDict:
        request_id = str(request["request_id"])
        engine_id = _required_str(request, "engine")
        model_id = self.registry.validate_engine_model(
            engine_id, _optional_str_field(request, "model")
        )
        language_id = _optional_str_field(request, "language")
        language = self.registry.language_for_engine(
            language_id if language_id is not None else self.registry.default_language,
            engine_id,
        )
        audio_path = Path(_required_str(request, "audio_path"))
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)
        started = time.monotonic()
        text = self._adapter(engine_id).transcribe(audio_path, model_id, language)
        elapsed = time.monotonic() - started
        return {
            "type": "result",
            "request_id": request_id,
            "text": text.strip(),
            "duration_sec": _duration_sec(audio_path),
            "elapsed_sec": round(elapsed, 3),
            "engine": engine_id,
            "model": model_id,
        }

    def _adapter(self, engine_id: str) -> ASRAdapter:
        adapter = self._adapters.get(engine_id)
        if adapter is None:
            adapter = make_adapter(engine_id)
            self._adapters[engine_id] = adapter
        return adapter


class InvalidRequestError(ValueError):
    """Raised for well-formed JSON requests with invalid per-type fields."""


def _required_str(request: JsonDict, key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidRequestError(f"{key} must be a non-empty string")
    return value


def _optional_str_field(request: JsonDict, key: str) -> str | None:
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidRequestError(f"{key} must be a non-empty string")
    return value


def _public_request_field(request: JsonDict, key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        return "<missing>"
    return value[:160]


def _sanitized_stack(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "<none>"
    return " > ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames[-8:]
    )


def _error_code_for(exc: Exception) -> str:
    if isinstance(exc, RegistryError):
        return "INVALID_MODEL"
    if isinstance(exc, AdapterError):
        message = str(exc).lower()
        if "audio" in message or "wav" in message:
            return "AUDIO_UNREADABLE"
        return "MODEL_NOT_AVAILABLE"
    return "BACKEND_ERROR"


def _error_message_for(exc: Exception) -> str:
    if isinstance(exc, AdapterError):
        if _error_code_for(exc) == "AUDIO_UNREADABLE":
            return "Audio file could not be read"
        return str(exc) or "Model is not available"
    if isinstance(exc, RegistryError):
        return str(exc) or "Invalid model selection"
    return exc.__class__.__name__


def _os_error_code_for(exc: OSError, request_type: str) -> str:
    if isinstance(exc, FileNotFoundError):
        return "AUDIO_NOT_FOUND"
    if request_type == "transcribe":
        return "AUDIO_UNREADABLE"
    return "BACKEND_IO_ERROR"


def _os_error_message_for(exc: OSError, request_type: str) -> str:
    if isinstance(exc, FileNotFoundError):
        return "Audio file not found"
    if request_type == "transcribe":
        return "Audio file could not be read"
    return "Backend I/O error"


def _duration_sec(audio_path: Path) -> float:
    try:
        import soundfile as sf

        info = sf.info(audio_path)
        return round(float(info.frames) / float(info.samplerate), 3)
    except Exception:
        return 0.0

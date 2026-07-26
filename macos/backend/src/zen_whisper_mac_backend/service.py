"""Synchronous backend service used by the Unix socket server and tests."""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import re
import subprocess
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zen_whisper_mac_backend import BACKEND_VERSION, PROTOCOL_VERSION
from zen_whisper_mac_backend.adapters import AdapterError, ASRAdapter, make_adapter
from zen_whisper_mac_backend.enhancements import (
    EnhancementValidationError,
    parse_postprocessor,
    parse_profile,
    process_transcript,
    recognition_hints,
    terminate_process_group,
    validate_transcribe_request,
)
from zen_whisper_mac_backend.protocol import JsonDict, error_response
from zen_whisper_mac_backend.registry import ModelRegistry, RegistryError, load_registry

logger = logging.getLogger(__name__)
_MODEL_ID_TOKEN_PREFIX = "__ZW_PUBLIC_MODEL_ID_"
_PUBLIC_MODEL_ID_RE = re.compile(
    r"\b(?:mlx-community|Qwen|openai|Blaizzy)/[A-Za-z0-9][A-Za-z0-9._-]*\b"
)
_PUBLIC_MODEL_ID_PRIVATE_SUFFIX_RE = re.compile(
    rf"({_MODEL_ID_TOKEN_PREFIX}\d+__);\s*"
    r"(?:private|secret|model|cache|snapshot|tokenizer|config)[^:\n'\"<>]*",
    re.IGNORECASE,
)
_PUBLIC_MODEL_ID_PATH_SUFFIX_RE = re.compile(
    rf"({_MODEL_ID_TOKEN_PREFIX}\d+__)(?:[/\\][^:\n'\"<>]*)",
    re.IGNORECASE,
)
_SEMICOLON_PATH_SUFFIX = (
    r";\s*[^:\n'\"<>]*(?:/|\\|private|secret|model|cache|snapshot|tokenizer|config)"
)
_AUDIO_BOUNDARY = rf"(?![\w./\\-]|{_SEMICOLON_PATH_SUFFIX})"
_AUDIO_PATH_RE = re.compile(
    r"(?:(?<![\w.-])/[^\n'\"<>]+?|[A-Za-z]:\\[^\n'\"<>]+?)"
    rf"\.(?:wav|m4a|mp3|flac|ogg|aiff|aif){_AUDIO_BOUNDARY}",
    re.IGNORECASE,
)
_RELATIVE_AUDIO_PATH_RE = re.compile(
    r"\b[^\n'\"<>]*(?:/|\\)[^\n'\"<>]*?"
    rf"\.(?:wav|m4a|mp3|flac|ogg|aiff|aif){_AUDIO_BOUNDARY}",
    re.IGNORECASE,
)
_RELATIVE_AUDIO_CONTEXT_RE = re.compile(
    r"(\b(?:near|path|file)\s+)[^\n'\"<>]*(?:/|\\)[^\n'\"<>]*?"
    rf"\.(?:wav|m4a|mp3|flac|ogg|aiff|aif){_AUDIO_BOUNDARY}",
    re.IGNORECASE,
)
_RELATIVE_PATH_CONTEXT_RE = re.compile(
    r"(\b(?:near|path|file)\s+)[^:\n'\"<>;]+;\s*[^:\n'\"<>]*(?:/|\\)[^:\n'\"<>]*",
    re.IGNORECASE,
)
_SEMICOLON_AUDIO_PREFIX_PATH_RE = re.compile(
    rf"(?<![\w/\\.-])[^:\n'\"<>;]*\.(?:wav|m4a|mp3|flac|ogg|aiff|aif)"
    rf"{_SEMICOLON_PATH_SUFFIX}[^:\n'\"<>]*",
    re.IGNORECASE,
)
_RELATIVE_FILE_PATH_RE = re.compile(
    r"(?<![\w/\\.-])[^:\n'\"<>]*(?:/|\\)[^:\n'\"<>]*"
    r"\.[A-Za-z][A-Za-z0-9]{0,7}(?![\w./\\-]|;\S)",
    re.IGNORECASE,
)
_SEMICOLON_RELATIVE_PATH_RE = re.compile(
    r"(?<![\w/\\.-])[^:\n'\"<>;]+;\s*[^:\n'\"<>]*(?:/|\\)[^:\n'\"<>]*",
    re.IGNORECASE,
)
_SEMICOLON_SENSITIVE_PATH_RE = re.compile(
    r"(?<![\w/\\.-])[^:\n'\"<>;]+;\s*"
    r"[^:\n'\"<>]*(?:private|secret|model|cache|snapshot|tokenizer|config)"
    r"[^:\n'\"<>]*(?:/|\\)[^:\n'\"<>]*(?=$|[:;\n'\"<>])",
    re.IGNORECASE,
)
_RELATIVE_SENSITIVE_PATH_CONTEXT_RE = re.compile(
    r"(\b(?:near|path|file|from|at|in)\s+)(?!__ZW_PUBLIC_MODEL_ID_)"
    r"[^:\n'\"<>;]*(?:private|secret|model|cache|snapshot|tokenizer|config)"
    r"[^:\n'\"<>;]*(?:/|\\)[^:\n'\"<>;]*",
    re.IGNORECASE,
)
_RELATIVE_SENSITIVE_PATH_RE = re.compile(
    r"(?<![\w/\\.-])(?!__ZW_PUBLIC_MODEL_ID_)"
    r"[^:\n'\"<>;]*(?:private|secret|model|cache|snapshot|tokenizer|config)"
    r"[^:\n'\"<>;]*(?:/|\\)[^:\n'\"<>;]*(?=$|[:;\n'\"<>])",
    re.IGNORECASE,
)
_AUDIO_NAME_CONTEXT_RE = re.compile(
    r"(\b(?:failed to read|could not read|near|path|file|read|open|audio(?: file)?)\s+)"
    rf"[^\n'\"<>/\\]*?\.(?:wav|m4a|mp3|flac|ogg|aiff|aif){_AUDIO_BOUNDARY}",
    re.IGNORECASE,
)
_AUDIO_NAME_WITH_SPACES_RE = re.compile(
    rf"(?<![\w/\\.-])[^/\n'\"<>:]*?\s+[^/\n'\"<>:]*?\.(?:wav|m4a|mp3|flac|ogg|aiff|aif){_AUDIO_BOUNDARY}",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?:(?<![\w.-])/[^\n'\"<>]+|[A-Za-z]:\\[^\n'\"<>]+)")
_AUDIO_NAME_RE = re.compile(
    rf"\b[^\s'\"<>/\\]+\.(?:wav|m4a|mp3|flac|ogg|aiff|aif){_AUDIO_BOUNDARY}",
    re.IGNORECASE,
)


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
        self._lifecycle = threading.Lock()
        self._asr_worker: _AsrWorker | None = None
        self._active_enhancement_process: subprocess.Popen[Any] | None = None
        self._closed = False
        self._should_shutdown = False

    @property
    def should_shutdown(self) -> bool:
        with self._lifecycle:
            return self._closed or self._should_shutdown

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
                if self.should_shutdown:
                    return _shutdown_response(request_id)
                return self._health(request_id)
            if request_type == "shutdown":
                self.shutdown(wait=False)
                return {"type": "shutdown_ack", "request_id": request_id}
            if request_type == "preload":
                return self._with_busy(request_id, request, lambda: self._preload(request))
            if request_type == "transcribe":
                return self._with_busy(request_id, request, lambda: self._transcribe(request))
            logger.info(
                "Unknown request type: request_id=%s type=%s",
                _public_log_request_field(request, "request_id"),
                _public_log_request_field(request, "type"),
            )
            return error_response(
                request_id,
                "UNKNOWN_REQUEST",
                "Unsupported request type",
                recoverable=False,
            )
        except (InvalidRequestError, EnhancementValidationError) as exc:
            logger.info(
                "Invalid request %s/%s: %s",
                _public_log_request_field(request, "request_id"),
                _public_log_request_field(request, "type"),
                _sanitize_log_text(str(exc)),
            )
            return error_response(
                request_id,
                "INVALID_REQUEST",
                str(exc),
                recoverable=False,
            )
        except AdapterError as exc:
            code = _error_code_for(exc)
            logger.info(
                "Request failed: code=%s message=%s cause=%s stack=%s",
                code,
                _error_message_for(exc),
                _sanitized_exception_chain(exc),
                _sanitized_stack(exc),
            )
            return error_response(
                request_id,
                code,
                _error_message_for(exc),
                recoverable=code == "MODEL_NOT_AVAILABLE",
            )
        except RegistryError as exc:
            logger.info("Request failed: %s", _sanitized_exception_chain(exc))
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
                _public_log_request_field(request, "request_id"),
                _public_log_request_field(request, "type"),
                _public_log_request_field(request, "engine"),
                _public_log_request_field(request, "model"),
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
                _public_log_request_field(request, "request_id"),
                _public_log_request_field(request, "type"),
                _public_log_request_field(request, "engine"),
                _public_log_request_field(request, "model"),
                _sanitized_stack(exc),
            )
            return error_response(
                request_id,
                "BACKEND_ERROR",
                "Unexpected backend error",
                recoverable=False,
            )

    def _with_busy(self, request_id: str, request: JsonDict, fn: Callable[[], JsonDict]) -> JsonDict:
        if self._is_closed():
            return _shutdown_response(request_id)
        if not self._busy.acquire(blocking=False):
            if self._is_closed():
                return _shutdown_response(request_id)
            return error_response(
                request_id,
                "BACKEND_BUSY",
                "Backend is already processing a request",
                recoverable=True,
            )
        try:
            try:
                future = self._submit_asr(request, fn)
            except BackendClosedError:
                return _shutdown_response(request_id)
            try:
                return future.result()
            except BackendWorkerFatalError:
                self.shutdown(wait=False)
                raise
        finally:
            self._busy.release()

    def close(self, *, wait: bool = True, timeout: float | None = None) -> bool:
        with self._lifecycle:
            already_closed = self._closed
            self._closed = True
            worker = self._asr_worker
            process = self._active_enhancement_process
            self._active_enhancement_process = None
        if process is not None:
            terminate_process_group(process)
        if already_closed and worker is None:
            return True
        if worker is not None:
            return worker.close(wait=wait, timeout=timeout)
        return True

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> bool:
        with self._lifecycle:
            self._should_shutdown = True
        return self.close(wait=wait, timeout=timeout)

    def _submit_asr(
        self,
        request: JsonDict,
        fn: Callable[[], JsonDict],
    ) -> concurrent.futures.Future[JsonDict]:
        with self._lifecycle:
            if self._closed:
                raise BackendClosedError()
            if self._asr_worker is None:
                self._asr_worker = _AsrWorker()
            try:
                return self._asr_worker.submit(_job_context(request), fn)
            except BackendClosedError:
                self._closed = True
                raise

    def _is_closed(self) -> bool:
        with self._lifecycle:
            return self._closed

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
        validate_transcribe_request(request)
        request_id = str(request["request_id"])
        engine_id = _required_str(request, "engine")
        model_id = self.registry.validate_engine_model(
            engine_id, _optional_str_field(request, "model")
        )
        language_id = _optional_str_field(request, "language")
        selected_language = (
            language_id if language_id is not None else self.registry.default_language
        )
        language = self.registry.language_for_engine(
            selected_language,
            engine_id,
        )
        profile = parse_profile(request.get("profile"))
        postprocessor = parse_postprocessor(request.get("postprocessor"))
        audio_path = Path(_required_str(request, "audio_path"))
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)
        started = time.monotonic()
        adapter = self._adapter(engine_id)
        hints = recognition_hints(profile)
        transcribe_with_hints = getattr(adapter, "transcribe_with_hints", None)
        hints_applied = bool(hints.initial_prompt) and callable(transcribe_with_hints)
        if hints_applied:
            text = transcribe_with_hints(
                audio_path,
                model_id,
                language,
                hints,
            )
        else:
            text = adapter.transcribe(audio_path, model_id, language)
        elapsed = time.monotonic() - started
        enhanced = process_transcript(
            text.strip(),
            profile,
            postprocessor,
            selected_language,
            on_process_started=self._register_enhancement_process,
            on_process_finished=self._finish_enhancement_process,
        )
        return {
            "type": "result",
            "request_id": request_id,
            "text": enhanced.text,
            "duration_sec": _duration_sec(audio_path),
            "elapsed_sec": round(elapsed, 3),
            "engine": engine_id,
            "model": model_id,
            "hints_applied": hints_applied,
            "enhancement": enhanced.response_metadata(),
        }

    def _register_enhancement_process(
        self,
        process: subprocess.Popen[Any],
    ) -> bool:
        with self._lifecycle:
            if (
                self._closed
                or self._should_shutdown
                or self._active_enhancement_process is not None
            ):
                return False
            self._active_enhancement_process = process
            return True

    def _finish_enhancement_process(
        self,
        process: subprocess.Popen[Any],
    ) -> None:
        with self._lifecycle:
            if self._active_enhancement_process is process:
                self._active_enhancement_process = None

    def _adapter(self, engine_id: str) -> ASRAdapter:
        adapter = self._adapters.get(engine_id)
        if adapter is None:
            adapter = make_adapter(engine_id)
            self._adapters[engine_id] = adapter
        return adapter


class InvalidRequestError(ValueError):
    """Raised for well-formed JSON requests with invalid per-type fields."""


class BackendClosedError(RuntimeError):
    """Raised when ASR work is submitted after the backend has been closed."""


class BackendWorkerFatalError(RuntimeError):
    """Raised when ASR worker catches a non-Exception fatal error."""

    def __init__(self, message: str, *, stack: str) -> None:
        super().__init__(message)
        self.stack = stack


@dataclass(frozen=True)
class _AsrJob:
    future: concurrent.futures.Future[JsonDict]
    context: str
    fn: Callable[[], JsonDict]


class _AsrWorker:
    def __init__(self) -> None:
        self._queue: queue.Queue[_AsrJob | None] = queue.Queue()
        self._closed = False
        self._lock = threading.Lock()
        self._current_context: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="zen-whisper-asr",
            daemon=True,
        )
        self._thread.start()

    def submit(self, context: str, fn: Callable[[], JsonDict]) -> concurrent.futures.Future[JsonDict]:
        with self._lock:
            if self._closed:
                raise BackendClosedError()
            future: concurrent.futures.Future[JsonDict] = concurrent.futures.Future()
            self._queue.put(_AsrJob(future=future, context=context, fn=fn))
            return future

    def close(self, *, wait: bool, timeout: float | None = None) -> bool:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        if wait:
            self._thread.join(timeout=timeout)
            stopped = not self._thread.is_alive()
            if not stopped:
                current_context = self._current_job_context()
                if current_context:
                    logger.warning(
                        "ASR worker did not stop before shutdown timeout: %s",
                        current_context,
                    )
                else:
                    logger.warning("ASR worker did not stop before shutdown timeout")
            return stopped
        return not self._thread.is_alive()

    def _current_job_context(self) -> str | None:
        with self._lock:
            return self._current_context

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if not item.future.set_running_or_notify_cancel():
                continue
            with self._lock:
                self._current_context = item.context
            try:
                item.future.set_result(item.fn())
            except Exception as exc:
                item.future.set_exception(exc)
            except BaseException as exc:
                stack = _sanitized_stack(exc)
                logger.error(
                    "Fatal ASR worker error: class=%s job=%s cause=%s stack=%s",
                    exc.__class__.__name__,
                    item.context,
                    _sanitized_exception_chain(exc),
                    stack,
                )
                fatal = BackendWorkerFatalError("Fatal ASR worker error", stack=stack)
                fatal.__cause__ = exc
                item.future.set_exception(fatal)
                with self._lock:
                    self._closed = True
                    self._current_context = None
                return
            finally:
                with self._lock:
                    if self._current_context == item.context:
                        self._current_context = None


def _shutdown_response(request_id: str) -> JsonDict:
    return error_response(
        request_id,
        "BACKEND_SHUTTING_DOWN",
        "Backend is shutting down",
        recoverable=False,
    )


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


def _public_log_request_field(request: JsonDict, key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        return "<missing>"
    return _sanitize_log_text(value)


def _job_context(request: JsonDict) -> str:
    fields = {
        "request_id": _public_log_request_field(request, "request_id"),
        "type": _public_log_request_field(request, "type"),
        "engine": _public_log_request_field(request, "engine"),
        "model": _public_log_request_field(request, "model"),
    }
    return " ".join(f"{key}={value}" for key, value in fields.items())


def _sanitized_stack(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "<none>"
    return " > ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames[-8:]
    )


def _sanitized_exception_chain(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 5:
        seen.add(id(current))
        message = _sanitize_log_text(str(current))
        if message:
            parts.append(f"{current.__class__.__name__}: {message}")
        else:
            parts.append(current.__class__.__name__)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return " <- ".join(parts) if parts else "<none>"


def _sanitize_log_text(value: str) -> str:
    value, public_model_ids = _protect_public_model_ids(value)
    value = _PUBLIC_MODEL_ID_PRIVATE_SUFFIX_RE.sub(r"\1; <path>", value)
    value = _PUBLIC_MODEL_ID_PATH_SUFFIX_RE.sub(r"\1/<path>", value)
    value = _SEMICOLON_AUDIO_PREFIX_PATH_RE.sub("<path>", value)
    value = _RELATIVE_AUDIO_CONTEXT_RE.sub(r"\1<audio>", value)
    value = _RELATIVE_AUDIO_PATH_RE.sub("<audio>", value)
    value = _AUDIO_PATH_RE.sub("<audio>", value)
    value = _RELATIVE_PATH_CONTEXT_RE.sub(r"\1<path>", value)
    value = _SEMICOLON_SENSITIVE_PATH_RE.sub("<path>", value)
    value = _RELATIVE_SENSITIVE_PATH_CONTEXT_RE.sub(_redact_relative_sensitive_path_context, value)
    value = _RELATIVE_SENSITIVE_PATH_RE.sub(_redact_relative_sensitive_path, value)
    value = _PATH_RE.sub("<path>", value)
    value = _RELATIVE_FILE_PATH_RE.sub("<path>", value)
    value = _SEMICOLON_RELATIVE_PATH_RE.sub("<path>", value)
    value = _AUDIO_NAME_CONTEXT_RE.sub(r"\1<audio>", value)
    value = _AUDIO_NAME_WITH_SPACES_RE.sub("<audio>", value)
    value = _AUDIO_NAME_RE.sub("<audio>", value)
    value = _restore_public_model_ids(value, public_model_ids)
    return value[:240]


def _redact_relative_sensitive_path(match: re.Match[str]) -> str:
    text = match.group(0)
    if _MODEL_ID_TOKEN_PREFIX in text:
        return text
    return "<path>"


def _redact_relative_sensitive_path_context(match: re.Match[str]) -> str:
    text = match.group(0)
    if _MODEL_ID_TOKEN_PREFIX in text:
        return text
    return f"{match.group(1)}<path>"


def _protect_public_model_ids(value: str) -> tuple[str, list[str]]:
    public_model_ids: list[str] = []

    def replace(match: re.Match[str]) -> str:
        public_model_ids.append(match.group(0))
        return f"{_MODEL_ID_TOKEN_PREFIX}{len(public_model_ids) - 1}__"

    return _PUBLIC_MODEL_ID_RE.sub(replace, value), public_model_ids


def _restore_public_model_ids(value: str, public_model_ids: list[str]) -> str:
    for index, model_id in enumerate(public_model_ids):
        value = value.replace(f"{_MODEL_ID_TOKEN_PREFIX}{index}__", model_id)
    return value


def _error_code_for(exc: Exception) -> str:
    if isinstance(exc, RegistryError):
        return "INVALID_MODEL"
    if isinstance(exc, AdapterError):
        if exc.kind == "audio_unreadable":
            return "AUDIO_UNREADABLE"
        return "MODEL_NOT_AVAILABLE"
    return "BACKEND_ERROR"


def _error_message_for(exc: Exception) -> str:
    if isinstance(exc, AdapterError):
        if _error_code_for(exc) == "AUDIO_UNREADABLE":
            return "Audio file could not be read"
        return _sanitize_log_text(str(exc) or "Model is not available") or "Model is not available"
    if isinstance(exc, RegistryError):
        message = _sanitize_log_text(str(exc) or "Invalid model selection")
        return message or "Invalid model selection"
    return exc.__class__.__name__


def _os_error_code_for(exc: OSError, request_type: str) -> str:
    if request_type == "transcribe" and isinstance(exc, FileNotFoundError):
        return "AUDIO_NOT_FOUND"
    if request_type == "transcribe":
        return "AUDIO_UNREADABLE"
    return "BACKEND_IO_ERROR"


def _os_error_message_for(exc: OSError, request_type: str) -> str:
    if request_type == "transcribe" and isinstance(exc, FileNotFoundError):
        return "Audio file not found"
    if request_type == "transcribe":
        return "Audio file could not be read"
    return "Backend I/O error"


def _duration_sec(audio_path: Path) -> float:
    try:
        import soundfile as sf

        info = sf.info(audio_path)
        return round(float(info.frames) / float(info.samplerate), 3)
    except Exception as exc:
        logger.warning(
            "Could not read audio duration metadata: class=%s audio=<redacted>",
            exc.__class__.__name__,
        )
        return 0.0

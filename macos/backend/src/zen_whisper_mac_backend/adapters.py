"""ASR adapters for the macOS backend.

The MLX dependencies are intentionally imported lazily so protocol and boundary
tests can run on machines without MLX or downloaded models.
"""

from __future__ import annotations

import gc
import inspect
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

from zen_whisper_mac_backend.enhancements import RecognitionHints

AdapterErrorKind = Literal["model_unavailable", "audio_unreadable"]
_ADAPTER_ERROR_KINDS = {"model_unavailable", "audio_unreadable"}
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
logger = logging.getLogger(__name__)


class AdapterError(RuntimeError):
    """Raised for engine/model load or transcription failures."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic: str | None = None,
        kind: AdapterErrorKind = "model_unavailable",
    ) -> None:
        if kind not in _ADAPTER_ERROR_KINDS:
            raise ValueError(f"Unknown adapter error kind: {kind}")
        super().__init__(message)
        self.diagnostic = diagnostic
        self._kind = kind

    @property
    def kind(self) -> AdapterErrorKind:
        return self._kind


class ASRAdapter(Protocol):
    engine_id: str

    def preload(self, model_id: str, language: str) -> None:
        """Load or warm up model resources."""

    def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
        """Transcribe a WAV file and return plain text."""


@dataclass
class DummyAdapter:
    engine_id: str = "dummy"
    text: str = "dummy transcript"

    def preload(self, model_id: str, language: str) -> None:
        return None

    def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
        if not audio_path.exists():
            raise AdapterError("Audio file does not exist", kind="audio_unreadable")
        return self.text.strip()


class MlxWhisperAdapter:
    engine_id = "mlx-whisper"

    def __init__(self) -> None:
        self._loaded_model: str | None = None

    def preload(self, model_id: str, language: str) -> None:
        try:
            import mlx_whisper

            mlx_whisper.transcribe(
                np.zeros(16000, dtype=np.float32),
                path_or_hf_repo=model_id,
                **_language_kwargs(language),
            )
        except Exception as exc:  # pragma: no cover - depends on optional MLX/model IO
            raise AdapterError("MLX Whisper unavailable") from exc
        self._loaded_model = model_id

    def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
        return self._transcribe(
            audio_path,
            model_id,
            language,
            initial_prompt="",
        )

    def transcribe_with_hints(
        self,
        audio_path: Path,
        model_id: str,
        language: str,
        hints: RecognitionHints,
    ) -> str:
        """Transcribe with profile context through MLX Whisper's initial prompt."""
        return self._transcribe(
            audio_path,
            model_id,
            language,
            initial_prompt=hints.initial_prompt,
        )

    def _transcribe(
        self,
        audio_path: Path,
        model_id: str,
        language: str,
        *,
        initial_prompt: str,
    ) -> str:
        if self._loaded_model != model_id:
            self.preload(model_id, language)
        try:
            audio = _load_wav_float32_mono_16k(audio_path)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("Could not read WAV audio", kind="audio_unreadable") from exc
        try:
            import mlx_whisper

            kwargs = _language_kwargs(language)
            if initial_prompt:
                kwargs["initial_prompt"] = initial_prompt
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=model_id,
                **kwargs,
            )
        except Exception as exc:  # pragma: no cover - depends on optional MLX/model IO
            error = AdapterError("MLX Whisper transcribe failed")
            if initial_prompt:
                # A dependency exception may echo keyword arguments. Profile
                # context is private, so hinted calls must not retain a cause
                # that the service's diagnostic chain could log.
                raise error from None
            raise error from exc
        return _extract_text(result)


class MlxQwen3AsrAdapter:
    engine_id = "mlx-qwen3-asr"

    def __init__(self) -> None:
        self._loaded_model_id: str | None = None
        self._model: object | None = None

    def preload(self, model_id: str, language: str) -> None:
        model = self._ensure_model(model_id)
        if not callable(getattr(model, "generate", None)):
            raise AdapterError("mlx-audio Qwen3-ASR model has no generate() method")

    def transcribe(self, audio_path: Path, model_id: str, language: str) -> str:
        model = self._ensure_model(model_id)
        generate = getattr(model, "generate", None)
        if not callable(generate):
            raise AdapterError("mlx-audio Qwen3-ASR model has no generate() method")
        try:
            result = generate(str(audio_path), **_qwen_generate_kwargs(generate, language))
        except Exception as exc:  # pragma: no cover - depends on optional MLX/model IO
            _clear_mlx_cache()
            kind = _qwen_failure_kind(exc, audio_path)
            raise AdapterError(
                _qwen_public_failure_message(kind),
                diagnostic=_safe_exception_summary(exc),
                kind=kind,
            ) from exc
        return _extract_text(result)

    def _ensure_model(self, model_id: str) -> object:
        if self._model is not None and self._loaded_model_id == model_id:
            return self._model
        try:
            from mlx_audio.stt import load
        except Exception as exc:  # pragma: no cover - depends on optional MLX install
            raise AdapterError("mlx-audio is required for Qwen3-ASR") from exc
        try:
            if self._model is not None:
                self._model = None
                self._loaded_model_id = None
                _clear_mlx_cache()
            self._model = load(model_id)
        except Exception as exc:  # pragma: no cover - depends on optional MLX/model IO
            raise AdapterError("Qwen3-ASR unavailable") from exc
        self._loaded_model_id = model_id
        return self._model


def make_adapter(engine_id: str) -> ASRAdapter:
    if engine_id == "mlx-whisper":
        return MlxWhisperAdapter()
    if engine_id == "mlx-qwen3-asr":
        return MlxQwen3AsrAdapter()
    if engine_id == "dummy":
        return DummyAdapter()
    raise AdapterError(f"Unsupported engine: {engine_id}")


def _extract_text(result: object) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        value = result.get("text")
        if isinstance(value, str):
            return value.strip()
    if isinstance(result, list):
        return "".join(_extract_text(item) for item in result).strip()
    value = getattr(result, "text", None)
    if isinstance(value, str):
        return value.strip()
    raise AdapterError(f"Could not extract text from result type: {type(result)!r}")


def _language_kwargs(language: str) -> dict[str, str]:
    if language == "auto":
        return {}
    return {"language": language}


def _qwen_generate_kwargs(generate: object, language: str) -> dict[str, object]:
    try:
        signature = inspect.signature(generate)
    except (TypeError, ValueError):
        return _qwen_language_kwargs(language)

    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    kwargs: dict[str, object] = {}
    if accepts_kwargs or _accepts_keyword_parameter(signature, "language"):
        kwargs.update(_qwen_language_kwargs(language))
    if accepts_kwargs or _accepts_keyword_parameter(signature, "verbose"):
        kwargs["verbose"] = False
    return kwargs


def _accepts_keyword_parameter(signature: inspect.Signature, name: str) -> bool:
    parameter = signature.parameters.get(name)
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def _qwen_language_kwargs(language: str) -> dict[str, str | None]:
    if language == "auto":
        return {"language": None}
    return {"language": language}


def _safe_exception_summary(exc: BaseException) -> str:
    message = _sanitize_diagnostic_text(str(exc)).strip()
    if not message:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"[:180]


def _sanitize_diagnostic_text(value: str) -> str:
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
    return _restore_public_model_ids(value, public_model_ids)


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


def _qwen_failure_kind(exc: BaseException, audio_path: Path) -> AdapterErrorKind:
    chain = _exception_chain(exc)
    mentions_audio_input = any(_exception_references_audio_path(current, audio_path) for current in chain)
    if any(
        _exception_references_audio_path(current, audio_path)
        and isinstance(current, OSError)
        for current in chain
    ):
        return "audio_unreadable"
    read_failure = any(
        _looks_like_audio_read_failure(str(current).lower())
        for current in chain
    )
    if mentions_audio_input and read_failure:
        return "audio_unreadable"
    if any(
        _looks_like_pathless_audio_decoder_failure(str(current).lower())
        and _looks_like_pathless_audio_input_context(str(current).lower())
        for current in chain
    ):
        return "audio_unreadable"
    return "model_unavailable"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 5:
        chain.append(current)
        seen.add(id(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return chain


def _looks_like_audio_read_failure(message: str) -> bool:
    return (
        "audio file" in message
        or "failed to read" in message
        or "could not read" in message
        or "no such file" in message
        or "not found" in message
        or "permission denied" in message
        or "is a directory" in message
        or "error opening" in message
        or "format not recognised" in message
        or "format not recognized" in message
        or "unknown format" in message
    )


def _looks_like_pathless_audio_decoder_failure(message: str) -> bool:
    return (
        "format not recognised" in message
        or "format not recognized" in message
        or "unknown format" in message
    )


def _looks_like_pathless_audio_input_context(message: str) -> bool:
    return (
        "audio file" in message
        or "audio input" in message
        or "input audio" in message
        or "wav file" in message
        or "wave file" in message
    )


def _exception_references_audio_path(exc: BaseException, audio_path: Path) -> bool:
    audio_name = audio_path.name.lower()
    message = str(exc).lower()
    if (
        audio_path.is_absolute()
        and _message_mentions_exact_absolute_audio_path(message, audio_path)
    ) or (
        _has_directory_part(audio_path)
        and _message_mentions_exact_relative_audio_path(message, audio_path)
    ) or _message_mentions_pathless_audio_name(message, audio_name):
        return True
    for attr in ("filename", "filename2"):
        value = getattr(exc, attr, None)
        if isinstance(value, (str, bytes)):
            try:
                candidate = Path(value.decode() if isinstance(value, bytes) else value)
            except Exception:
                continue
            if candidate == audio_path:
                return True
            if not _has_directory_part(candidate) and candidate.name == audio_path.name:
                return True
    return False


def _message_mentions_exact_absolute_audio_path(message: str, audio_path: Path) -> bool:
    pattern = re.compile(
        rf"(?<![\w.-]){re.escape(str(audio_path).lower())}{_AUDIO_BOUNDARY}",
        re.IGNORECASE,
    )
    return pattern.search(message) is not None


def _message_mentions_exact_relative_audio_path(message: str, audio_path: Path) -> bool:
    pattern = re.compile(
        rf"(?<![\w/\\.-]){re.escape(str(audio_path).lower())}{_AUDIO_BOUNDARY}",
        re.IGNORECASE,
    )
    return pattern.search(message) is not None


def _message_mentions_pathless_audio_name(message: str, audio_name: str) -> bool:
    exact_pattern = re.compile(
        rf"(?<![/\\\w.;-]){re.escape(audio_name)}{_AUDIO_BOUNDARY}",
        re.IGNORECASE,
    )
    if exact_pattern.search(message):
        return True
    for match in _AUDIO_NAME_RE.finditer(message):
        if not _has_audio_name_boundaries(message, match.start(), match.end()):
            continue
        candidate = Path(match.group(0))
        if not _has_directory_part(candidate) and candidate.name.lower() == audio_name:
            return True
    return False


def _has_audio_name_boundaries(message: str, start: int, end: int) -> bool:
    if start > 0 and re.match(r"[/\\\w.;-]", message[start - 1]):
        return False
    if end < len(message) and (
        re.match(r"[/\\\w.-]", message[end])
        or _semicolon_suffix_is_path_continuation(message, end)
    ):
        return False
    return True


def _semicolon_suffix_is_path_continuation(message: str, offset: int) -> bool:
    if offset >= len(message) or message[offset] != ";":
        return False
    suffix = message[offset + 1:].lstrip()
    return "/" in suffix or "\\" in suffix


def _has_directory_part(path: Path) -> bool:
    return len(path.parts) > 1


def _qwen_public_failure_message(kind: AdapterErrorKind) -> str:
    if kind == "audio_unreadable":
        return "Qwen3-ASR audio file could not be read"
    return "Qwen3-ASR transcribe failed"


def _clear_mlx_cache() -> None:
    mx = sys.modules.get("mlx.core")
    clear_cache = getattr(mx, "clear_cache", None) if mx is not None else None
    if callable(clear_cache):
        try:  # pragma: no cover - optional MLX runtime
            clear_cache()
        except Exception as exc:
            logger.warning("Could not clear MLX cache: %s", _safe_exception_summary(exc))
    gc.collect()


def _load_wav_float32_mono_16k(audio_path: Path) -> np.ndarray:
    import soundfile as sf

    audio, samplerate = sf.read(audio_path, dtype="float32", always_2d=False)
    if samplerate != 16000:
        raise AdapterError(f"Expected 16 kHz WAV, got {samplerate} Hz", kind="audio_unreadable")
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if audio.ndim != 1:
        raise AdapterError("Expected mono WAV audio", kind="audio_unreadable")
    return np.asarray(audio, dtype=np.float32)

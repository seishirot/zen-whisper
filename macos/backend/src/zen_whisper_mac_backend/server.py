"""Unix-domain socket entrypoint for the native macOS backend."""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

from zen_whisper_mac_backend.logging_config import setup_logging
from zen_whisper_mac_backend.protocol import (
    MAX_LINE_BYTES,
    ProtocolError,
    decode_line,
    encode_message,
    error_response,
)
from zen_whisper_mac_backend.service import BackendService

logger = logging.getLogger(__name__)
MAX_HANDLER_THREADS = 8
BACKEND_CLOSE_TIMEOUT_SEC = 0.2
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
_AUDIO_NAME_WITH_SPACES_RE = re.compile(
    rf"(?<![\w/\\.-])[^/\n'\"<>:]*?\s+[^/\n'\"<>:]*?\.(?:wav|m4a|mp3|flac|ogg|aiff|aif){_AUDIO_BOUNDARY}",
    re.IGNORECASE,
)
_AUDIO_NAME_RE = re.compile(
    rf"\b[^\s'\"<>/\\]+\.(?:wav|m4a|mp3|flac|ogg|aiff|aif){_AUDIO_BOUNDARY}",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?:(?<![\w.-])/[^\n'\"<>]+|[A-Za-z]:\\[^\n'\"<>]+)")


def _parent_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return True
    if os.getppid() == 1 and parent_pid != 1:
        return False
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _start_parent_monitor(service: BackendService, parent_pid: int | None) -> None:
    if parent_pid is None:
        return

    def monitor() -> None:
        while not service.should_shutdown:
            if not _parent_is_alive(parent_pid):
                logger.info("Parent process exited; shutting down backend")
                service.shutdown(wait=False)
                return
            time.sleep(0.5)

    threading.Thread(target=monitor, daemon=True).start()


def _handle_connection(
    connection: socket.socket,
    service: BackendService,
    handler_slots: threading.BoundedSemaphore,
) -> None:
    try:
        with connection:
            connection.settimeout(1.0)
            reader = connection.makefile("rb")
            writer = connection.makefile("wb")
            try:
                line = reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                request = decode_line(line)
            except socket.timeout:
                logger.info("Protocol error: client read timed out")
                return
            except ProtocolError as exc:
                message = _protocol_error_message(exc)
                logger.info("Protocol error: class=%s message=%s", exc.__class__.__name__, message)
                response = error_response(
                    "unknown",
                    "PROTOCOL_ERROR",
                    message,
                    recoverable=False,
                )
            except Exception as exc:
                logger.error(
                    "Backend error before request dispatch: class=%s message=%s stack=%s",
                    exc.__class__.__name__,
                    _safe_exception_message(exc),
                    _safe_stack_summary(exc),
                )
                response = error_response(
                    "unknown",
                    "BACKEND_ERROR",
                    "Unexpected backend error",
                    recoverable=False,
                )
            else:
                try:
                    response = service.handle(request)
                except Exception as exc:  # pragma: no cover - defensive server boundary
                    logger.error(
                        "Backend error escaped service boundary: class=%s message=%s stack=%s",
                        exc.__class__.__name__,
                        _safe_exception_message(exc),
                        _safe_stack_summary(exc),
                    )
                    response = error_response(
                        str(request.get("request_id", "unknown")),
                        "BACKEND_ERROR",
                        "Unexpected backend error",
                        recoverable=False,
                    )
            try:
                writer.write(encode_message(response))
                writer.flush()
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                logger.info("Client disconnected before response could be sent")
    finally:
        handler_slots.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--log-dir")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--auth-token-stdin", action="store_true")
    args = parser.parse_args(argv)

    app_support = Path(
        os.environ.get(
            "ZEN_WHISPER_APP_SUPPORT",
            str(Path.home() / "Library/Application Support/zen-whisper"),
        )
    )
    log_dir = Path(args.log_dir) if args.log_dir else Path.home() / "Library/Logs/zen-whisper"
    setup_logging(log_dir)
    auth_token = sys.stdin.readline().rstrip("\n") if args.auth_token_stdin else None
    if not auth_token:
        logger.error("backend auth token is required")
        return 2

    socket_path = Path(args.socket_path)
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    app_support.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)

    service = BackendService(auth_token=auth_token)
    _start_parent_monitor(service, args.parent_pid)
    handler_slots = threading.BoundedSemaphore(MAX_HANDLER_THREADS)
    logger.info("Starting backend socket at %s", socket_path)
    cleanup_ok = True
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            server.listen(16)
            server.settimeout(0.5)
            while not service.should_shutdown:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                if not handler_slots.acquire(blocking=False):
                    logger.info("Protocol error: too many concurrent clients")
                    connection.close()
                    continue
                thread = threading.Thread(
                    target=_handle_connection,
                    args=(connection, service, handler_slots),
                    daemon=True,
                )
                thread.start()
    finally:
        try:
            socket_path.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_ok = False
            logger.warning(
                "Could not remove backend socket: class=%s errno=%s",
                exc.__class__.__name__,
                getattr(exc, "errno", None),
            )
        try:
            close_clean = service.close(wait=True, timeout=BACKEND_CLOSE_TIMEOUT_SEC)
        except Exception as exc:
            cleanup_ok = False
            logger.warning(
                "Backend service close failed: class=%s errno=%s message=%s",
                exc.__class__.__name__,
                getattr(exc, "errno", None),
                _safe_exception_message(exc),
            )
        else:
            cleanup_ok = cleanup_ok and close_clean
    if cleanup_ok:
        logger.info("Backend stopped")
        return 0
    else:
        logger.warning("Backend stopped with cleanup warnings")
        return 1


def _safe_exception_message(exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        return "<empty>"
    return _sanitize_log_text(message)[:180]


def _safe_stack_summary(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "<no traceback>"
    parts: list[str] = []
    for frame in frames[-3:]:
        location = f"{Path(frame.filename).name}:{frame.lineno}"
        source = _sanitize_log_text((frame.line or "").strip())
        parts.append(f"{location} {source}".strip())
    return " | ".join(parts)


def _sanitize_log_text(message: str) -> str:
    message, public_model_ids = _protect_public_model_ids(message)
    message = _PUBLIC_MODEL_ID_PRIVATE_SUFFIX_RE.sub(r"\1; <path>", message)
    message = _PUBLIC_MODEL_ID_PATH_SUFFIX_RE.sub(r"\1/<path>", message)
    message = _SEMICOLON_AUDIO_PREFIX_PATH_RE.sub("<path>", message)
    message = _RELATIVE_AUDIO_CONTEXT_RE.sub(r"\1<audio>", message)
    message = _RELATIVE_AUDIO_PATH_RE.sub("<audio>", message)
    message = _AUDIO_PATH_RE.sub("<audio>", message)
    message = _RELATIVE_PATH_CONTEXT_RE.sub(r"\1<path>", message)
    message = _SEMICOLON_SENSITIVE_PATH_RE.sub("<path>", message)
    message = _RELATIVE_SENSITIVE_PATH_CONTEXT_RE.sub(_redact_relative_sensitive_path_context, message)
    message = _RELATIVE_SENSITIVE_PATH_RE.sub(_redact_relative_sensitive_path, message)
    message = _PATH_RE.sub("<path>", message)
    message = _RELATIVE_FILE_PATH_RE.sub("<path>", message)
    message = _SEMICOLON_RELATIVE_PATH_RE.sub("<path>", message)
    message = _AUDIO_NAME_WITH_SPACES_RE.sub("<audio>", message)
    message = _AUDIO_NAME_RE.sub("<audio>", message)
    message = _restore_public_model_ids(message, public_model_ids)
    return message[:240]


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


def _protocol_error_message(exc: BaseException) -> str:
    if isinstance(exc, ProtocolError):
        return _safe_exception_message(exc)
    return exc.__class__.__name__


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

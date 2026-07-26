"""Immutable profile hints and shell-free transcript postprocessing."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Literal

from zen_whisper_mac_backend.protocol import MAX_LINE_BYTES

logger = logging.getLogger(__name__)

EnhancementMode = Literal["off", "dictionary", "preset"]
EnhancementOutcome = Literal["raw", "dictionary", "cli", "cli_fallback"]
ProcessStartedCallback = Callable[[subprocess.Popen[Any]], bool]
ProcessFinishedCallback = Callable[[subprocess.Popen[Any]], None]

_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
_UNSAFE_OUTPUT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
# Invalid UTF-8 can expand to a three-byte replacement character. Reserving a
# quarter of the 1 MiB transport budget for JSON escaping and result metadata
# keeps every accepted CLI result below UnixSocketClient.maxResponseBytes.
_RESPONSE_ENVELOPE_BUDGET_BYTES = MAX_LINE_BYTES // 4
_MAX_CLI_STREAM_BYTES = (
    MAX_LINE_BYTES - _RESPONSE_ENVELOPE_BUDGET_BYTES
) // 3
_MAX_TRANSCRIBE_REQUEST_BYTES = MAX_LINE_BYTES // 2
_MAX_ENHANCEMENT_PAYLOAD_BYTES = MAX_LINE_BYTES // 4
_MAX_TRANSCRIBE_VALUE_COUNT = 8_192
_MAX_ENHANCEMENT_VALUE_COUNT = 4_096
_MAX_REQUEST_ID_BYTES = 256
_MAX_PROFILE_TERMS = 256
_MAX_TERM_ALIASES = 128
_MAX_PRESET_ARGUMENTS = 256
_MAX_PRESET_ENVIRONMENT_ENTRIES = 128
_MAX_POSTPROCESSOR_TIMEOUT_SEC = 300.0
_STREAM_READ_CHUNK_BYTES = 64 * 1024
_PROCESS_CLEANUP_TIMEOUT_SEC = 1.0
_ALLOWED_PLACEHOLDERS = frozenset(
    {
        "prompt",
        "transcript",
        "context",
        "terms",
        "profile_name",
        "language",
    }
)
_PROFILE_FIELDS = frozenset({"id", "name", "context", "terms"})
_TERM_FIELDS = frozenset(
    {"canonical", "spoken", "replace_from", "description"}
)
_POSTPROCESSOR_FIELDS = frozenset({"mode", "preset"})
_PRESET_FIELDS = frozenset(
    {
        "id",
        "display_name",
        "executable",
        "arguments",
        "preflight_executable",
        "preflight_arguments",
        "preflight_failure_message",
        "input_mode",
        "output_mode",
        "timeout_sec",
        "data_destination",
        "prompt_template",
        "environment",
    }
)


class EnhancementValidationError(ValueError):
    """Raised when an enhancement payload violates the wire schema."""


@dataclass(frozen=True)
class ProfileTerm:
    canonical: str
    spoken: tuple[str, ...]
    replace_from: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class Profile:
    profile_id: str
    name: str
    context: str
    terms: tuple[ProfileTerm, ...]


@dataclass(frozen=True)
class RecognitionHints:
    context: str
    hotwords: tuple[str, ...]

    @property
    def initial_prompt(self) -> str:
        """Return the profile context consumed by hint-aware MLX Whisper."""
        if self.context:
            return self.context
        if self.hotwords:
            return f"重要語彙: {', '.join(self.hotwords)}"
        return ""


@dataclass(frozen=True)
class PostprocessorPreset:
    preset_id: str
    display_name: str
    executable: str
    arguments: tuple[str, ...]
    preflight_executable: str | None
    preflight_arguments: tuple[str, ...]
    preflight_failure_message: str
    input_mode: Literal["stdin", "argument"]
    output_mode: Literal["stdout"]
    timeout_sec: float
    data_destination: Literal["local", "remote", "unknown"]
    prompt_template: str
    environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PostprocessorSelection:
    mode: EnhancementMode
    preset: PostprocessorPreset | None = None


@dataclass(frozen=True)
class PostprocessResult:
    text: str
    outcome: EnhancementOutcome
    cli_selected: bool
    succeeded: bool
    applied: bool
    warning_code: str | None = None
    error: str | None = None

    def response_metadata(self) -> dict[str, object]:
        result: dict[str, object] = {
            "outcome": self.outcome,
            "cli_selected": self.cli_selected,
            "succeeded": self.succeeded,
            "applied": self.applied,
        }
        if self.warning_code is not None:
            result["warning_code"] = self.warning_code
        if self.error is not None:
            result["error"] = self.error
        return result


@dataclass
class _StreamCapture:
    keep_content: bool
    content: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0
    overflowed: bool = False
    exception_type: str | None = None


@dataclass(frozen=True)
class _CommandResult:
    status: Literal["completed", "timeout", "too_large", "communication_failed"]
    returncode: int | None
    stdout: str
    stdout_bytes: int
    stderr_bytes: int
    exception_type: str | None = None


def validate_transcribe_request(request: dict[str, Any]) -> None:
    """Reject resource-heavy enhancement payloads before ASR starts."""
    request_id = request.get("request_id")
    if isinstance(request_id, str):
        try:
            request_id_size = len(request_id.encode("utf-8"))
        except UnicodeError as exc:
            raise EnhancementValidationError(
                "transcribe request_id contains invalid text"
            ) from exc
        if request_id_size > _MAX_REQUEST_ID_BYTES:
            raise EnhancementValidationError(
                "transcribe request_id is too large"
            )
    _validate_json_budget(
        request,
        byte_limit=_MAX_TRANSCRIBE_REQUEST_BYTES,
        value_limit=_MAX_TRANSCRIBE_VALUE_COUNT,
        label="transcribe request",
    )
    enhancement_payload = {
        key: request[key]
        for key in ("profile", "postprocessor")
        if key in request
    }
    _validate_json_budget(
        enhancement_payload,
        byte_limit=_MAX_ENHANCEMENT_PAYLOAD_BYTES,
        value_limit=_MAX_ENHANCEMENT_VALUE_COUNT,
        label="enhancement payload",
    )

    profile = request.get("profile")
    if isinstance(profile, dict):
        terms = profile.get("terms")
        if isinstance(terms, list):
            if len(terms) > _MAX_PROFILE_TERMS:
                raise EnhancementValidationError(
                    "profile contains too many terms"
                )
            for term in terms:
                if not isinstance(term, dict):
                    continue
                for key in ("spoken", "replace_from"):
                    aliases = term.get(key)
                    if (
                        isinstance(aliases, list)
                        and len(aliases) > _MAX_TERM_ALIASES
                    ):
                        raise EnhancementValidationError(
                            "profile term contains too many aliases"
                        )

    postprocessor = request.get("postprocessor")
    if not isinstance(postprocessor, dict):
        return
    preset = postprocessor.get("preset")
    if not isinstance(preset, dict):
        return
    for key in ("arguments", "preflight_arguments"):
        arguments = preset.get(key)
        if (
            isinstance(arguments, list)
            and len(arguments) > _MAX_PRESET_ARGUMENTS
        ):
            raise EnhancementValidationError(
                "postprocessor preset contains too many arguments"
            )
    environment = preset.get("environment")
    if (
        isinstance(environment, dict)
        and len(environment) > _MAX_PRESET_ENVIRONMENT_ENTRIES
    ):
        raise EnhancementValidationError(
            "postprocessor preset contains too many environment entries"
        )


def parse_profile(value: object) -> Profile | None:
    """Parse the data-only profile carried by a transcribe request."""
    if value is None:
        return None
    raw = _required_object(value, "profile")
    _reject_unknown_fields(raw, _PROFILE_FIELDS, "profile")
    _require_fields(raw, _PROFILE_FIELDS, "profile")

    profile_id = _nonempty_string(raw["id"], "profile.id")
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        raise EnhancementValidationError("profile.id has an invalid format")
    name = _nonempty_string(raw["name"], "profile.name")
    context = _string(raw["context"], "profile.context").strip()
    raw_terms = raw["terms"]
    if not isinstance(raw_terms, list):
        raise EnhancementValidationError("profile.terms must be an array")

    terms: list[ProfileTerm] = []
    replacements: dict[str, str] = {}
    for index, value_term in enumerate(raw_terms):
        field = f"profile.terms[{index}]"
        raw_term = _required_object(value_term, field)
        _reject_unknown_fields(raw_term, _TERM_FIELDS, field)
        _require_fields(raw_term, _TERM_FIELDS, field)
        canonical = _nonempty_string(raw_term["canonical"], f"{field}.canonical")
        spoken = _string_array(raw_term["spoken"], f"{field}.spoken")
        replace_from = _string_array(
            raw_term["replace_from"], f"{field}.replace_from"
        )
        description = _string(raw_term["description"], f"{field}.description").strip()
        for source in replace_from:
            previous = replacements.get(source)
            if previous is not None and previous != canonical:
                raise EnhancementValidationError(
                    f"{field}.replace_from conflicts with another term"
                )
            replacements[source] = canonical
        terms.append(
            ProfileTerm(
                canonical=canonical,
                spoken=spoken,
                replace_from=replace_from,
                description=description,
            )
        )

    return Profile(
        profile_id=profile_id,
        name=name,
        context=context,
        terms=tuple(terms),
    )


def recognition_hints(profile: Profile | None) -> RecognitionHints:
    """Build deterministic ASR hints without treating replacements as hotwords."""
    if profile is None:
        return RecognitionHints(context="", hotwords=())

    hotwords: list[str] = []
    for term in profile.terms:
        hotwords.append(term.canonical)
        hotwords.extend(term.spoken)

    context_parts: list[str] = []
    if profile.context:
        context_parts.append(profile.context)
    if profile.terms:
        context_parts.append(f"重要語彙:\n{render_terms(profile)}")
    return RecognitionHints(
        context="\n\n".join(context_parts),
        hotwords=tuple(dict.fromkeys(hotwords)),
    )


def render_terms(profile: Profile | None) -> str:
    if profile is None or not profile.terms:
        return "（なし）"
    lines: list[str] = []
    for term in profile.terms:
        details: list[str] = []
        if term.spoken:
            details.append(f"読み・呼び方: {', '.join(term.spoken)}")
        if term.replace_from:
            details.append(f"よくある誤認識: {', '.join(term.replace_from)}")
        if term.description:
            details.append(term.description)
        suffix = f" — {'; '.join(details)}" if details else ""
        lines.append(f"- {term.canonical}{suffix}")
    return "\n".join(lines)


def parse_postprocessor(value: object) -> PostprocessorSelection:
    """Parse an optional immutable postprocessor selection."""
    if value is None:
        return PostprocessorSelection(mode="off")
    raw = _required_object(value, "postprocessor")
    _reject_unknown_fields(raw, _POSTPROCESSOR_FIELDS, "postprocessor")
    if "mode" not in raw:
        raise EnhancementValidationError("postprocessor.mode is required")
    mode = _string(raw["mode"], "postprocessor.mode")
    if mode not in {"off", "dictionary", "preset"}:
        raise EnhancementValidationError(
            "postprocessor.mode must be off, dictionary, or preset"
        )
    if mode != "preset":
        if "preset" in raw and raw["preset"] is not None:
            raise EnhancementValidationError(
                "postprocessor.preset is only valid in preset mode"
            )
        return PostprocessorSelection(mode=mode)  # type: ignore[arg-type]
    if "preset" not in raw or raw["preset"] is None:
        raise EnhancementValidationError(
            "postprocessor.preset is required in preset mode"
        )
    return PostprocessorSelection(mode="preset", preset=_parse_preset(raw["preset"]))


def apply_replacements(text: str, profile: Profile | None) -> str:
    """Apply literal case-sensitive replacements once, longest source first."""
    if profile is None:
        return text
    replacements: dict[str, str] = {}
    for term in profile.terms:
        for source in term.replace_from:
            replacements[source] = term.canonical
    if not replacements:
        return text
    sources = sorted(replacements, key=lambda source: (-len(source), source))
    pattern = re.compile("|".join(re.escape(source) for source in sources))
    return pattern.sub(lambda match: replacements[match.group(0)], text)


def process_transcript(
    text: str,
    profile: Profile | None,
    selection: PostprocessorSelection,
    language: str,
    *,
    on_process_started: ProcessStartedCallback | None = None,
    on_process_finished: ProcessFinishedCallback | None = None,
) -> PostprocessResult:
    """Apply dictionary replacement and an optional trusted local CLI."""
    if selection.mode == "off":
        return PostprocessResult(
            text=text,
            outcome="raw",
            cli_selected=False,
            succeeded=True,
            applied=False,
        )

    corrected = apply_replacements(text, profile)
    if selection.mode == "dictionary":
        return PostprocessResult(
            text=corrected,
            outcome="dictionary",
            cli_selected=False,
            succeeded=True,
            applied=corrected != text,
        )

    preset = selection.preset
    if preset is None:  # pragma: no cover - protected by parsing and type shape
        raise EnhancementValidationError(
            "postprocessor.preset is required in preset mode"
        )
    return _run_postprocessor(
        preset,
        corrected,
        profile,
        language,
        on_process_started=on_process_started,
        on_process_finished=on_process_finished,
    )


def _parse_preset(value: object) -> PostprocessorPreset:
    raw = _required_object(value, "postprocessor.preset")
    _reject_unknown_fields(raw, _PRESET_FIELDS, "postprocessor.preset")
    required_fields = _PRESET_FIELDS - {
        "preflight_executable",
        "data_destination",
    }
    _require_fields(raw, required_fields, "postprocessor.preset")

    preset_id = _nonempty_string(raw["id"], "postprocessor.preset.id")
    if not _PROFILE_ID_RE.fullmatch(preset_id):
        raise EnhancementValidationError(
            "postprocessor.preset.id has an invalid format"
        )
    display_name = _nonempty_string(
        raw["display_name"], "postprocessor.preset.display_name"
    )
    executable = _nonempty_string(
        raw["executable"], "postprocessor.preset.executable"
    )
    arguments = _string_array(
        raw["arguments"], "postprocessor.preset.arguments", allow_empty=True
    )

    preflight_value = raw.get("preflight_executable")
    preflight_executable: str | None
    if preflight_value is None:
        preflight_executable = None
    else:
        preflight_executable = _nonempty_string(
            preflight_value, "postprocessor.preset.preflight_executable"
        )
    preflight_arguments = _string_array(
        raw["preflight_arguments"],
        "postprocessor.preset.preflight_arguments",
        allow_empty=True,
    )
    if preflight_executable is None and preflight_arguments:
        raise EnhancementValidationError(
            "postprocessor.preset.preflight_arguments requires preflight_executable"
        )

    preflight_failure_message = _string(
        raw["preflight_failure_message"],
        "postprocessor.preset.preflight_failure_message",
    ).strip()
    input_mode = _string(raw["input_mode"], "postprocessor.preset.input_mode")
    if input_mode not in {"stdin", "argument"}:
        raise EnhancementValidationError(
            "postprocessor.preset.input_mode must be stdin or argument"
        )
    output_mode = _string(raw["output_mode"], "postprocessor.preset.output_mode")
    if output_mode != "stdout":
        raise EnhancementValidationError(
            "postprocessor.preset.output_mode must be stdout"
        )
    timeout_value = raw["timeout_sec"]
    if (
        not isinstance(timeout_value, (int, float))
        or isinstance(timeout_value, bool)
        or not math.isfinite(timeout_value)
        or timeout_value <= 0
        or timeout_value > _MAX_POSTPROCESSOR_TIMEOUT_SEC
    ):
        raise EnhancementValidationError(
            "postprocessor.preset.timeout_sec must be greater than 0 "
            "and at most 300"
        )
    timeout_sec = float(timeout_value)
    data_destination = _string(
        raw.get("data_destination", "unknown"),
        "postprocessor.preset.data_destination",
    )
    if data_destination not in {"local", "remote", "unknown"}:
        raise EnhancementValidationError(
            "postprocessor.preset.data_destination must be local, remote, or unknown"
        )
    prompt_template = _string(
        raw["prompt_template"], "postprocessor.preset.prompt_template"
    )
    environment = _environment(raw["environment"])

    _validate_no_nul(executable, "postprocessor.preset.executable")
    for index, argument in enumerate(arguments):
        _validate_no_nul(argument, f"postprocessor.preset.arguments[{index}]")
    if preflight_executable is not None:
        _validate_no_nul(
            preflight_executable,
            "postprocessor.preset.preflight_executable",
        )
    for index, argument in enumerate(preflight_arguments):
        _validate_no_nul(
            argument,
            f"postprocessor.preset.preflight_arguments[{index}]",
        )

    _validate_template(prompt_template, "postprocessor.preset.prompt_template")
    if "{{transcript}}" not in prompt_template:
        raise EnhancementValidationError(
            "postprocessor.preset.prompt_template must contain {{transcript}}"
        )
    if _contains_placeholder(executable):
        raise EnhancementValidationError(
            "postprocessor.preset.executable cannot contain placeholders"
        )
    preflight_values = (preflight_executable or "", *preflight_arguments)
    if any(_contains_placeholder(value) for value in preflight_values):
        raise EnhancementValidationError(
            "postprocessor.preset preflight cannot contain placeholders"
        )

    invocation_placeholders = set().union(
        *(_find_placeholders(value) for value in arguments),
    )
    for index, argument in enumerate(arguments):
        _validate_template(argument, f"postprocessor.preset.arguments[{index}]")
    if input_mode == "stdin" and invocation_placeholders:
        raise EnhancementValidationError(
            "postprocessor.preset stdin arguments cannot contain placeholders"
        )
    if input_mode == "argument" and "prompt" not in invocation_placeholders:
        raise EnhancementValidationError(
            "postprocessor.preset argument mode requires {{prompt}} in arguments"
        )

    return PostprocessorPreset(
        preset_id=preset_id,
        display_name=display_name,
        executable=executable,
        arguments=arguments,
        preflight_executable=preflight_executable,
        preflight_arguments=preflight_arguments,
        preflight_failure_message=preflight_failure_message,
        input_mode=input_mode,  # type: ignore[arg-type]
        output_mode=output_mode,  # type: ignore[arg-type]
        timeout_sec=timeout_sec,
        data_destination=data_destination,  # type: ignore[arg-type]
        prompt_template=prompt_template,
        environment=environment,
    )


def _run_postprocessor(
    preset: PostprocessorPreset,
    transcript: str,
    profile: Profile | None,
    language: str,
    *,
    on_process_started: ProcessStartedCallback | None,
    on_process_finished: ProcessFinishedCallback | None,
) -> PostprocessResult:
    try:
        return _run_postprocessor_impl(
            preset,
            transcript,
            profile,
            language,
            on_process_started=on_process_started,
            on_process_finished=on_process_finished,
        )
    except Exception as exc:
        _log_failure(
            preset,
            "internal_failure",
            transcript,
            exception_type=type(exc).__name__,
        )
        return _fallback(
            transcript,
            (
                "CLI_FAILED",
                f"{preset.display_name} の実行に失敗しました",
            ),
        )


def _run_postprocessor_impl(
    preset: PostprocessorPreset,
    transcript: str,
    profile: Profile | None,
    language: str,
    *,
    on_process_started: ProcessStartedCallback | None,
    on_process_finished: ProcessFinishedCallback | None,
) -> PostprocessResult:
    values = _template_values(transcript, profile, language)
    prompt = _render(preset.prompt_template, values)
    values["prompt"] = prompt
    arguments = tuple(_render(argument, values) for argument in preset.arguments)
    environment = os.environ.copy()
    cli_path = os.environ.get("ZEN_WHISPER_CLI_PATH")
    environment["PATH"] = cli_path or os.environ.get("PATH") or os.defpath
    environment.pop("ZEN_WHISPER_CLI_PATH", None)
    environment.update(dict(preset.environment))

    with tempfile.TemporaryDirectory(prefix="zen_whisper_postprocess_") as temp_dir:
        if preset.preflight_executable is not None:
            failure = _run_preflight(
                preset,
                temp_dir,
                environment,
                on_process_started=on_process_started,
                on_process_finished=on_process_finished,
            )
            if failure is not None:
                return _fallback(transcript, failure)

        process: subprocess.Popen[Any] | None = None
        try:
            process = subprocess.Popen(
                [preset.executable, *arguments],
                shell=False,
                cwd=temp_dir,
                env=environment,
                stdin=(
                    subprocess.PIPE
                    if preset.input_mode == "stdin"
                    else subprocess.DEVNULL
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            _log_failure(preset, "executable_not_found", transcript)
            return _fallback(
                transcript,
                (
                    "EXECUTABLE_NOT_FOUND",
                    f"{preset.display_name} の実行ファイルが見つかりません",
                ),
            )
        except Exception as exc:
            _log_failure(
                preset,
                "launch_failed",
                transcript,
                exception_type=type(exc).__name__,
            )
            return _fallback(
                transcript,
                (
                    "CLI_LAUNCH_FAILED",
                    f"{preset.display_name} の実行を開始できませんでした",
                ),
            )

        authorized, registered, callback_error = _register_active_process(
            process,
            on_process_started,
        )
        if not authorized:
            terminate_process_group(process)
            _close_process_streams(process)
            _log_failure(
                preset,
                "process_registration_rejected",
                transcript,
                exception_type=callback_error,
            )
            return _fallback(
                transcript,
                (
                    "CLI_CANCELLED",
                    f"{preset.display_name} は終了処理のためキャンセルされました",
                ),
            )

        try:
            command_result = _communicate_bounded(
                process,
                (
                    prompt.encode("utf-8")
                    if preset.input_mode == "stdin"
                    else None
                ),
                preset.timeout_sec,
            )
            if command_result.status == "timeout":
                _log_failure(preset, "timeout", transcript)
                return _fallback(
                    transcript,
                    (
                        "CLI_TIMEOUT",
                        f"{preset.display_name} がタイムアウトしました",
                    ),
                )
            if command_result.status == "too_large":
                _log_failure(
                    preset,
                    "output_too_large",
                    transcript,
                    output_length=command_result.stdout_bytes,
                    error_length=command_result.stderr_bytes,
                )
                return _fallback(
                    transcript,
                    (
                        "CLI_OUTPUT_TOO_LARGE",
                        f"{preset.display_name} の出力が大きすぎます",
                    ),
                )
            if command_result.status == "communication_failed":
                _log_failure(
                    preset,
                    "communication_failed",
                    transcript,
                    output_length=command_result.stdout_bytes,
                    error_length=command_result.stderr_bytes,
                    exception_type=command_result.exception_type,
                )
                return _fallback(
                    transcript,
                    (
                        "CLI_COMMUNICATION_FAILED",
                        f"{preset.display_name} の実行に失敗しました",
                    ),
                )
            if command_result.returncode != 0:
                _log_failure(
                    preset,
                    "exit_nonzero",
                    transcript,
                    output_length=command_result.stdout_bytes,
                    error_length=command_result.stderr_bytes,
                )
                return _fallback(
                    transcript,
                    (
                        "CLI_FAILED",
                        f"{preset.display_name} が正常に完了しませんでした",
                    ),
                )
            output = sanitize_cli_output(command_result.stdout)
            if not output:
                _log_failure(
                    preset,
                    "empty_output",
                    transcript,
                    output_length=command_result.stdout_bytes,
                )
                return _fallback(
                    transcript,
                    (
                        "CLI_OUTPUT_EMPTY",
                        f"{preset.display_name} の出力が空でした",
                    ),
                )
            logger.info(
                "Postprocessing completed: preset=%s status=success "
                "input_chars=%d output_chars=%d",
                preset.preset_id,
                len(transcript),
                len(output),
            )
            return PostprocessResult(
                text=output,
                outcome="cli",
                cli_selected=True,
                succeeded=True,
                applied=True,
            )
        finally:
            if registered:
                _notify_process_finished(
                    process,
                    on_process_finished,
                    preset,
                    transcript,
                )
            _close_process_streams(process)


def _run_preflight(
    preset: PostprocessorPreset,
    cwd: str,
    environment: dict[str, str],
    *,
    on_process_started: ProcessStartedCallback | None,
    on_process_finished: ProcessFinishedCallback | None,
) -> tuple[str, str] | None:
    executable = preset.preflight_executable
    if executable is None:
        return None
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            [executable, *preset.preflight_arguments],
            shell=False,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        _log_failure(preset, "preflight_executable_not_found", "")
        return (
            "PREFLIGHT_FAILED",
            preset.preflight_failure_message
            or f"{preset.display_name} の事前確認に失敗しました",
        )
    except Exception as exc:
        _log_failure(
            preset,
            "preflight_launch_failed",
            "",
            exception_type=type(exc).__name__,
        )
        return (
            "PREFLIGHT_FAILED",
            preset.preflight_failure_message
            or f"{preset.display_name} の事前確認に失敗しました",
        )

    authorized, registered, callback_error = _register_active_process(
        process,
        on_process_started,
    )
    if not authorized:
        terminate_process_group(process)
        _close_process_streams(process)
        _log_failure(
            preset,
            "preflight_registration_rejected",
            "",
            exception_type=callback_error,
        )
        return (
            "CLI_CANCELLED",
            f"{preset.display_name} は終了処理のためキャンセルされました",
        )

    try:
        try:
            returncode = process.wait(timeout=min(preset.timeout_sec, 10.0))
        except subprocess.TimeoutExpired:
            terminate_process_group(process)
            _log_failure(preset, "preflight_timeout", "")
            return (
                "PREFLIGHT_TIMEOUT",
                f"{preset.display_name} の事前確認がタイムアウトしました",
            )
        if returncode != 0:
            _log_failure(preset, "preflight_exit_nonzero", "")
            return (
                "PREFLIGHT_FAILED",
                preset.preflight_failure_message
                or f"{preset.display_name} の事前確認に失敗しました",
            )
        logger.info(
            "Postprocessing preflight completed: preset=%s status=success",
            preset.preset_id,
        )
        return None
    finally:
        if registered:
            _notify_process_finished(
                process,
                on_process_finished,
                preset,
                "",
            )
        _close_process_streams(process)


def _fallback(
    corrected: str,
    failure: tuple[str, str],
) -> PostprocessResult:
    warning_code, message = failure
    return PostprocessResult(
        text=corrected,
        outcome="cli_fallback",
        cli_selected=True,
        succeeded=False,
        applied=True,
        warning_code=warning_code,
        error=_public_error(message),
    )


def sanitize_cli_output(value: str) -> str:
    """Flatten unsafe control/separator runs and reject whitespace-only output."""
    return _UNSAFE_OUTPUT_CONTROL_RE.sub(" ", value).strip()


def _template_values(
    transcript: str,
    profile: Profile | None,
    language: str,
) -> dict[str, str]:
    return {
        "prompt": "",
        "transcript": transcript,
        "context": profile.context if profile and profile.context else "（なし）",
        "terms": render_terms(profile),
        "profile_name": profile.name if profile else "（なし）",
        "language": language,
    }


def _render(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)


def _find_placeholders(value: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(value))


def _contains_placeholder(value: str) -> bool:
    return _ANY_PLACEHOLDER_RE.search(value) is not None


def _validate_template(value: str, field: str) -> None:
    unknown = set(_ANY_PLACEHOLDER_RE.findall(value)) - _ALLOWED_PLACEHOLDERS
    if unknown:
        raise EnhancementValidationError(f"{field} has unsupported placeholders")


def _register_active_process(
    process: subprocess.Popen[Any],
    callback: ProcessStartedCallback | None,
) -> tuple[bool, bool, str | None]:
    if callback is None:
        return True, False, None
    try:
        accepted = callback(process)
    except Exception as exc:
        return False, False, type(exc).__name__
    if not accepted:
        return False, False, None
    return True, True, None


def _notify_process_finished(
    process: subprocess.Popen[Any],
    callback: ProcessFinishedCallback | None,
    preset: PostprocessorPreset,
    transcript: str,
) -> None:
    if callback is None:
        return
    try:
        callback(process)
    except Exception as exc:
        _log_failure(
            preset,
            "process_finish_callback_failed",
            transcript,
            exception_type=type(exc).__name__,
        )


def _communicate_bounded(
    process: subprocess.Popen[Any],
    input_data: bytes | None,
    timeout_sec: float,
) -> _CommandResult:
    """Drain process pipes concurrently without retaining unbounded output."""
    stdout_capture = _StreamCapture(keep_content=True)
    stderr_capture = _StreamCapture(keep_content=False)
    wake = threading.Event()
    writer_errors: list[str] = []
    threads: list[threading.Thread] = []

    if process.stdout is None or process.stderr is None:
        terminate_process_group(process)
        return _CommandResult(
            status="communication_failed",
            returncode=process.returncode,
            stdout="",
            stdout_bytes=0,
            stderr_bytes=0,
            exception_type="MissingPipe",
        )

    threads.extend(
        [
            threading.Thread(
                target=_drain_stream,
                args=(process.stdout, stdout_capture, wake),
                name="zen-whisper-cli-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=_drain_stream,
                args=(process.stderr, stderr_capture, wake),
                name="zen-whisper-cli-stderr",
                daemon=True,
            ),
        ]
    )
    if input_data is not None:
        if process.stdin is None:
            terminate_process_group(process)
            return _CommandResult(
                status="communication_failed",
                returncode=process.returncode,
                stdout="",
                stdout_bytes=0,
                stderr_bytes=0,
                exception_type="MissingPipe",
            )
        threads.append(
            threading.Thread(
                target=_write_stdin,
                args=(process.stdin, input_data, writer_errors, wake),
                name="zen-whisper-cli-stdin",
                daemon=True,
            )
        )

    try:
        for thread in threads:
            thread.start()
    except Exception as exc:
        terminate_process_group(process)
        _join_threads(threads)
        return _CommandResult(
            status="communication_failed",
            returncode=process.returncode,
            stdout="",
            stdout_bytes=stdout_capture.total_bytes,
            stderr_bytes=stderr_capture.total_bytes,
            exception_type=type(exc).__name__,
        )

    deadline = time.monotonic() + timeout_sec
    status: Literal["completed", "timeout", "too_large", "communication_failed"]
    while True:
        if stdout_capture.overflowed or stderr_capture.overflowed:
            status = "too_large"
            break
        if (
            stdout_capture.exception_type is not None
            or stderr_capture.exception_type is not None
            or writer_errors
        ):
            status = "communication_failed"
            break
        if process.poll() is not None:
            status = "completed"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            status = "timeout"
            break
        wake.wait(timeout=min(remaining, 0.05))
        wake.clear()

    if status != "completed":
        terminate_process_group(process)
    if not _join_threads(threads):
        terminate_process_group(process)
        _join_threads(threads)
        status = "communication_failed"

    exception_type = (
        stdout_capture.exception_type
        or stderr_capture.exception_type
        or (writer_errors[0] if writer_errors else None)
    )
    if status == "completed" and exception_type is not None:
        status = "communication_failed"
    if status == "completed" and (
        stdout_capture.overflowed or stderr_capture.overflowed
    ):
        status = "too_large"

    stdout = ""
    if status == "completed":
        stdout = bytes(stdout_capture.content).decode(
            "utf-8",
            errors="replace",
        )
    return _CommandResult(
        status=status,
        returncode=process.returncode,
        stdout=stdout,
        stdout_bytes=stdout_capture.total_bytes,
        stderr_bytes=stderr_capture.total_bytes,
        exception_type=exception_type,
    )


def _drain_stream(
    stream: BinaryIO,
    capture: _StreamCapture,
    wake: threading.Event,
) -> None:
    try:
        while True:
            chunk = stream.read(_STREAM_READ_CHUNK_BYTES)
            if not chunk:
                return
            capture.total_bytes += len(chunk)
            if capture.keep_content and len(capture.content) <= _MAX_CLI_STREAM_BYTES:
                remaining = _MAX_CLI_STREAM_BYTES + 1 - len(capture.content)
                capture.content.extend(chunk[:remaining])
            if capture.total_bytes > _MAX_CLI_STREAM_BYTES:
                capture.overflowed = True
                wake.set()
    except Exception as exc:
        capture.exception_type = type(exc).__name__
        wake.set()
    finally:
        try:
            stream.close()
        except Exception:
            pass
        wake.set()


def _write_stdin(
    stream: BinaryIO,
    input_data: bytes,
    errors: list[str],
    wake: threading.Event,
) -> None:
    try:
        stream.write(input_data)
        stream.flush()
    except BrokenPipeError:
        pass
    except Exception as exc:
        errors.append(type(exc).__name__)
        wake.set()
    finally:
        try:
            stream.close()
        except Exception:
            pass
        wake.set()


def _join_threads(threads: list[threading.Thread]) -> bool:
    deadline = time.monotonic() + _PROCESS_CLEANUP_TIMEOUT_SEC
    for thread in threads:
        if thread.ident is None:
            continue
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return all(thread.ident is None or not thread.is_alive() for thread in threads)


def terminate_process_group(process: subprocess.Popen[Any]) -> None:
    """Terminate and reap a timed-out process group with bounded waits."""
    process_group = process.pid
    _signal_process_group(process_group, signal.SIGTERM)
    try:
        process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SEC)
    except Exception:
        pass
    _signal_process_group(process_group, signal.SIGKILL)
    try:
        process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SEC)
    except Exception:
        pass


def _signal_process_group(process_group: int, sig: signal.Signals) -> None:
    try:
        os.killpg(process_group, sig)
    except OSError:
        return


def _close_process_streams(process: subprocess.Popen[Any]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _log_failure(
    preset: PostprocessorPreset,
    status: str,
    transcript: str,
    *,
    output_length: int | None = None,
    error_length: int | None = None,
    exception_type: str | None = None,
) -> None:
    logger.warning(
        "Postprocessing failed: preset=%s status=%s input_chars=%d "
        "output_chars=%s error_chars=%s exception_type=%s",
        preset.preset_id,
        status,
        len(transcript),
        output_length if output_length is not None else 0,
        error_length if error_length is not None else 0,
        exception_type or "none",
    )


def _public_error(value: str) -> str:
    sanitized = sanitize_cli_output(value)
    return sanitized or "後処理に失敗しました"


def _validate_json_budget(
    value: object,
    *,
    byte_limit: int,
    value_limit: int,
    label: str,
) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise EnhancementValidationError(
            f"{label} must contain valid JSON values"
        ) from exc
    if len(encoded) > byte_limit:
        raise EnhancementValidationError(f"{label} is too large")

    count = 0
    stack = [value]
    while stack:
        current = stack.pop()
        count += 1
        if count > value_limit:
            raise EnhancementValidationError(f"{label} has too many values")
        if isinstance(current, dict):
            count += len(current)
            if count > value_limit:
                raise EnhancementValidationError(
                    f"{label} has too many values"
                )
            stack.extend(current.values())
        elif isinstance(current, list):
            count += len(current)
            if count > value_limit:
                raise EnhancementValidationError(
                    f"{label} has too many values"
                )
            stack.extend(current)


def _required_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EnhancementValidationError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise EnhancementValidationError(f"{field} keys must be strings")
    return value


def _reject_unknown_fields(
    raw: dict[str, Any],
    allowed: frozenset[str],
    field: str,
) -> None:
    if set(raw) - allowed:
        raise EnhancementValidationError(f"{field} contains unsupported fields")


def _require_fields(
    raw: dict[str, Any],
    required: frozenset[str],
    field: str,
) -> None:
    if required - set(raw):
        raise EnhancementValidationError(f"{field} is missing required fields")


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise EnhancementValidationError(f"{field} must be a string")
    return value


def _nonempty_string(value: object, field: str) -> str:
    result = _string(value, field).strip()
    if not result:
        raise EnhancementValidationError(f"{field} must be a non-empty string")
    return result


def _string_array(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EnhancementValidationError(f"{field} must be an array of strings")
    result: list[str] = []
    for item in value:
        if "\x00" in item:
            raise EnhancementValidationError(f"{field} cannot contain NUL")
        if allow_empty:
            result.append(item)
            continue
        stripped = item.strip()
        if not stripped:
            raise EnhancementValidationError(
                f"{field} cannot contain empty strings"
            )
        result.append(stripped)
    return tuple(result)


def _environment(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise EnhancementValidationError(
            "postprocessor.preset.environment must map strings to strings"
        )
    result: list[tuple[str, str]] = []
    for key, item in value.items():
        if not key or "=" in key or "\x00" in key or "\x00" in item:
            raise EnhancementValidationError(
                "postprocessor.preset.environment contains an invalid entry"
            )
        result.append((key, item))
    return tuple(sorted(result))


def _validate_no_nul(value: str, field: str) -> None:
    if "\x00" in value:
        raise EnhancementValidationError(f"{field} cannot contain NUL")

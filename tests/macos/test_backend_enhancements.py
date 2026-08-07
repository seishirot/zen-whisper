from __future__ import annotations

import logging
import os
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "macos/backend/src"
sys.path.insert(0, str(BACKEND_SRC))

import zen_whisper_mac_backend.enhancements as enhancements_module  # noqa: E402
import zen_whisper_mac_backend.adapters as adapters_module  # noqa: E402
import zen_whisper_mac_backend.service as service_module  # noqa: E402
from zen_whisper_mac_backend.adapters import (  # noqa: E402
    DummyAdapter,
    MlxWhisperAdapter,
)
from zen_whisper_mac_backend.enhancements import (  # noqa: E402
    EnhancementValidationError,
    apply_replacements,
    parse_postprocessor,
    parse_profile,
    process_transcript,
    recognition_hints,
)
from zen_whisper_mac_backend.protocol import (  # noqa: E402
    MAX_LINE_BYTES,
    encode_message,
)
from zen_whisper_mac_backend.service import (  # noqa: E402
    BackendService,
    ProgressDeliveryError,
)


@pytest.fixture(autouse=True)
def _use_verified_test_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapters_module,
        "_pinned_model_snapshot",
        lambda _engine_id, model_id: model_id,
    )


def _profile_payload() -> dict[str, object]:
    return {
        "id": "zen-whisper",
        "name": "Zen Whisper",
        "context": "Project context",
        "terms": [
            {
                "canonical": "ZenWhisper",
                "spoken": ["ゼンウィスパー", "ZenWhisper"],
                "replace_from": ["全ウィスパー"],
                "description": "Product name",
            },
            {
                "canonical": "MLX",
                "spoken": ["エムエルエックス"],
                "replace_from": ["M L X"],
                "description": "",
            },
        ],
    }


def _preset_payload(
    *,
    executable: str = sys.executable,
    arguments: list[str] | None = None,
    input_mode: str = "stdin",
    system_prompt: str = "",
    prompt_template: str = "{{transcript}}",
    timeout_sec: float = 3.0,
    preflight_executable: str | None = None,
    preflight_arguments: list[str] | None = None,
    preflight_failure_message: str = "",
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "test-cli",
        "display_name": "Test CLI",
        "executable": executable,
        "arguments": arguments or [],
        "preflight_arguments": preflight_arguments or [],
        "preflight_failure_message": preflight_failure_message,
        "input_mode": input_mode,
        "output_mode": "stdout",
        "timeout_sec": timeout_sec,
        "data_destination": "local",
        "system_prompt": system_prompt,
        "prompt_template": prompt_template,
        "environment": environment or {},
    }
    if preflight_executable is not None:
        payload["preflight_executable"] = preflight_executable
    return payload


def _selection(
    **preset_overrides: object,
):
    return parse_postprocessor(
        {
            "mode": "preset",
            "preset": _preset_payload(**preset_overrides),
        }
    )


def _audio_file(tmp_path: Path) -> Path:
    audio = tmp_path / "sample.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    return audio


def _transcribe_request(audio: Path) -> dict[str, object]:
    return {
        "type": "transcribe",
        "request_id": "enhanced-transcribe",
        "audio_path": str(audio),
        "engine": "mlx-whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "ja",
    }


def _assert_nonnegative_elapsed(metadata: dict[str, object]) -> None:
    elapsed = metadata.get("elapsed_sec")
    assert isinstance(elapsed, float)
    assert elapsed >= 0


def test_profile_builds_context_and_deduplicated_hotwords() -> None:
    profile = parse_profile(_profile_payload())

    hints = recognition_hints(profile)

    assert hints.hotwords == (
        "ZenWhisper",
        "ゼンウィスパー",
        "MLX",
        "エムエルエックス",
    )
    assert "全ウィスパー" not in hints.hotwords
    assert "Project context" in hints.context
    assert "Product name" in hints.context
    assert "全ウィスパー" in hints.context
    assert hints.initial_prompt == hints.context


def test_profile_rejects_non_data_fields() -> None:
    payload = _profile_payload()
    payload["command"] = "private executable"

    with pytest.raises(
        EnhancementValidationError,
        match="profile contains unsupported fields",
    ):
        parse_profile(payload)


def test_dictionary_replacement_is_longest_first_single_pass_and_not_spoken() -> None:
    profile = parse_profile(
        {
            "id": "replace",
            "name": "Replace",
            "context": "",
            "terms": [
                {
                    "canonical": "LONG",
                    "spoken": ["spoken-only"],
                    "replace_from": ["abc", "ab"],
                    "description": "",
                },
                {
                    "canonical": "abc",
                    "spoken": [],
                    "replace_from": ["source"],
                    "description": "",
                },
            ],
        }
    )

    assert apply_replacements("abc ab source spoken-only", profile) == (
        "LONG LONG abc spoken-only"
    )


def test_off_and_dictionary_results_have_stable_metadata() -> None:
    profile = parse_profile(_profile_payload())

    raw = process_transcript(
        "全ウィスパー",
        profile,
        parse_postprocessor(None),
        "ja",
    )
    dictionary = process_transcript(
        "全ウィスパー",
        profile,
        parse_postprocessor({"mode": "dictionary"}),
        "ja",
    )

    assert raw.text == "全ウィスパー"
    assert raw.response_metadata() == {
        "outcome": "raw",
        "cli_selected": False,
        "succeeded": True,
        "applied": False,
    }
    assert dictionary.text == "ZenWhisper"
    assert dictionary.response_metadata() == {
        "outcome": "dictionary",
        "cli_selected": False,
        "succeeded": True,
        "applied": True,
    }


@pytest.mark.parametrize(
    ("input_mode", "arguments", "match"),
    [
        ("stdin", ["{{transcript}}"], "stdin arguments"),
        ("argument", ["static"], r"requires \{\{prompt\}\}"),
    ],
)
def test_cli_mode_rejects_unsafe_placeholder_placement(
    input_mode: str,
    arguments: list[str],
    match: str,
) -> None:
    with pytest.raises(EnhancementValidationError, match=match):
        _selection(input_mode=input_mode, arguments=arguments)


@pytest.mark.parametrize(
    ("arguments", "system_prompt", "prompt_template", "match"),
    [
        ([], "Dedicated", "{{transcript}}", "system_prompt_file"),
        (
            ["{{system_prompt_file}}"],
            "",
            "{{transcript}}",
            "system_prompt",
        ),
        (
            ["{{system_prompt_file}}"],
            "Dedicated {{language}}",
            "{{transcript}}",
            "cannot contain placeholders",
        ),
        (
            [],
            "",
            "{{system_prompt_file}} {{transcript}}",
            "prompt_template",
        ),
    ],
)
def test_cli_system_prompt_requires_a_paired_command_only_file_placeholder(
    arguments: list[str],
    system_prompt: str,
    prompt_template: str,
    match: str,
) -> None:
    with pytest.raises(EnhancementValidationError, match=match):
        _selection(
            arguments=arguments,
            system_prompt=system_prompt,
            prompt_template=prompt_template,
        )


def test_cli_rejects_dynamic_executables_and_preflight() -> None:
    with pytest.raises(EnhancementValidationError, match="executable cannot"):
        _selection(executable="{{transcript}}")
    with pytest.raises(EnhancementValidationError, match="executable cannot"):
        _selection(executable="{{unsupported-value}}")
    with pytest.raises(EnhancementValidationError, match="preflight cannot"):
        _selection(
            preflight_executable=sys.executable,
            preflight_arguments=["{{unsupported-value}}"],
        )


def test_preset_defaults_destination_and_caps_timeout() -> None:
    preset = _preset_payload()
    preset.pop("data_destination")

    selection = parse_postprocessor(
        {"mode": "preset", "preset": preset}
    )

    assert selection.preset is not None
    assert selection.preset.data_destination == "unknown"
    with pytest.raises(EnhancementValidationError, match="at most 300"):
        _selection(timeout_sec=300.01)


def test_stdin_cli_receives_prompt_and_uses_empty_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        enhancements_module.secrets,
        "token_hex",
        lambda _size: "b" * 32,
    )
    profile = parse_profile(_profile_payload())
    selection = _selection(
        arguments=[
            "-c",
            (
                "import os,sys; data=sys.stdin.read(); "
                "sys.stdout.write(f'{len(os.listdir(\".\"))}|' + data)"
            ),
        ],
        prompt_template=(
            "{{boundary}}|{{profile_name}}|{{language}}|{{context}}|"
            "{{terms}}|{{transcript}}"
        ),
    )

    result = process_transcript(
        "全ウィスパー",
        profile,
        selection,
        "ja",
    )

    assert result.succeeded is True
    assert result.outcome == "cli"
    assert result.cli_selected is True
    assert result.text.startswith(f"0|{'b' * 32}|Zen Whisper|ja|Project context|")
    assert result.text.endswith("|ZenWhisper")


def test_cli_uses_private_per_run_system_prompt_file() -> None:
    profile = parse_profile(_profile_payload())
    selection = _selection(
        arguments=[
            "-c",
            (
                "import os,pathlib,stat,sys; path=pathlib.Path(sys.argv[1]); "
                "mode=stat.S_IMODE(path.stat().st_mode); "
                "sys.stdout.write("
                "path.read_text(encoding='utf-8') + '|' + oct(mode) + '|' "
                "+ str(path) + '|' + sys.stdin.read())"
            ),
            "{{system_prompt_file}}",
        ],
        system_prompt="Dedicated",
    )

    result = process_transcript(
        "全ウィスパー",
        profile,
        selection,
        "ja",
    )

    assert result.succeeded is True
    system_prompt, mode, path, transcript = result.text.split("|", 3)
    assert system_prompt == "Dedicated"
    assert mode == "0o600"
    assert transcript == "ZenWhisper"
    assert not Path(path).exists()


def test_cli_uses_a_fresh_boundary_for_each_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundaries = iter(("1" * 32, "2" * 32))
    requested_sizes: list[int] = []

    def fake_token_hex(size: int) -> str:
        requested_sizes.append(size)
        return next(boundaries)

    monkeypatch.setattr(
        enhancements_module.secrets,
        "token_hex",
        fake_token_hex,
    )
    selection = _selection(
        arguments=[
            "-c",
            "import sys; sys.stdout.write(sys.stdin.read())",
        ],
        prompt_template="{{boundary}}|{{transcript}}",
    )

    first = process_transcript("first", None, selection, "ja")
    second = process_transcript("second", None, selection, "ja")

    assert requested_sizes == [16, 16]
    assert first.text == ("1" * 32) + "|first"
    assert second.text == ("2" * 32) + "|second"


def test_argument_cli_receives_rendered_prompt_without_a_shell() -> None:
    selection = _selection(
        input_mode="argument",
        arguments=[
            "-c",
            "import sys; sys.stdout.write(sys.argv[1])",
            "{{prompt}}",
        ],
        prompt_template="language={{language}} transcript={{transcript}}",
    )

    result = process_transcript("hello", None, selection, "en")

    assert result.text == "language=en transcript=hello"
    assert result.response_metadata() == {
        "outcome": "cli",
        "cli_selected": True,
        "succeeded": True,
        "applied": True,
    }


def test_cli_uses_dedicated_path_without_mutating_backend_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "zen-whisper-test-cli"
    executable.write_text("#!/bin/sh\nprintf 'dedicated path'\n", encoding="utf-8")
    executable.chmod(0o700)
    parent_path = "/backend/restricted/path"
    monkeypatch.setenv("PATH", parent_path)
    monkeypatch.setenv("ZEN_WHISPER_CLI_PATH", str(tmp_path))
    selection = _selection(executable=executable.name)

    result = process_transcript("input", None, selection, "ja")

    assert result.text == "dedicated path"
    assert result.succeeded is True
    assert os.environ["PATH"] == parent_path


def test_cli_output_control_runs_are_flattened_without_logging_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        logging.INFO,
        logger="zen_whisper_mac_backend.enhancements",
    )
    private_one = "PRIVATE SUCCESS ONE"
    private_two = "PRIVATE SUCCESS TWO"
    private_three = "PRIVATE SUCCESS THREE"
    selection = _selection(
        arguments=[
            "-c",
            (
                "import sys; "
                f"sys.stdout.write({private_one!r} + "
                r"'\n\x00\x1f\x7f\x85' + "
                f"{private_two!r} + "
                r"'\u2028\u2029' + "
                f"{private_three!r})"
            ),
        ],
    )

    result = process_transcript("input", None, selection, "ja")

    assert result.text == (
        f"{private_one} {private_two} {private_three}"
    )
    assert result.succeeded is True
    assert private_one not in caplog.text
    assert private_two not in caplog.text
    assert private_three not in caplog.text


def test_control_only_cli_output_falls_back_to_dictionary() -> None:
    profile = parse_profile(_profile_payload())
    selection = _selection(
        arguments=[
            "-c",
            r"import sys; sys.stdout.write('\n\x00\u2028')",
        ],
    )

    result = process_transcript("全ウィスパー", profile, selection, "ja")

    assert result.text == "ZenWhisper"
    assert result.outcome == "cli_fallback"
    assert result.cli_selected is True
    assert result.succeeded is False
    assert result.applied is True
    assert result.warning_code == "CLI_OUTPUT_EMPTY"
    assert result.error == "Test CLI の出力が空でした"


def test_nonzero_cli_falls_back_without_logging_sensitive_streams(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    profile = parse_profile(_profile_payload())
    private_input = "全ウィスパー PRIVATE TRANSCRIPT"
    private_stdout = "PRIVATE STDOUT"
    private_stderr = "PRIVATE STDERR"
    private_environment = "PRIVATE ENVIRONMENT"
    selection = _selection(
        arguments=[
            "-c",
            (
                "import sys; "
                f"sys.stdout.write({private_stdout!r}); "
                f"sys.stderr.write({private_stderr!r}); "
                "raise SystemExit(7)"
            ),
        ],
        environment={"PRIVATE_TEST_VALUE": private_environment},
    )

    result = process_transcript(private_input, profile, selection, "ja")

    assert result.text == "ZenWhisper PRIVATE TRANSCRIPT"
    assert result.warning_code == "CLI_FAILED"
    assert result.succeeded is False
    assert private_input not in caplog.text
    assert "ZenWhisper PRIVATE TRANSCRIPT" not in caplog.text
    assert private_stdout not in caplog.text
    assert private_stderr not in caplog.text
    assert private_environment not in caplog.text
    assert sys.executable not in caplog.text


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_cli_streams_are_bounded_without_logging_their_bodies(
    stream_name: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(enhancements_module, "_MAX_CLI_STREAM_BYTES", 64)
    private_body = f"PRIVATE {stream_name.upper()} BODY"
    script = (
        "import sys; "
        f"sys.{stream_name}.write({private_body!r} * 100); "
        "sys.stdout.write('otherwise valid')"
    )
    selection = _selection(arguments=["-c", script])

    result = process_transcript("fallback", None, selection, "ja")

    assert result.text == "fallback"
    assert result.warning_code == "CLI_OUTPUT_TOO_LARGE"
    assert result.error == "Test CLI の出力が大きすぎます"
    assert private_body not in caplog.text


def test_cli_stdout_cap_keeps_successful_result_inside_transport_budget(
    tmp_path: Path,
) -> None:
    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={"mlx-whisper": DummyAdapter("mlx-whisper", "fallback")}
    )
    safe_size = enhancements_module._MAX_CLI_STREAM_BYTES  # noqa: SLF001
    request = _transcribe_request(audio)
    request["postprocessor"] = {
        "mode": "preset",
        "preset": _preset_payload(
            arguments=[
                "-c",
                (
                    "import sys; "
                    f"sys.stdout.buffer.write(b'\\xff' * {safe_size})"
                ),
            ],
        ),
    }
    overflow = _transcribe_request(audio)
    overflow["request_id"] = "overflow-transcribe"
    overflow["postprocessor"] = {
        "mode": "preset",
        "preset": _preset_payload(
            arguments=[
                "-c",
                (
                    "import sys; "
                    f"sys.stdout.buffer.write(b'x' * {safe_size + 1})"
                ),
            ],
        ),
    }

    try:
        successful_progress: list[dict[str, object]] = []
        fallback_progress: list[dict[str, object]] = []
        successful = service.handle(
            request,
            on_progress=successful_progress.append,
        )
        fallback = service.handle(
            overflow,
            on_progress=fallback_progress.append,
        )
    finally:
        service.close()

    encoded = encode_message(successful)
    assert successful["type"] == "result"
    assert successful["enhancement"]["outcome"] == "cli"
    _assert_nonnegative_elapsed(successful["enhancement"])
    assert successful_progress == [
        {
            "type": "progress",
            "request_id": "enhanced-transcribe",
            "stage": "postprocessing",
        }
    ]
    assert len(encoded) < MAX_LINE_BYTES
    assert fallback["type"] == "result"
    assert fallback["text"] == "fallback"
    assert fallback["enhancement"]["outcome"] == "cli_fallback"
    _assert_nonnegative_elapsed(fallback["enhancement"])
    assert fallback_progress == [
        {
            "type": "progress",
            "request_id": "overflow-transcribe",
            "stage": "postprocessing",
        }
    ]
    assert (
        fallback["enhancement"]["warning_code"]
        == "CLI_OUTPUT_TOO_LARGE"
    )


def test_missing_cli_and_failed_preflight_return_public_fallbacks() -> None:
    profile = parse_profile(_profile_payload())
    missing = process_transcript(
        "全ウィスパー",
        profile,
        _selection(executable="/definitely/missing/zen-whisper-command"),
        "ja",
    )
    preflight = process_transcript(
        "全ウィスパー",
        profile,
        _selection(
            arguments=["-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            preflight_executable=sys.executable,
            preflight_arguments=["-c", "raise SystemExit(1)"],
            preflight_failure_message="CLI is not configured",
        ),
        "ja",
    )

    assert missing.text == "ZenWhisper"
    assert missing.warning_code == "EXECUTABLE_NOT_FOUND"
    assert "/definitely/missing" not in (missing.error or "")
    assert preflight.text == "ZenWhisper"
    assert preflight.warning_code == "PREFLIGHT_FAILED"
    assert preflight.error == "CLI is not configured"


def test_cli_timeout_terminates_with_a_bounded_wait() -> None:
    selection = _selection(
        arguments=["-c", "import time; time.sleep(30)"],
        timeout_sec=0.05,
    )

    started = time.monotonic()
    result = process_transcript("fallback", None, selection, "ja")
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert result.text == "fallback"
    assert result.warning_code == "CLI_TIMEOUT"


def test_preflight_timeout_is_bounded_and_skips_main_command() -> None:
    selection = _selection(
        arguments=[
            "-c",
            "raise AssertionError('main command must not run')",
        ],
        preflight_executable=sys.executable,
        preflight_arguments=["-c", "import time; time.sleep(30)"],
        timeout_sec=0.05,
    )

    started = time.monotonic()
    result = process_transcript("fallback", None, selection, "ja")
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert result.text == "fallback"
    assert result.warning_code == "PREFLIGHT_TIMEOUT"


def test_cli_infrastructure_failure_still_returns_dictionary_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = parse_profile(_profile_payload())
    selection = _selection()

    class BrokenTemporaryDirectory:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise OSError("private temp path")

    monkeypatch.setattr(
        enhancements_module.tempfile,
        "TemporaryDirectory",
        BrokenTemporaryDirectory,
    )

    result = process_transcript("全ウィスパー", profile, selection, "ja")

    assert result.text == "ZenWhisper"
    assert result.outcome == "cli_fallback"
    assert result.warning_code == "CLI_FAILED"
    assert "private temp path" not in (result.error or "")


def test_legacy_transcribe_request_stays_raw_and_uses_three_argument_adapter(
    tmp_path: Path,
) -> None:
    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={"mlx-whisper": DummyAdapter("mlx-whisper", " raw text ")}
    )
    progress: list[dict[str, object]] = []
    try:
        result = service.handle(
            _transcribe_request(audio),
            on_progress=progress.append,
        )
    finally:
        service.close()

    assert result["type"] == "result"
    assert result["text"] == "raw text"
    assert result["hints_applied"] is False
    assert {
        key: value
        for key, value in result["enhancement"].items()
        if key != "elapsed_sec"
    } == {
        "outcome": "raw",
        "cli_selected": False,
        "succeeded": True,
        "applied": False,
    }
    _assert_nonnegative_elapsed(result["enhancement"])
    assert progress == []


def test_service_applies_mlx_profile_hints_and_dictionary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_transcribe(audio: object, **kwargs: object) -> dict[str, str]:
        calls.append(dict(kwargs))
        return {"text": "全ウィスパー"}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )
    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={"mlx-whisper": MlxWhisperAdapter()}
    )
    request = _transcribe_request(audio)
    request["profile"] = _profile_payload()
    request["postprocessor"] = {"mode": "dictionary"}
    progress: list[dict[str, object]] = []
    try:
        result = service.handle(request, on_progress=progress.append)
    finally:
        service.close()

    assert result["type"] == "result"
    assert result["text"] == "ZenWhisper"
    assert result["hints_applied"] is True
    assert {
        key: value
        for key, value in result["enhancement"].items()
        if key != "elapsed_sec"
    } == {
        "outcome": "dictionary",
        "cli_selected": False,
        "succeeded": True,
        "applied": True,
    }
    _assert_nonnegative_elapsed(result["enhancement"])
    assert progress == [
        {
            "type": "progress",
            "request_id": "enhanced-transcribe",
            "stage": "postprocessing",
        }
    ]
    assert len(calls) == 2
    assert "initial_prompt" not in calls[0]
    assert calls[1]["initial_prompt"] == recognition_hints(
        parse_profile(_profile_payload())
    ).context


def test_postprocessor_timing_excludes_progress_delivery_delay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = {"now": 10.0}
    monkeypatch.setattr(
        service_module.time,
        "monotonic",
        lambda: clock["now"],
    )
    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={
            "mlx-whisper": DummyAdapter(
                "mlx-whisper",
                "全ウィスパー",
            )
        }
    )
    request = _transcribe_request(audio)
    request["profile"] = _profile_payload()
    request["postprocessor"] = {"mode": "dictionary"}

    def delayed_progress(_: dict[str, object]) -> None:
        clock["now"] += 100

    try:
        result = service.handle(
            request,
            on_progress=delayed_progress,
        )
    finally:
        service.close()

    assert result["enhancement"]["elapsed_sec"] == 0.0


def test_service_propagates_progress_delivery_failure(
    tmp_path: Path,
) -> None:
    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={"mlx-whisper": DummyAdapter("mlx-whisper", "hello")}
    )
    request = _transcribe_request(audio)
    request["postprocessor"] = {"mode": "dictionary"}

    def fail_progress(_: dict[str, object]) -> None:
        raise ProgressDeliveryError

    try:
        with pytest.raises(ProgressDeliveryError):
            service.handle(request, on_progress=fail_progress)
    finally:
        service.close()


def test_service_postprocessor_log_records_safe_timing_metadata_only(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    private_transcript = "PRIVATE TRANSCRIPT CONTENT"
    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={
            "mlx-whisper": DummyAdapter(
                "mlx-whisper",
                private_transcript,
            )
        }
    )
    request = _transcribe_request(audio)
    request["postprocessor"] = {
        "mode": "preset",
        "preset": _preset_payload(
            arguments=[
                "-c",
                "import sys; sys.stdout.write('corrected')",
            ],
        ),
    }
    caplog.set_level(logging.INFO)

    try:
        result = service.handle(request)
    finally:
        service.close()

    assert result["enhancement"]["outcome"] == "cli"
    assert "Postprocessor completed: id=test-cli outcome=cli" in caplog.text
    assert "succeeded=True applied=True elapsed_sec=" in caplog.text
    assert private_transcript not in caplog.text


def test_service_fallback_log_never_includes_private_cli_content(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    private_transcript = "PRIVATE FALLBACK TRANSCRIPT"
    private_stderr = "PRIVATE CLI STDERR"
    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={
            "mlx-whisper": DummyAdapter(
                "mlx-whisper",
                private_transcript,
            )
        }
    )
    request = _transcribe_request(audio)
    request["postprocessor"] = {
        "mode": "preset",
        "preset": _preset_payload(
            arguments=[
                "-c",
                (
                    "import sys; "
                    f"sys.stderr.write({private_stderr!r}); "
                    "raise SystemExit(7)"
                ),
            ],
        ),
    }
    caplog.set_level(logging.INFO)

    try:
        result = service.handle(request)
    finally:
        service.close()

    assert result["enhancement"]["outcome"] == "cli_fallback"
    assert "Postprocessor completed: id=test-cli outcome=cli_fallback" in caplog.text
    assert "succeeded=False applied=True elapsed_sec=" in caplog.text
    assert private_transcript not in caplog.text
    assert private_stderr not in caplog.text


def test_hint_unsupported_engine_still_applies_dictionary(
    tmp_path: Path,
) -> None:
    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={
            "mlx-qwen3-asr": DummyAdapter(
                "mlx-qwen3-asr",
                "全ウィスパー",
            )
        }
    )
    request = _transcribe_request(audio)
    request.update(
        {
            "engine": "mlx-qwen3-asr",
            "model": "mlx-community/Qwen3-ASR-0.6B-8bit",
            "profile": _profile_payload(),
            "postprocessor": {"mode": "dictionary"},
        }
    )

    try:
        result = service.handle(request)
    finally:
        service.close()

    assert result["type"] == "result"
    assert result["text"] == "ZenWhisper"
    assert result["hints_applied"] is False
    assert result["enhancement"]["outcome"] == "dictionary"


def test_hinted_adapter_failure_never_logs_profile_contents(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    def fake_transcribe(audio: object, **kwargs: object) -> dict[str, str]:
        raise RuntimeError(f"dependency echoed {kwargs.get('initial_prompt')}")

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )
    audio = _audio_file(tmp_path)
    adapter = MlxWhisperAdapter()
    adapter._loaded_model = "mlx-community/whisper-large-v3-turbo"  # noqa: SLF001
    adapter._loaded_model_path = "verified-model"  # noqa: SLF001
    service = BackendService(adapters={"mlx-whisper": adapter})
    request = _transcribe_request(audio)
    request["profile"] = _profile_payload()

    with caplog.at_level(
        logging.INFO,
        logger="zen_whisper_mac_backend.service",
    ):
        try:
            result = service.handle(request)
        finally:
            service.close()

    assert result["type"] == "error"
    assert result["message"] == "MLX Whisper transcribe failed"
    assert "Project context" not in caplog.text
    assert "Product name" not in caplog.text
    assert "全ウィスパー" not in caplog.text
    assert "ゼンウィスパー" not in caplog.text


def test_invalid_enhancement_is_rejected_before_asr(tmp_path: Path) -> None:
    class ExplodingAdapter(DummyAdapter):
        def transcribe(
            self,
            audio_path: Path,
            model_id: str,
            language: str,
        ) -> str:
            raise AssertionError("ASR must not run")

    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={"mlx-whisper": ExplodingAdapter("mlx-whisper", "unused")}
    )
    request = _transcribe_request(audio)
    request["profile"] = {**_profile_payload(), "command": "private command"}
    try:
        result = service.handle(request)
    finally:
        service.close()

    assert result["type"] == "error"
    assert result["code"] == "INVALID_REQUEST"
    assert result["message"] == "profile contains unsupported fields"


def test_large_enhancement_requests_are_rejected_before_asr(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    class ExplodingAdapter(DummyAdapter):
        def transcribe(
            self,
            audio_path: Path,
            model_id: str,
            language: str,
        ) -> str:
            raise AssertionError("ASR must not run")

    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={"mlx-whisper": ExplodingAdapter("mlx-whisper", "unused")}
    )
    private_padding = "PRIVATE OVERSIZED PROFILE "
    oversized_profile = _transcribe_request(audio)
    oversized_profile["request_id"] = "oversized-profile"
    oversized_profile["profile"] = {
        **_profile_payload(),
        "context": private_padding
        + "x" * enhancements_module._MAX_ENHANCEMENT_PAYLOAD_BYTES,  # noqa: SLF001
    }
    oversized_request = _transcribe_request(audio)
    oversized_request["request_id"] = "oversized-request"
    oversized_request["padding"] = (
        private_padding
        + "x" * enhancements_module._MAX_TRANSCRIBE_REQUEST_BYTES  # noqa: SLF001
    )
    oversized_request_id = _transcribe_request(audio)
    oversized_request_id["request_id"] = (
        "r" * (enhancements_module._MAX_REQUEST_ID_BYTES + 1)  # noqa: SLF001
    )

    with caplog.at_level(
        logging.INFO,
        logger="zen_whisper_mac_backend.service",
    ):
        try:
            profile_result = service.handle(oversized_profile)
            request_result = service.handle(oversized_request)
            request_id_result = service.handle(oversized_request_id)
        finally:
            service.close()

    assert len(encode_message(oversized_profile)) < MAX_LINE_BYTES
    assert len(encode_message(oversized_request)) < MAX_LINE_BYTES
    assert profile_result["code"] == "INVALID_REQUEST"
    assert profile_result["message"] == "enhancement payload is too large"
    assert request_result["code"] == "INVALID_REQUEST"
    assert request_result["message"] == "transcribe request is too large"
    assert request_id_result["code"] == "INVALID_REQUEST"
    assert (
        request_id_result["message"]
        == "transcribe request_id is too large"
    )
    assert private_padding not in caplog.text


def test_profile_field_count_is_rejected_before_asr(tmp_path: Path) -> None:
    class ExplodingAdapter(DummyAdapter):
        def transcribe(
            self,
            audio_path: Path,
            model_id: str,
            language: str,
        ) -> str:
            raise AssertionError("ASR must not run")

    audio = _audio_file(tmp_path)
    service = BackendService(
        adapters={"mlx-whisper": ExplodingAdapter("mlx-whisper", "unused")}
    )
    request = _transcribe_request(audio)
    request["profile"] = {
        "id": "too-many-terms",
        "name": "Too Many",
        "context": "",
        "terms": [
            {
                "canonical": f"term-{index}",
                "spoken": [],
                "replace_from": [],
                "description": "",
            }
            for index in range(
                enhancements_module._MAX_PROFILE_TERMS + 1  # noqa: SLF001
            )
        ],
    }

    try:
        result = service.handle(request)
    finally:
        service.close()

    assert len(encode_message(request)) < MAX_LINE_BYTES
    assert result["type"] == "error"
    assert result["code"] == "INVALID_REQUEST"
    assert result["message"] == "profile contains too many terms"


def test_cli_postprocessing_remains_inside_backend_busy_guard(
    tmp_path: Path,
) -> None:
    audio = _audio_file(tmp_path)
    marker = tmp_path / "cli-started"
    service = BackendService(
        adapters={"mlx-whisper": DummyAdapter("mlx-whisper", "hello")}
    )
    request = _transcribe_request(audio)
    request["postprocessor"] = {
        "mode": "preset",
        "preset": _preset_payload(
            arguments=[
                "-c",
                (
                    "import pathlib,sys,time; "
                    f"pathlib.Path({str(marker)!r}).write_text('ready'); "
                    "time.sleep(0.3); sys.stdout.write(sys.stdin.read())"
                ),
            ],
        ),
    }
    first_result: dict[str, object] = {}

    thread = threading.Thread(
        target=lambda: first_result.update(service.handle(request)),
    )
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()

        busy = service.handle(
            {
                "type": "preload",
                "request_id": "busy-during-cli",
                "engine": "mlx-whisper",
                "model": "mlx-community/whisper-large-v3-turbo",
                "language": "ja",
            }
        )
    finally:
        thread.join(timeout=3)
        service.close()

    assert busy["type"] == "error"
    assert busy["code"] == "BACKEND_BUSY"
    assert first_result["type"] == "result"
    assert first_result["text"] == "hello"


@pytest.mark.parametrize("phase", ["preflight", "main"])
def test_shutdown_terminates_active_postprocessor_process_group(
    phase: str,
    tmp_path: Path,
) -> None:
    audio = _audio_file(tmp_path)
    started_marker = tmp_path / f"{phase}-started"
    delayed_marker = tmp_path / f"{phase}-orphaned"
    child_script = (
        "import pathlib,time; time.sleep(0.8); "
        f"pathlib.Path({str(delayed_marker)!r}).write_text('orphaned')"
    )
    blocking_script = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        f"pathlib.Path({str(started_marker)!r}).write_text('ready'); "
        "time.sleep(30)"
    )
    preset_arguments = [
        "-c",
        (
            blocking_script
            if phase == "main"
            else "import sys; sys.stdout.write(sys.stdin.read())"
        ),
    ]
    preset_options: dict[str, object] = {"arguments": preset_arguments}
    if phase == "preflight":
        preset_options.update(
            {
                "preflight_executable": sys.executable,
                "preflight_arguments": ["-c", blocking_script],
            }
        )

    service = BackendService(
        adapters={"mlx-whisper": DummyAdapter("mlx-whisper", "hello")}
    )
    request = _transcribe_request(audio)
    request["postprocessor"] = {
        "mode": "preset",
        "preset": _preset_payload(**preset_options),
    }
    result: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result.update(service.handle(request)),
    )
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not started_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started_marker.exists()

        shutdown_started = time.monotonic()
        service.shutdown(wait=False)
        shutdown_elapsed = time.monotonic() - shutdown_started
        thread.join(timeout=3)

        assert shutdown_elapsed < 3
        assert not thread.is_alive()
        marker_deadline = time.monotonic() + 1.2
        while time.monotonic() < marker_deadline:
            assert not delayed_marker.exists()
            time.sleep(0.05)
    finally:
        service.shutdown(wait=False)
        thread.join(timeout=3)

    assert result["type"] == "result"
    assert result["enhancement"]["outcome"] == "cli_fallback"

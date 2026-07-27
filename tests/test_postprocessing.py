"""Generic shell-free command postprocessor tests."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import src.postprocessing as postprocessing
from src.platform import is_windows
from src.postprocessing import (
    DATA_DESTINATION_LOCAL,
    DATA_DESTINATION_REMOTE,
    DATA_DESTINATION_UNKNOWN,
    PostprocessorPreset,
    _build_invocation,
    load_local_postprocessor_ids,
    load_local_postprocessors,
    load_postprocessors,
    process_transcript,
    run_postprocessor,
    save_postprocessor,
)
from src.profiles import Profile, ProfileTerm
from src.toml_storage import StaleFileError, file_fingerprint


def _profile() -> Profile:
    return Profile(
        profile_id="coding",
        name="Coding",
        context="Project context",
        terms=(
            ProfileTerm(
                canonical="ZenWhisper",
                spoken=("ゼンウィスパー",),
                replace_from=("全ウィスパー",),
            ),
        ),
    )


def test_bundled_ollama_preset_is_local_and_has_no_pull_preflight(tmp_path):
    presets = load_postprocessors(user_path=tmp_path / "missing.toml")

    ollama = presets["ollama"]
    assert ollama.data_destination == DATA_DESTINATION_LOCAL
    assert ollama.preflight_command == "ollama show qwen3.5:4b"
    assert "ollama pull" not in ollama.command
    assert ollama.environment["OLLAMA_HOST"] == "127.0.0.1:11434"


def test_bundled_claude_preset_is_remote_stateless_and_toolless(tmp_path):
    presets = load_postprocessors(user_path=tmp_path / "missing.toml")

    claude = presets["claude"]
    argv, prompt = _build_invocation(
        claude,
        "全ウィスパー",
        _profile(),
        "ja",
    )

    assert claude.data_destination == DATA_DESTINATION_REMOTE
    assert claude.input_mode == "stdin"
    assert claude.preflight_command == "claude --version"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--effort" not in argv
    assert "--safe-mode" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "{{transcript}}" not in prompt
    assert "全ウィスパー" in prompt


def test_bundled_codex_preset_restores_hardened_historical_default(tmp_path):
    presets = load_postprocessors(user_path=tmp_path / "missing.toml")

    codex = presets["codex"]
    argv, prompt = _build_invocation(
        codex,
        "全ウィスパー",
        _profile(),
        "ja",
    )

    assert codex.display_name == "Codex 校正"
    assert codex.data_destination == DATA_DESTINATION_REMOTE
    assert codex.input_mode == "stdin"
    assert codex.timeout_sec == 30
    assert argv == [
        "codex",
        "exec",
        "--model",
        "gpt-5.6-luna",
        "-c",
        "model_reasoning_effort=low",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-c",
        "project_doc_max_bytes=0",
        "-",
    ]
    assert "{{transcript}}" not in prompt
    assert "全ウィスパー" in prompt


def test_native_bundled_catalog_matches_python_desktop_defaults(tmp_path):
    root = Path(__file__).resolve().parents[1]
    native = json.loads(
        (
            root
            / "macos"
            / "ZenWhisper"
            / "ZenWhisper"
            / "Resources"
            / "postprocessors.default.json"
        ).read_text(encoding="utf-8")
    )["postprocessors"]
    desktop = load_postprocessors(user_path=tmp_path / "missing.toml")

    assert set(native) == set(desktop) == {"codex", "claude", "ollama"}
    for preset_id, desktop_preset in desktop.items():
        native_preset = native[preset_id]
        command = postprocessing.split_command(desktop_preset.command)
        preflight = postprocessing.split_command(
            desktop_preset.preflight_command
        )

        assert native_preset["display_name"] == desktop_preset.display_name
        assert native_preset["executable"] == command[0]
        assert native_preset["arguments"] == command[1:]
        assert native_preset["preflight_executable"] == (
            preflight[0] if preflight else ""
        )
        assert native_preset["preflight_arguments"] == (
            preflight[1:] if preflight else []
        )
        assert (
            native_preset["preflight_failure_message"]
            == desktop_preset.preflight_failure_message
        )
        assert native_preset["input_mode"] == desktop_preset.input_mode
        assert (
            native_preset["data_destination"]
            == desktop_preset.data_destination
        )
        assert native_preset["timeout_sec"] == desktop_preset.timeout_sec
        assert (
            native_preset["prompt_template"]
            == desktop_preset.prompt_template
        )
        assert native_preset["environment"] == desktop_preset.environment


def test_user_can_define_arbitrary_argument_cli(tmp_path):
    bundled = tmp_path / "bundled.toml"
    bundled.write_text("", encoding="utf-8")
    local = tmp_path / "postprocessors.toml"
    local.write_text(
        """
[postprocessors.custom]
display_name = "Custom"
command = 'custom-cli --prompt "{{prompt}}"'
input_mode = "argument"
prompt_template = "Fix: {{transcript}}"
""",
        encoding="utf-8",
    )

    preset = load_postprocessors(bundled, local)["custom"]
    argv, prompt = _build_invocation(
        preset,
        'hello" & calc',
        None,
        "ja",
    )

    assert argv == ["custom-cli", "--prompt", 'Fix: hello" & calc']
    assert prompt == 'Fix: hello" & calc'


def test_unknown_placeholder_skips_only_invalid_preset(tmp_path):
    local = tmp_path / "postprocessors.toml"
    local.write_text(
        """
[postprocessors.good]
command = "good"
prompt_template = "{{transcript}}"

[postprocessors.bad]
command = "bad"
prompt_template = "{{missing}} {{transcript}}"
""",
        encoding="utf-8",
    )

    presets = load_postprocessors(tmp_path / "missing.toml", local)

    assert list(presets) == ["good"]


def test_stdin_command_rejects_all_dynamic_placeholders(tmp_path):
    local = tmp_path / "postprocessors.toml"
    local.write_text(
        """
[postprocessors.bad]
command = 'tool --label "{{transcript}}"'
input_mode = "stdin"
prompt_template = "{{transcript}}"
""",
        encoding="utf-8",
    )

    presets = load_postprocessors(tmp_path / "missing.toml", local)

    assert presets == {}


def test_argument_command_rejects_placeholder_executable(tmp_path):
    local = tmp_path / "postprocessors.toml"
    local.write_text(
        """
[postprocessors.bad]
command = '"{{prompt}}" --run'
input_mode = "argument"
prompt_template = "{{transcript}}"
""",
        encoding="utf-8",
    )

    presets = load_postprocessors(tmp_path / "missing.toml", local)

    assert presets == {}


def test_replacing_bundled_command_resets_command_specific_security_fields(tmp_path):
    bundled = tmp_path / "bundled.toml"
    bundled.write_text(
        """
[postprocessors.local]
display_name = "Local"
command = "local run"
input_mode = "stdin"
data_destination = "local"
preflight_command = "local show"
preflight_failure_message = "missing"
environment = { LOCAL_ONLY = "1" }
prompt_template = "{{transcript}}"
""",
        encoding="utf-8",
    )
    local = tmp_path / "postprocessors.toml"
    local.write_text(
        """
[postprocessors.local]
command = "remote run"
""",
        encoding="utf-8",
    )

    preset = load_postprocessors(bundled, local)["local"]

    assert preset.command == "remote run"
    assert preset.data_destination == DATA_DESTINATION_UNKNOWN
    assert preset.preflight_command == ""
    assert preset.preflight_failure_message == ""
    assert preset.environment == {}


def test_replacing_bundled_environment_resets_inherited_destination(tmp_path):
    bundled = tmp_path / "bundled.toml"
    bundled.write_text(
        """
[postprocessors.ollama]
display_name = "Ollama"
command = "ollama run model"
data_destination = "local"
environment = { OLLAMA_HOST = "127.0.0.1:11434" }
prompt_template = "{{transcript}}"
""",
        encoding="utf-8",
    )
    local = tmp_path / "postprocessors.toml"
    local.write_text(
        """
[postprocessors.ollama]
environment = { OLLAMA_HOST = "remote.example:11434" }
""",
        encoding="utf-8",
    )

    preset = load_postprocessors(bundled, local)["ollama"]

    assert preset.environment["OLLAMA_HOST"] == "remote.example:11434"
    assert preset.data_destination == DATA_DESTINATION_UNKNOWN


def test_stdin_runner_uses_shell_false_and_empty_working_directory(monkeypatch):
    calls = []

    class FakeProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            assert kwargs["shell"] is False
            assert kwargs["stdin"] is subprocess.PIPE
            assert list(postprocessing.Path(kwargs["cwd"]).iterdir()) == []

        def communicate(self, input=None, timeout=None):
            assert input == "Fix secret transcript"
            return " corrected ", ""

    monkeypatch.setattr(postprocessing.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(postprocessing, "subprocess_run_options", lambda: {})
    preset = PostprocessorPreset(
        preset_id="fake",
        display_name="Fake",
        command="fake-cli --quiet",
        prompt_template="Fix {{transcript}}",
    )

    result = run_postprocessor(preset, "secret transcript", None, "ja")

    assert result.succeeded is True
    assert result.text == "corrected"
    assert calls[0][0] == ["fake-cli", "--quiet"]


def test_cli_output_controls_are_collapsed_to_one_safe_line(monkeypatch):
    class FakeProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            return (
                "\x00cmd1\r\ncmd2\ttext\u0085x\u2028y\u2029z"
                "\x1b[31m\x7f",
                "",
            )

    monkeypatch.setattr(postprocessing.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(postprocessing, "subprocess_run_options", lambda: {})
    preset = PostprocessorPreset(
        preset_id="fake",
        display_name="Fake",
        command="fake-cli",
    )

    result = run_postprocessor(preset, "raw", None, "ja")

    assert result.succeeded is True
    assert result.text == "cmd1 cmd2 text x y z [31m"
    assert not any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in result.text
    )
    assert "\u2028" not in result.text
    assert "\u2029" not in result.text


def test_control_only_cli_output_is_treated_as_empty(monkeypatch):
    class FakeProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            return "\r\n\t\x00\u0085\u2028\u2029", ""

    monkeypatch.setattr(postprocessing.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(postprocessing, "subprocess_run_options", lambda: {})
    preset = PostprocessorPreset(
        preset_id="fake",
        display_name="Fake",
        command="fake-cli",
    )

    result = run_postprocessor(preset, "raw", None, "ja")

    assert result.succeeded is False
    assert result.text == "raw"
    assert "出力が空" in result.error


def test_process_start_is_registered_before_dispatch_guard_releases(monkeypatch):
    events = []

    class FakeProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            events.append("popen")

        def communicate(self, input=None, timeout=None):
            events.append("communicate")
            return "corrected", ""

    @contextmanager
    def start_guard():
        events.append("guard-enter")
        yield True
        events.append("guard-exit")

    monkeypatch.setattr(postprocessing.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(postprocessing, "subprocess_run_options", lambda: {})
    preset = PostprocessorPreset(
        preset_id="guarded",
        display_name="Guarded",
        command="guarded-cli",
    )

    result = run_postprocessor(
        preset,
        "raw",
        None,
        "ja",
        start_guard=start_guard,
        on_process_started=lambda process: events.append("registered"),
        on_process_finished=lambda process: events.append("finished"),
    )

    assert result.succeeded is True
    assert events == [
        "guard-enter",
        "popen",
        "registered",
        "guard-exit",
        "communicate",
        "finished",
    ]


def test_preflight_is_registered_and_main_command_is_guarded_again(monkeypatch):
    events = []

    class FakeProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            self.argv = argv
            events.append(("popen", argv))

        def wait(self, timeout=None):
            events.append(("wait", self.argv))
            return 0

        def communicate(self, input=None, timeout=None):
            events.append(("communicate", self.argv))
            return "corrected", ""

    @contextmanager
    def start_guard():
        events.append(("guard-enter", None))
        yield True
        events.append(("guard-exit", None))

    monkeypatch.setattr(postprocessing.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(postprocessing, "subprocess_run_options", lambda: {})
    preset = PostprocessorPreset(
        preset_id="guarded-preflight",
        display_name="Guarded preflight",
        command="tool run",
        preflight_command="tool show",
    )

    result = run_postprocessor(
        preset,
        "raw",
        None,
        "ja",
        start_guard=start_guard,
        on_process_started=lambda process: events.append(
            ("started", process.argv)
        ),
        on_process_finished=lambda process: events.append(
            ("finished", process.argv)
        ),
    )

    assert result.succeeded is True
    assert events == [
        ("guard-enter", None),
        ("popen", ["tool", "show"]),
        ("started", ["tool", "show"]),
        ("guard-exit", None),
        ("wait", ["tool", "show"]),
        ("finished", ["tool", "show"]),
        ("guard-enter", None),
        ("popen", ["tool", "run"]),
        ("started", ["tool", "run"]),
        ("guard-exit", None),
        ("communicate", ["tool", "run"]),
        ("finished", ["tool", "run"]),
    ]


def test_finish_guard_discards_stale_success(monkeypatch):
    class FakeProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            return "stale result", ""

    @contextmanager
    def rejected_finish():
        yield False

    monkeypatch.setattr(postprocessing.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(postprocessing, "subprocess_run_options", lambda: {})
    preset = PostprocessorPreset(
        preset_id="stale",
        display_name="Stale",
        command="stale-cli",
    )

    result = run_postprocessor(
        preset,
        "raw",
        None,
        "ja",
        finish_guard=rejected_finish,
    )

    assert result.succeeded is False
    assert result.text == "raw"
    assert "結果を破棄" in result.error


def test_save_postprocessor_round_trips_local_preset(tmp_path):
    user_path = tmp_path / "postprocessors.toml"
    preset = PostprocessorPreset(
        preset_id="custom_local",
        display_name="Custom Local",
        command="custom-cli",
        timeout_sec=12.5,
        data_destination=DATA_DESTINATION_LOCAL,
        prompt_template="Fix: {{transcript}}",
        environment={"LOCAL_ONLY": "1"},
    )

    saved_path = save_postprocessor(preset, user_path)

    assert saved_path == user_path
    assert load_local_postprocessors(user_path) == {"custom_local": preset}
    assert list(tmp_path.glob(".postprocessors.toml.*.tmp")) == []


def test_save_postprocessor_rejects_unsafe_id(tmp_path):
    with pytest.raises(
        postprocessing.PostprocessorConfigError,
        match="プリセットID",
    ):
        save_postprocessor(
            PostprocessorPreset(
                preset_id="../outside",
                display_name="Unsafe",
                command="unsafe",
            ),
            tmp_path / "postprocessors.toml",
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
def test_save_postprocessor_rejects_non_finite_timeout(
    tmp_path,
    timeout,
):
    with pytest.raises(
        postprocessing.PostprocessorConfigError,
        match="timeout_sec",
    ):
        save_postprocessor(
            PostprocessorPreset(
                preset_id="bad_timeout",
                display_name="Bad Timeout",
                command="bad-cli",
                timeout_sec=timeout,
            ),
            tmp_path / "postprocessors.toml",
        )


def test_save_postprocessor_does_not_overwrite_malformed_local_file(
    tmp_path,
):
    user_path = tmp_path / "postprocessors.toml"
    malformed = b"[postprocessors.broken\ncommand = 'broken'\n"
    user_path.write_bytes(malformed)

    with pytest.raises(
        postprocessing.PostprocessorConfigError,
        match="上書きしません",
    ):
        save_postprocessor(
            PostprocessorPreset(
                preset_id="safe",
                display_name="Safe",
                command="safe-cli",
            ),
            user_path,
        )

    assert user_path.read_bytes() == malformed


def test_save_postprocessor_preserves_invalid_existing_target(tmp_path):
    user_path = tmp_path / "postprocessors.toml"
    invalid = (
        "[postprocessors.broken]\n"
        'display_name = "Broken"\n'
        'command = ""\n'
    )
    user_path.write_text(invalid, encoding="utf-8")

    with pytest.raises(
        postprocessing.PostprocessorConfigError,
        match="上書きしません",
    ):
        save_postprocessor(
            PostprocessorPreset(
                preset_id="broken",
                display_name="Replacement",
                command="replacement-cli",
            ),
            user_path,
        )

    assert user_path.read_text(encoding="utf-8") == invalid


def test_save_postprocessor_rejects_changed_file_fingerprint(tmp_path):
    user_path = tmp_path / "postprocessors.toml"
    user_path.write_text(
        (
            "[postprocessors.local]\n"
            'display_name = "Before"\n'
            'command = "local-cli"\n'
        ),
        encoding="utf-8",
    )
    expected = file_fingerprint(user_path)
    external = (
        "[postprocessors.local]\n"
        'display_name = "External"\n'
        'command = "external-cli"\n'
    )
    user_path.write_text(external, encoding="utf-8")

    with pytest.raises(StaleFileError):
        save_postprocessor(
            PostprocessorPreset(
                preset_id="local",
                display_name="UI",
                command="ui-cli",
            ),
            user_path,
            expected_fingerprint=expected,
        )

    assert user_path.read_text(encoding="utf-8") == external


def test_local_ids_include_partial_bundled_overrides(tmp_path):
    user_path = tmp_path / "postprocessors.toml"
    user_path.write_text(
        "[postprocessors.claude]\ntimeout_sec = 10\n",
        encoding="utf-8",
    )

    assert load_local_postprocessor_ids(user_path) == frozenset({"claude"})
    assert load_local_postprocessors(user_path) == {}


def test_argument_mode_rejects_windows_batch_launcher(monkeypatch):
    calls = []

    monkeypatch.setattr(
        postprocessing,
        "command_uses_windows_batch",
        lambda executable, environment: True,
    )
    monkeypatch.setattr(
        postprocessing.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    preset = PostprocessorPreset(
        preset_id="batch",
        display_name="Batch",
        command='batch-cli "{{prompt}}"',
        input_mode="argument",
        prompt_template="{{transcript}}",
    )

    result = run_postprocessor(preset, "untrusted & transcript", None, "ja")

    assert result.succeeded is False
    assert ".cmd/.bat" in result.error
    assert calls == []


def test_preflight_failure_prevents_main_command(monkeypatch):
    calls = []

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            calls.append(argv)

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(postprocessing.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(postprocessing, "subprocess_run_options", lambda: {})
    preset = PostprocessorPreset(
        preset_id="local",
        display_name="Local",
        command="local run",
        preflight_command="local show model",
        preflight_failure_message="Model is missing",
    )

    result = run_postprocessor(preset, "original", None, "ja")

    assert calls == [["local", "show", "model"]]
    assert result.succeeded is False
    assert result.text == "original"
    assert result.error == "Model is missing"


def test_timeout_cleanup_has_no_unbounded_final_wait(monkeypatch):
    class FakeStream:
        closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        pid = 123

        def __init__(self):
            self.stdin = FakeStream()
            self.stdout = FakeStream()
            self.stderr = FakeStream()
            self.killed = False
            self.wait_timeouts = []

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired("fake", timeout)

        def poll(self):
            return None if not self.killed else 1

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            return 1

    process = FakeProcess()
    monkeypatch.setattr(postprocessing, "terminate_process_tree", lambda process: None)

    postprocessing._finish_timed_out_process(process, "fake")

    assert process.killed is True
    assert process.wait_timeouts == [5.0]
    assert process.stdin.closed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True


def test_command_failure_keeps_dictionary_result_and_does_not_log_transcript(
    monkeypatch,
    caplog,
):
    secret = "private spoken text"

    class FakeProcess:
        returncode = 2

        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            return "", f"echoed {secret}"

    monkeypatch.setattr(postprocessing.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(postprocessing, "subprocess_run_options", lambda: {})
    preset = PostprocessorPreset(
        preset_id="fake",
        display_name="Fake",
        command="fake",
        prompt_template="{{transcript}}",
    )

    with caplog.at_level(logging.WARNING):
        result = process_transcript(
            f"全ウィスパー {secret}",
            _profile(),
            "fake",
            {"fake": preset},
            "ja",
        )

    assert result.succeeded is False
    assert result.text == f"ZenWhisper {secret}"
    assert secret not in caplog.text


def _is_windows_process_alive(pid: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
            handle,
            ctypes.byref(exit_code),
        ):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _force_kill_windows_process(pid: int) -> None:
    if not pid or not _is_windows_process_alive(pid):
        return
    from src.platform.windows import _system_executable

    subprocess.run(
        [
            _system_executable("taskkill.exe"),
            "/PID",
            str(pid),
            "/T",
            "/F",
        ],
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


@pytest.mark.skipif(not is_windows(), reason="Windows process-tree regression")
def test_timeout_terminates_spawned_windows_child_process(tmp_path):
    parent_script = tmp_path / "spawn_child.py"
    child_pid_path = tmp_path / "child.pid"
    parent_script.write_text(
        """
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(sys.argv[1], "w", encoding="utf-8") as file:
    file.write(str(child.pid))
time.sleep(60)
""",
        encoding="utf-8",
    )
    command = subprocess.list2cmdline(
        [sys.executable, str(parent_script), str(child_pid_path)]
    )
    preset = PostprocessorPreset(
        preset_id="timeout-tree",
        display_name="Timeout tree",
        command=command,
        timeout_sec=1.0,
    )
    child_pid = 0

    try:
        result = run_postprocessor(preset, "private transcript", None, "ja")
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        assert result.succeeded is False
        assert "タイムアウト" in result.error
        assert _is_windows_process_alive(child_pid) is False
    finally:
        _force_kill_windows_process(child_pid)


@pytest.mark.skipif(not is_windows(), reason="Windows process-tree regression")
def test_preflight_timeout_terminates_spawned_windows_child_process(tmp_path):
    preflight_script = tmp_path / "spawn_preflight_child.py"
    child_pid_path = tmp_path / "preflight-child.pid"
    preflight_script.write_text(
        """
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(sys.argv[1], "w", encoding="utf-8") as file:
    file.write(str(child.pid))
time.sleep(60)
""",
        encoding="utf-8",
    )
    preflight_command = subprocess.list2cmdline(
        [sys.executable, str(preflight_script), str(child_pid_path)]
    )
    main_command = subprocess.list2cmdline(
        [sys.executable, "-c", "print('unused')"]
    )
    preset = PostprocessorPreset(
        preset_id="preflight-timeout-tree",
        display_name="Preflight timeout tree",
        command=main_command,
        preflight_command=preflight_command,
        timeout_sec=1.0,
    )
    child_pid = 0

    try:
        result = run_postprocessor(preset, "private transcript", None, "ja")
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        assert result.succeeded is False
        assert "事前確認がタイムアウト" in result.error
        assert _is_windows_process_alive(child_pid) is False
    finally:
        _force_kill_windows_process(child_pid)


def test_off_mode_does_not_apply_dictionary():
    result = process_transcript(
        "全ウィスパー",
        _profile(),
        "off",
        {},
        "ja",
    )

    assert result.text == "全ウィスパー"
    assert result.applied is False

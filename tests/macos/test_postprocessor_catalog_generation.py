"""Tests for compiling the shared postprocessor defaults into native JSON."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "macos/scripts/generate_postprocessor_catalog.py"


def _run_generator(source: Path, destination: Path, *arguments: str):
    return subprocess.run(
        [
            sys.executable,
            "-P",
            str(GENERATOR),
            str(source),
            str(destination),
            *arguments,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_generator_translates_commands_and_skips_disabled_presets(tmp_path):
    source = tmp_path / "defaults.toml"
    destination = tmp_path / "defaults.json"
    source.write_text(
        """
[postprocessors.example]
display_name = "Example"
command = 'cleaner --label "two words" --system-prompt-file "{{system_prompt_file}}"'
preflight_command = "cleaner --version"
input_mode = "stdin"
output_mode = "stdout"
timeout_sec = 12
data_destination = "local"
environment = { MODE = "strict" }
system_prompt = "Dedicated proofreader"
prompt_template = "<transcript>\\n{{transcript}}\\n</transcript>"

[postprocessors.disabled]
enabled = false
display_name = "Disabled"
command = "disabled"
prompt_template = "{{transcript}}"
""",
        encoding="utf-8",
    )

    result = _run_generator(source, destination)

    assert result.returncode == 0, result.stderr
    catalog = json.loads(destination.read_text(encoding="utf-8"))
    assert set(catalog["postprocessors"]) == {"example"}
    preset = catalog["postprocessors"]["example"]
    assert preset["executable"] == "cleaner"
    assert preset["arguments"] == [
        "--label",
        "two words",
        "--system-prompt-file",
        "{{system_prompt_file}}",
    ]
    assert preset["preflight_executable"] == "cleaner"
    assert preset["preflight_arguments"] == ["--version"]
    assert preset["environment"] == {"MODE": "strict"}
    assert preset["system_prompt"] == "Dedicated proofreader"
    assert preset["timeout_sec"] == 12
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o644


def test_generator_translates_argument_mode_prompt_as_one_argument(tmp_path):
    source = tmp_path / "argument.toml"
    destination = tmp_path / "defaults.json"
    source.write_text(
        """
[postprocessors.example]
display_name = "Example"
command = 'cleaner --prompt "{{prompt}}"'
input_mode = "argument"
prompt_template = "{{transcript}}"
""",
        encoding="utf-8",
    )

    result = _run_generator(source, destination)

    assert result.returncode == 0, result.stderr
    preset = json.loads(destination.read_text(encoding="utf-8"))["postprocessors"][
        "example"
    ]
    assert preset["executable"] == "cleaner"
    assert preset["arguments"] == ["--prompt", "{{prompt}}"]
    assert preset["input_mode"] == "argument"


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (
            """
command = 'cleaner "{{transcript}}"'
input_mode = "stdin"
prompt_template = "{{transcript}}"
""",
            "stdin",
        ),
        (
            """
command = "cleaner"
input_mode = "argument"
prompt_template = "{{transcript}}"
""",
            "{{prompt}}",
        ),
        (
            """
command = "cleaner"
prompt_template = "{{unknown}} {{transcript}}"
""",
            "未対応",
        ),
        (
            """
command = "cleaner"
prompt_template = "missing transcript placeholder"
""",
            "{{transcript}}",
        ),
        (
            """
command = "cleaner"
prompt_template = "{{transcript}}"
future_field = true
""",
            "未対応",
        ),
        (
            """
command = 'cleaner "unterminated'
prompt_template = "{{transcript}}"
""",
            "解析",
        ),
        (
            """
command = "cleaner"
timeout_sec = 301
prompt_template = "{{transcript}}"
""",
            "300",
        ),
        (
            """
command = "cleaner"
environment = {"BAD=KEY" = "value"}
prompt_template = "{{transcript}}"
""",
            "environment key",
        ),
        (
            """
command = "cleaner"
system_prompt = "Dedicated"
prompt_template = "{{transcript}}"
""",
            "{{system_prompt_file}}",
        ),
        (
            """
command = 'cleaner "{{system_prompt_file}}"'
prompt_template = "{{transcript}}"
""",
            "system_prompt",
        ),
        (
            """
command = 'cleaner "{{system_prompt_file}}"'
system_prompt = "Dedicated {{language}}"
prompt_template = "{{transcript}}"
""",
            "system_prompt",
        ),
        (
            """
command = "cleaner"
prompt_template = "{{system_prompt_file}} {{transcript}}"
""",
            "prompt_template",
        ),
    ],
)
def test_generator_validation_preserves_existing_destination(
    tmp_path,
    body,
    expected_error,
):
    source = tmp_path / "invalid.toml"
    destination = tmp_path / "defaults.json"
    source.write_text(
        f"""
[postprocessors.invalid]
display_name = "Invalid"
{body}
""",
        encoding="utf-8",
    )
    original = b'{"keep":true}\n'
    destination.write_bytes(original)

    result = _run_generator(source, destination)

    assert result.returncode == 1
    assert expected_error in result.stderr
    assert destination.read_bytes() == original
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (
            "command = "
            + repr("cleaner " + " ".join(["argument"] * 257))
            + '\nprompt_template = "{{transcript}}"\n',
            "256",
        ),
        (
            "command = \"cleaner\"\nenvironment = {"
            + ", ".join(f"KEY_{index} = \"value\"" for index in range(129))
            + '}\nprompt_template = "{{transcript}}"\n',
            "128",
        ),
        (
            'command = "cleaner"\nprompt_template = """{{transcript}}'
            + ("x" * (8 * 1024))
            + '"""\n',
            "8192",
        ),
        (
            "command = \"cleaner\"\n"
            "preflight_command = 'cleaner \"{{unterminated\"'\n"
            'prompt_template = "{{transcript}}"\n',
            "テンプレート",
        ),
        (
            "command = "
            + repr(
                "cleaner "
                + " ".join(f"'{('x' * (8 * 1024))}'" for _ in range(40))
            )
            + '\nprompt_template = "{{transcript}}"\n',
            "payload",
        ),
        (
            "command = "
            + repr(
                "cleaner "
                + " ".join(f"'{('/' * (8 * 1024))}'" for _ in range(31))
            )
            + '\nprompt_template = "{{transcript}}"\n',
            "payload",
        ),
        (
            'command = "clean{{er"\nprompt_template = "{{transcript}}"\n',
            "テンプレート",
        ),
    ],
)
def test_generator_enforces_native_acceptance_limits(
    tmp_path,
    body,
    expected_error,
):
    source = tmp_path / "invalid-limits.toml"
    destination = tmp_path / "defaults.json"
    source.write_text(
        f"""
[postprocessors.invalid]
display_name = "Invalid"
{body}
""",
        encoding="utf-8",
    )
    original = b'{"keep":true}\n'
    destination.write_bytes(original)

    result = _run_generator(source, destination)

    assert result.returncode == 1
    assert expected_error in result.stderr
    assert destination.read_bytes() == original
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


def test_generator_accepts_values_at_native_limits(tmp_path):
    source = tmp_path / "native-limits.toml"
    destination = tmp_path / "defaults.json"
    transcript_placeholder = "{{transcript}}"
    prompt = transcript_placeholder + (
        "x" * (8192 - len(transcript_placeholder.encode("utf-8")))
    )
    command = "cleaner " + " ".join(
        f"argument-{index}" for index in range(256)
    )
    environment = ", ".join(
        f'KEY_{index} = "value"' for index in range(128)
    )
    source.write_text(
        "\n".join(
            (
                "[postprocessors.limits]",
                'display_name = "Limits"',
                f"command = {json.dumps(command)}",
                f"environment = {{{environment}}}",
                f"prompt_template = {json.dumps(prompt)}",
                "",
            )
        ),
        encoding="utf-8",
    )

    result = _run_generator(source, destination)

    assert result.returncode == 0, result.stderr
    preset = json.loads(destination.read_text(encoding="utf-8"))["postprocessors"][
        "limits"
    ]
    assert len(preset["arguments"]) == 256
    assert len(preset["environment"]) == 128
    assert len(preset["prompt_template"].encode("utf-8")) == 8192


def test_generator_check_detects_a_stale_catalog(tmp_path):
    source = tmp_path / "defaults.toml"
    destination = tmp_path / "defaults.json"
    source.write_text(
        """
[postprocessors.example]
display_name = "Example"
command = "cleaner"
prompt_template = "{{transcript}}"
""",
        encoding="utf-8",
    )
    destination.write_text("{}\n", encoding="utf-8")

    result = _run_generator(source, destination, "--check")

    assert result.returncode == 1
    assert "is stale" in result.stderr
    assert destination.read_text(encoding="utf-8") == "{}\n"

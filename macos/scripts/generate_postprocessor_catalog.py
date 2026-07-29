#!/usr/bin/env python3
"""Generate the native macOS postprocessor catalog from the shared TOML source."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import tempfile
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT / "postprocessors.default.toml"
DEFAULT_DESTINATION = (
    REPO_ROOT
    / "macos"
    / "ZenWhisper"
    / "ZenWhisper"
    / "Resources"
    / "postprocessors.default.json"
)

ALLOWED_FIELDS = {
    "display_name",
    "command",
    "preflight_command",
    "preflight_failure_message",
    "input_mode",
    "output_mode",
    "timeout_sec",
    "data_destination",
    "system_prompt",
    "prompt_template",
    "environment",
    "enabled",
}
ALLOWED_PLACEHOLDERS = {
    "prompt",
    "system_prompt_file",
    "transcript",
    "context",
    "terms",
    "profile_name",
    "language",
    "boundary",
}
PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
PRESET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_ARGUMENT_COUNT = 256
MAX_ENVIRONMENT_ENTRY_COUNT = 128
MAX_STRING_UTF8_BYTES = 8 * 1024
MAX_SERIALIZED_PAYLOAD_BYTES = 256 * 1024
MAX_ENHANCEMENT_VALUE_COUNT = 4_096
MAX_CATALOG_DOCUMENT_BYTES = 1024 * 1024


class CatalogGenerationError(ValueError):
    """Raised when the shared TOML cannot be translated safely."""


def _split_posix_command(command: str) -> list[str]:
    """Split a command using the argv rules used by the native macOS app."""
    try:
        arguments = shlex.split(command, posix=True)
    except ValueError as exc:
        raise CatalogGenerationError(f"command の解析に失敗しました: {exc}") from exc
    if not arguments:
        raise CatalogGenerationError("command は空にできません")
    return arguments


def _string_field(
    raw: dict[str, Any],
    field: str,
    default: str,
) -> str:
    value = raw.get(field, default)
    if not isinstance(value, str):
        raise CatalogGenerationError(f"{field} は文字列で指定してください")
    return value


def _validate_placeholders(value: str) -> set[str]:
    placeholders = set(PLACEHOLDER_RE.findall(value))
    unknown = placeholders - ALLOWED_PLACEHOLDERS
    if unknown:
        raise CatalogGenerationError(
            f"未対応のプレースホルダーがあります: {', '.join(sorted(unknown))}"
        )
    return placeholders


def _validate_string(
    value: str,
    field: str,
    *,
    allow_empty: bool = True,
) -> str:
    if not allow_empty and not value:
        raise CatalogGenerationError(f"{field} は空にできません")
    if "\0" in value:
        raise CatalogGenerationError(f"{field} にNUL文字は使用できません")
    if len(value.encode("utf-8")) > MAX_STRING_UTF8_BYTES:
        raise CatalogGenerationError(
            f"{field} はUTF-8で{MAX_STRING_UTF8_BYTES}バイト以下にしてください"
        )
    return value


def _json_value_count(value: object) -> int:
    if isinstance(value, dict):
        return 1 + len(value) + sum(
            _json_value_count(item) for item in value.values()
        )
    if isinstance(value, list):
        return 1 + len(value) + sum(_json_value_count(item) for item in value)
    return 1


def _swift_json_bytes(value: object) -> bytes:
    # Foundation JSONSerialization escapes forward slashes unless
    # .withoutEscapingSlashes is requested. Native payload validation uses the
    # default behavior, so mirror it when enforcing the same byte budget.
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return encoded.replace("/", "\\/").encode("utf-8")


def _validate_backend_payload(
    preset_id: str,
    preset: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "id": preset_id,
        "display_name": preset["display_name"],
        "executable": preset["executable"],
        "arguments": preset["arguments"],
        "preflight_arguments": preset["preflight_arguments"],
        "preflight_failure_message": preset["preflight_failure_message"],
        "input_mode": preset["input_mode"],
        "output_mode": "stdout",
        "timeout_sec": preset["timeout_sec"],
        "data_destination": preset["data_destination"],
        "system_prompt": preset["system_prompt"],
        "prompt_template": preset["prompt_template"],
        "environment": preset["environment"],
    }
    preflight_executable = preset["preflight_executable"]
    if preflight_executable:
        payload["preflight_executable"] = preflight_executable
    envelope = {"postprocessor": {"mode": "preset", "preset": payload}}
    encoded = _swift_json_bytes(envelope)
    if len(encoded) > MAX_SERIALIZED_PAYLOAD_BYTES:
        raise CatalogGenerationError(
            "postprocessor payload がmacOSのサイズ上限を超えています"
        )
    if _json_value_count(envelope) > MAX_ENHANCEMENT_VALUE_COUNT:
        raise CatalogGenerationError(
            "postprocessor payload がmacOSの値数上限を超えています"
        )


def _native_preset(
    preset_id: str,
    raw: dict[str, Any],
) -> dict[str, object] | None:
    if not PRESET_ID_RE.fullmatch(preset_id) or preset_id in {"off", "dictionary"}:
        raise CatalogGenerationError(f"無効なプリセットIDです: {preset_id}")
    unknown_fields = set(raw) - ALLOWED_FIELDS
    if unknown_fields:
        raise CatalogGenerationError(
            f"未対応のフィールドがあります: {', '.join(sorted(unknown_fields))}"
        )

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise CatalogGenerationError("enabled は true または false で指定してください")
    if not enabled:
        return None

    display_name = _validate_string(
        _string_field(raw, "display_name", preset_id).strip(),
        "display_name",
        allow_empty=False,
    )
    command_text = _string_field(raw, "command", "")
    preflight_text = _string_field(raw, "preflight_command", "")
    preflight_failure_message = _validate_string(
        _string_field(
            raw,
            "preflight_failure_message",
            "",
        ).strip(),
        "preflight_failure_message",
    )
    input_mode = _string_field(raw, "input_mode", "stdin")
    output_mode = _string_field(raw, "output_mode", "stdout")
    data_destination = _string_field(raw, "data_destination", "unknown")
    system_prompt = _validate_string(
        _string_field(raw, "system_prompt", ""),
        "system_prompt",
    )
    prompt_template = _validate_string(
        _string_field(raw, "prompt_template", "{{transcript}}"),
        "prompt_template",
    )
    if not display_name or not command_text.strip():
        raise CatalogGenerationError("display_name と command は空にできません")
    if input_mode not in {"stdin", "argument"}:
        raise CatalogGenerationError("input_mode は stdin または argument で指定してください")
    if output_mode != "stdout":
        raise CatalogGenerationError("macOS の output_mode は stdout のみ対応しています")
    if data_destination not in {"local", "remote", "unknown"}:
        raise CatalogGenerationError(
            "data_destination は local、remote、unknown のいずれかで指定してください"
        )

    timeout_value = raw.get("timeout_sec", 30)
    if (
        not isinstance(timeout_value, (int, float))
        or isinstance(timeout_value, bool)
        or not math.isfinite(timeout_value)
        or timeout_value <= 0
        or timeout_value > 300
    ):
        raise CatalogGenerationError("timeout_sec は300以下の正の数で指定してください")
    timeout: int | float = timeout_value
    if float(timeout_value).is_integer():
        timeout = int(timeout_value)

    environment = raw.get("environment", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise CatalogGenerationError(
            "environment は文字列キー・値のテーブルで指定してください"
        )
    if len(environment) > MAX_ENVIRONMENT_ENTRY_COUNT:
        raise CatalogGenerationError(
            f"environment は{MAX_ENVIRONMENT_ENTRY_COUNT}件以下にしてください"
        )
    for key, value in environment.items():
        _validate_string(key, "environment key", allow_empty=False)
        _validate_string(value, f"environment.{key}")
        if not key.strip() or "=" in key:
            raise CatalogGenerationError(
                "environment key は空白のみまたは '=' を含む値にできません"
            )

    prompt_placeholders = _validate_placeholders(prompt_template)
    command_placeholders = _validate_placeholders(command_text)
    if PLACEHOLDER_RE.search(system_prompt):
        raise CatalogGenerationError(
            "system_prompt ではプレースホルダーを使用できません"
        )
    if "system_prompt_file" in prompt_placeholders:
        raise CatalogGenerationError(
            "prompt_template では {{system_prompt_file}} を使用できません"
        )
    if "transcript" not in prompt_placeholders:
        raise CatalogGenerationError("prompt_template には {{transcript}} が必要です")
    if bool(system_prompt) != ("system_prompt_file" in command_placeholders):
        raise CatalogGenerationError(
            "system_prompt と command の {{system_prompt_file}} は"
            "両方を指定するか両方を省略してください"
        )
    if input_mode == "stdin" and command_placeholders - {"system_prompt_file"}:
        raise CatalogGenerationError(
            "stdin モードの command では {{system_prompt_file}} 以外の"
            "プレースホルダーを使用できません"
        )
    if input_mode == "argument" and "prompt" not in command_placeholders:
        raise CatalogGenerationError(
            "argument モードでは command に {{prompt}} が必要です"
        )
    if PLACEHOLDER_RE.search(preflight_text):
        raise CatalogGenerationError(
            "preflight_command ではプレースホルダーを使用できません"
        )

    command = _split_posix_command(command_text)
    if len(command) - 1 > MAX_ARGUMENT_COUNT:
        raise CatalogGenerationError(
            f"arguments は{MAX_ARGUMENT_COUNT}件以下にしてください"
        )
    executable = _validate_string(
        command[0].strip(),
        "executable",
        allow_empty=False,
    )
    arguments = command[1:]
    for index, argument in enumerate(arguments):
        _validate_string(argument, f"arguments[{index}]")
    if "{{" in executable or "}}" in executable:
        raise CatalogGenerationError(
            "実行ファイル名にはテンプレート構文を使用できません"
        )
    preflight = (
        _split_posix_command(preflight_text)
        if preflight_text
        else []
    )
    if len(preflight) - 1 > MAX_ARGUMENT_COUNT:
        raise CatalogGenerationError(
            f"preflight_arguments は{MAX_ARGUMENT_COUNT}件以下にしてください"
        )
    preflight_executable = ""
    preflight_arguments: list[str] = []
    if preflight:
        preflight_executable = _validate_string(
            preflight[0].strip(),
            "preflight_executable",
            allow_empty=False,
        )
        preflight_arguments = preflight[1:]
        for index, argument in enumerate(preflight_arguments):
            _validate_string(argument, f"preflight_arguments[{index}]")
        if any(
            "{{" in value or "}}" in value
            for value in [preflight_executable, *preflight_arguments]
        ):
            raise CatalogGenerationError(
                "preflight ではテンプレート構文を使用できません"
            )
    native = {
        "display_name": display_name,
        "executable": executable,
        "arguments": arguments,
        "preflight_executable": preflight_executable,
        "preflight_arguments": preflight_arguments,
        "preflight_failure_message": preflight_failure_message,
        "input_mode": input_mode,
        "data_destination": data_destination,
        "timeout_sec": timeout,
        "system_prompt": system_prompt,
        "prompt_template": prompt_template,
        "environment": dict(sorted(environment.items())),
    }
    _validate_backend_payload(preset_id, native)
    return native


def generate_catalog(source: Path) -> dict[str, object]:
    """Load and strictly validate the shared defaults before translating them."""
    try:
        with source.open("rb") as source_file:
            root = tomllib.load(source_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CatalogGenerationError(f"{source} を読み込めません: {exc}") from exc
    raw_presets = root.get("postprocessors", {})
    if not isinstance(raw_presets, dict):
        raise CatalogGenerationError("postprocessors はテーブルで指定してください")

    postprocessors: dict[str, object] = {}
    for preset_id, raw in raw_presets.items():
        if not isinstance(preset_id, str) or not isinstance(raw, dict):
            raise CatalogGenerationError("postprocessors の各項目はテーブルにしてください")
        try:
            preset = _native_preset(preset_id, raw)
        except CatalogGenerationError as exc:
            raise CatalogGenerationError(f"{preset_id}: {exc}") from exc
        if preset is not None:
            postprocessors[preset_id] = preset
    return {"version": 1, "postprocessors": postprocessors}


def encode_catalog(catalog: dict[str, object]) -> bytes:
    encoded = (
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_CATALOG_DOCUMENT_BYTES:
        raise CatalogGenerationError(
            "生成カタログがmacOSのドキュメントサイズ上限を超えています"
        )
    return encoded


def _atomic_write(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        default=DEFAULT_DESTINATION,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the generated catalog is stale",
    )
    arguments = parser.parse_args()

    try:
        expected = encode_catalog(generate_catalog(arguments.source))
    except (OSError, CatalogGenerationError, ValueError) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")

    if arguments.check:
        try:
            current = arguments.destination.read_bytes()
        except OSError as exc:
            parser.exit(1, f"{parser.prog}: {exc}\n")
        if current != expected:
            parser.exit(
                1,
                (
                    f"{arguments.destination} is stale; regenerate it from "
                    f"{arguments.source}\n"
                ),
            )
        return 0

    try:
        _atomic_write(arguments.destination, expected)
    except OSError as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

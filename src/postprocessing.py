"""Trusted local-command transcript postprocessing."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import ContextManager

from src.config import POSTPROCESSOR_DICTIONARY, POSTPROCESSOR_OFF
from src.platform import (
    command_uses_windows_batch,
    split_command,
    subprocess_run_options,
    terminate_process_tree,
)
from src.profiles import Profile, apply_replacements, render_terms
from src.toml_storage import (
    FileFingerprint,
    atomic_write_toml,
    file_fingerprint,
)

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_PRESETS_PATH = _ROOT_DIR / "postprocessors.default.toml"
_USER_PRESETS_PATH = _ROOT_DIR / "postprocessors.toml"
_PRESET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

DATA_DESTINATION_LOCAL = "local"
DATA_DESTINATION_REMOTE = "remote"
DATA_DESTINATION_UNKNOWN = "unknown"
VALID_DATA_DESTINATIONS = (
    DATA_DESTINATION_LOCAL,
    DATA_DESTINATION_REMOTE,
    DATA_DESTINATION_UNKNOWN,
)
VALID_INPUT_MODES = ("stdin", "argument")
VALID_OUTPUT_MODES = ("stdout",)
ADAPTER_GENERIC = "generic"
ADAPTER_KIRO = "kiro"
VALID_ADAPTERS = (ADAPTER_GENERIC, ADAPTER_KIRO)
_KIRO_AGENT_ID = "zen-whisper-postprocessor"

StateGuard = Callable[[], ContextManager[bool]]
ProcessCallback = Callable[[subprocess.Popen[str]], None]

SUPPORTED_TEMPLATE_PLACEHOLDERS = (
    "agent",
    "prompt",
    "system_prompt_file",
    "transcript",
    "context",
    "terms",
    "profile_name",
    "language",
    "boundary",
)
_ALLOWED_PLACEHOLDERS = frozenset(SUPPORTED_TEMPLATE_PLACEHOLDERS)
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
_UNSAFE_OUTPUT_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+"
)
_PRESET_FIELDS = {
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
    "adapter",
    "model",
    "enabled",
}


class PostprocessorConfigError(ValueError):
    """Raised when a command preset is invalid."""


@dataclass(frozen=True)
class PostprocessorPreset:
    preset_id: str
    display_name: str
    command: str
    input_mode: str = "stdin"
    output_mode: str = "stdout"
    timeout_sec: float = 30.0
    data_destination: str = DATA_DESTINATION_UNKNOWN
    adapter: str = ADAPTER_GENERIC
    model: str = ""
    system_prompt: str = ""
    prompt_template: str = "{{transcript}}"
    preflight_command: str = ""
    preflight_failure_message: str = ""
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PostprocessResult:
    text: str
    succeeded: bool
    applied: bool
    error: str = ""


def _read_preset_tables(
    path: Path,
    *,
    strict: bool = False,
) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except Exception as exc:
        if strict:
            raise PostprocessorConfigError(
                f"{path.name} のTOMLを読み込めないため上書きしません"
            ) from exc
        logger.warning(
            "後処理プリセットを読み込めません: file=%s reason=%s",
            path.name,
            type(exc).__name__,
        )
        return {}

    raw_presets = data.get("postprocessors", {})
    if not isinstance(raw_presets, dict):
        if strict:
            raise PostprocessorConfigError(
                f"{path.name} の postprocessors がテーブルでないため"
                "上書きしません"
            )
        logger.warning(
            "後処理プリセットを読み込めません: file=%s reason=postprocessors is not a table",
            path.name,
        )
        return {}
    presets: dict[str, dict[str, object]] = {}
    for preset_id, raw in raw_presets.items():
        if not isinstance(raw, dict):
            if strict:
                raise PostprocessorConfigError(
                    f"{path.name} のプリセット '{preset_id}' が"
                    "テーブルでないため上書きしません"
                )
            continue
        presets[str(preset_id)] = dict(raw)
    return presets


def _find_unknown_placeholders(template: str) -> set[str]:
    return {
        placeholder
        for placeholder in _ANY_PLACEHOLDER_RE.findall(template)
        if placeholder not in _ALLOWED_PLACEHOLDERS
    }


def _parse_preset(preset_id: str, raw: dict[str, object]) -> PostprocessorPreset | None:
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PostprocessorConfigError("enabled は true または false で指定してください")
    if enabled is False:
        return None
    if not preset_id:
        raise PostprocessorConfigError("プリセットIDは空にできません")
    if preset_id in (POSTPROCESSOR_OFF, POSTPROCESSOR_DICTIONARY):
        raise PostprocessorConfigError(f"'{preset_id}' は予約済みのIDです")

    unknown_fields = set(raw) - _PRESET_FIELDS
    if unknown_fields:
        raise PostprocessorConfigError(
            f"未対応のフィールドがあります: {', '.join(sorted(unknown_fields))}"
        )

    display_name = raw.get("display_name", preset_id)
    command = raw.get("command", "")
    input_mode = raw.get("input_mode", "stdin")
    output_mode = raw.get("output_mode", "stdout")
    data_destination = raw.get("data_destination", DATA_DESTINATION_UNKNOWN)
    adapter = raw.get("adapter", ADAPTER_GENERIC)
    model = raw.get("model", "")
    system_prompt = raw.get("system_prompt", "")
    prompt_template = raw.get("prompt_template", "{{transcript}}")
    preflight_command = raw.get("preflight_command", "")
    preflight_failure_message = raw.get("preflight_failure_message", "")
    timeout_sec = raw.get("timeout_sec", 30.0)
    environment = raw.get("environment", {})

    string_fields = {
        "display_name": display_name,
        "command": command,
        "input_mode": input_mode,
        "output_mode": output_mode,
        "data_destination": data_destination,
        "adapter": adapter,
        "model": model,
        "system_prompt": system_prompt,
        "prompt_template": prompt_template,
        "preflight_command": preflight_command,
        "preflight_failure_message": preflight_failure_message,
    }
    for field_name, value in string_fields.items():
        if not isinstance(value, str):
            raise PostprocessorConfigError(f"{field_name} は文字列で指定してください")
    normalized_model = model.strip()
    if not display_name.strip() or not command.strip():
        raise PostprocessorConfigError("display_name と command は空にできません")
    if input_mode not in VALID_INPUT_MODES:
        raise PostprocessorConfigError(
            f"input_mode は {', '.join(VALID_INPUT_MODES)} から選んでください"
        )
    if output_mode not in VALID_OUTPUT_MODES:
        raise PostprocessorConfigError(
            f"output_mode は {', '.join(VALID_OUTPUT_MODES)} から選んでください"
        )
    if data_destination not in VALID_DATA_DESTINATIONS:
        raise PostprocessorConfigError(
            f"data_destination は {', '.join(VALID_DATA_DESTINATIONS)} から選んでください"
        )
    if adapter not in VALID_ADAPTERS:
        raise PostprocessorConfigError(
            f"adapter は {', '.join(VALID_ADAPTERS)} から選んでください"
        )
    if (
        not isinstance(timeout_sec, (int, float))
        or isinstance(timeout_sec, bool)
        or not math.isfinite(timeout_sec)
        or timeout_sec <= 0
    ):
        raise PostprocessorConfigError("timeout_sec は正の数で指定してください")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise PostprocessorConfigError("environment は文字列キー・値のテーブルで指定してください")

    unknown_placeholders = (
        _find_unknown_placeholders(command)
        | _find_unknown_placeholders(system_prompt)
        | _find_unknown_placeholders(prompt_template)
    )
    if unknown_placeholders:
        raise PostprocessorConfigError(
            f"未対応のプレースホルダーがあります: {', '.join(sorted(unknown_placeholders))}"
        )
    if _ANY_PLACEHOLDER_RE.search(system_prompt):
        raise PostprocessorConfigError(
            "system_prompt ではプレースホルダーを使用できません"
        )
    prompt_placeholders = set(_PLACEHOLDER_RE.findall(prompt_template))
    if "system_prompt_file" in prompt_placeholders:
        raise PostprocessorConfigError(
            "prompt_template では {{system_prompt_file}} を使用できません"
        )
    if "{{transcript}}" not in prompt_template:
        raise PostprocessorConfigError("prompt_template には {{transcript}} が必要です")
    command_placeholders = set(_PLACEHOLDER_RE.findall(command))
    has_system_prompt_file = "system_prompt_file" in command_placeholders
    if _PLACEHOLDER_RE.search(preflight_command):
        raise PostprocessorConfigError("preflight_command ではプレースホルダーを使用できません")

    # Parse once during loading so malformed quoting is reported before recording.
    try:
        command_argv = split_command(command)
        if not command_argv:
            raise ValueError("empty command")
        if preflight_command and not split_command(preflight_command):
            raise ValueError("empty preflight command")
    except ValueError as exc:
        raise PostprocessorConfigError(f"command の解析に失敗しました: {exc}") from exc
    if _PLACEHOLDER_RE.search(command_argv[0]):
        raise PostprocessorConfigError(
            "実行ファイル名にはプレースホルダーを使用できません"
        )
    if adapter == ADAPTER_GENERIC:
        if normalized_model:
            raise PostprocessorConfigError(
                "generic adapter のモデルは command のCLI引数で指定してください"
            )
        if bool(system_prompt) != has_system_prompt_file:
            raise PostprocessorConfigError(
                "system_prompt と command の {{system_prompt_file}} は"
                "両方を指定するか両方を省略してください"
            )
        if input_mode == "stdin" and command_placeholders - {
            "system_prompt_file"
        }:
            raise PostprocessorConfigError(
                "stdin モードの command では {{system_prompt_file}} 以外の"
                "プレースホルダーを使用できません"
            )
        if input_mode == "argument" and "{{prompt}}" not in command:
            raise PostprocessorConfigError(
                "argument モードでは command に {{prompt}} が必要です"
            )
    else:
        executable_name = Path(command_argv[0]).name.lower()
        if executable_name not in {"kiro-cli", "kiro-cli.exe"}:
            raise PostprocessorConfigError(
                "kiro adapter の実行ファイルは kiro-cli で指定してください"
            )
        if not normalized_model:
            raise PostprocessorConfigError(
                "kiro adapter では model を指定してください"
            )
        if not system_prompt.strip():
            raise PostprocessorConfigError(
                "kiro adapter では system_prompt を指定してください"
            )
        if input_mode != "stdin":
            raise PostprocessorConfigError(
                "kiro adapter の input_mode は stdin にしてください"
            )
        if has_system_prompt_file or command_placeholders != {"agent"}:
            raise PostprocessorConfigError(
                "kiro adapter の command では {{agent}} だけを使用してください"
            )

    return PostprocessorPreset(
        preset_id=preset_id,
        display_name=display_name.strip(),
        command=command,
        input_mode=input_mode,
        output_mode=output_mode,
        timeout_sec=float(timeout_sec),
        data_destination=data_destination,
        adapter=adapter,
        model=normalized_model,
        system_prompt=system_prompt,
        prompt_template=prompt_template,
        preflight_command=preflight_command,
        preflight_failure_message=preflight_failure_message.strip(),
        environment=dict(environment),
    )


def load_postprocessors(
    default_path: Path | None = None,
    user_path: Path | None = None,
) -> dict[str, PostprocessorPreset]:
    """Load bundled presets, then merge optional user overrides by field."""
    bundled_path = default_path or _DEFAULT_PRESETS_PATH
    local_path = user_path or _USER_PRESETS_PATH
    merged = _read_preset_tables(bundled_path)
    for preset_id, override in _read_preset_tables(local_path).items():
        bundled = merged.get(preset_id, {})
        safe_override = dict(override)
        command_changed = (
            "command" in safe_override
            and safe_override["command"] != bundled.get("command")
        )
        environment_changed = (
            "environment" in safe_override
            and safe_override["environment"] != bundled.get("environment")
        )
        adapter_changed = (
            "adapter" in safe_override
            and safe_override["adapter"] != bundled.get("adapter", ADAPTER_GENERIC)
        )
        model_changed = (
            "model" in safe_override
            and safe_override["model"] != bundled.get("model", "")
        )
        if command_changed and bundled:
            # These fields describe or constrain a specific executable. Carrying
            # them over to a replacement command can incorrectly label a remote
            # CLI as local or run an unrelated preflight.
            safe_override.setdefault("input_mode", "stdin")
            safe_override.setdefault("preflight_command", "")
            safe_override.setdefault("preflight_failure_message", "")
            safe_override.setdefault("environment", {})
            safe_override.setdefault("adapter", ADAPTER_GENERIC)
            safe_override.setdefault("model", "")
            if (
                "{{system_prompt_file}}" not in str(safe_override["command"])
                and "{{agent}}" not in str(safe_override["command"])
            ):
                safe_override.setdefault("system_prompt", "")
        if bundled and (
            command_changed
            or environment_changed
            or adapter_changed
            or model_changed
        ):
            if "data_destination" not in override:
                safe_override["data_destination"] = DATA_DESTINATION_UNKNOWN
                logger.warning(
                    "組み込み後処理の command/environment が変更されたため"
                    "送信先を不明として扱います: "
                    "preset=%s",
                    preset_id,
                )
        merged[preset_id] = {**bundled, **safe_override}

    presets: dict[str, PostprocessorPreset] = {}
    for preset_id, raw in merged.items():
        try:
            preset = _parse_preset(preset_id, raw)
        except PostprocessorConfigError as exc:
            logger.warning(
                "後処理プリセットを読み込めません: preset=%s reason=%s",
                preset_id,
                exc,
            )
            continue
        if preset is not None:
            presets[preset_id] = preset

    logger.info("後処理プリセットを読み込みました: count=%d", len(presets))
    return presets


def _preset_data(preset: PostprocessorPreset) -> dict[str, object]:
    if not _PRESET_ID_RE.fullmatch(preset.preset_id):
        raise PostprocessorConfigError(
            "プリセットIDは英数字で始まる64文字以内の英数字・_・-で指定してください"
        )
    raw: dict[str, object] = {
        "display_name": preset.display_name,
        "command": preset.command,
        "input_mode": preset.input_mode,
        "output_mode": preset.output_mode,
        "timeout_sec": preset.timeout_sec,
        "data_destination": preset.data_destination,
        "adapter": preset.adapter,
        "model": preset.model,
        "system_prompt": preset.system_prompt,
        "prompt_template": preset.prompt_template,
        "preflight_command": preset.preflight_command,
        "preflight_failure_message": preset.preflight_failure_message,
        "environment": dict(preset.environment),
    }
    validated = _parse_preset(preset.preset_id, raw)
    if validated is None:
        raise PostprocessorConfigError("無効なプリセットは保存できません")
    return raw


def save_postprocessor(
    preset: PostprocessorPreset,
    user_path: Path | None = None,
    *,
    expected_fingerprint: FileFingerprint | None = None,
) -> Path:
    """Validate and atomically upsert one trusted local CLI preset."""
    destination = user_path or _USER_PRESETS_PATH
    presets = _read_preset_tables(destination, strict=True)
    if preset.preset_id in presets:
        effective = load_postprocessors(user_path=destination)
        if preset.preset_id not in effective:
            raise PostprocessorConfigError(
                f"既存プリセット '{preset.preset_id}' が壊れているか"
                "無効なため上書きしません"
            )
    presets[preset.preset_id] = _preset_data(preset)
    atomic_write_toml(
        {"postprocessors": presets},
        destination,
        expected_fingerprint=expected_fingerprint,
    )
    logger.info("ローカル後処理プリセットを保存しました: id=%s", preset.preset_id)
    return destination


def postprocessors_file_fingerprint(
    user_path: Path | None = None,
) -> FileFingerprint:
    """Return the identity of the local postprocessor file."""
    return file_fingerprint(user_path or _USER_PRESETS_PATH)


def load_local_postprocessors(
    user_path: Path | None = None,
) -> dict[str, PostprocessorPreset]:
    """Load complete local presets for structured settings editing."""
    destination = user_path or _USER_PRESETS_PATH
    presets: dict[str, PostprocessorPreset] = {}
    for preset_id, raw in _read_preset_tables(destination).items():
        try:
            preset = _parse_preset(preset_id, raw)
        except PostprocessorConfigError as exc:
            logger.warning(
                "ローカル後処理プリセットを編集用に読み込めません: "
                "preset=%s reason=%s",
                preset_id,
                exc,
            )
            continue
        if preset is not None:
            presets[preset_id] = preset
    return presets


def load_local_postprocessor_ids(
    user_path: Path | None = None,
) -> frozenset[str]:
    """Return locally declared IDs, including partial bundled overrides."""
    destination = user_path or _USER_PRESETS_PATH
    return frozenset(_read_preset_tables(destination))


def _render(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)


def _template_values(
    transcript: str,
    profile: Profile | None,
    language: str,
    system_prompt_file: str,
    agent: str,
) -> dict[str, str]:
    context = profile.context.strip() if profile and profile.context.strip() else "（なし）"
    values = {
        "agent": agent,
        "transcript": transcript,
        "context": context,
        "terms": render_terms(profile),
        "profile_name": profile.name if profile else "（なし）",
        "language": language,
        "boundary": secrets.token_hex(16),
        "prompt": "",
        "system_prompt_file": system_prompt_file,
    }
    return values


def _build_invocation(
    preset: PostprocessorPreset,
    transcript: str,
    profile: Profile | None,
    language: str,
    *,
    system_prompt_file: str = "",
    agent: str = "",
) -> tuple[list[str], str]:
    values = _template_values(
        transcript,
        profile,
        language,
        system_prompt_file,
        agent,
    )
    prompt = _render(preset.prompt_template, values)
    values["prompt"] = prompt
    argv = [_render(argument, values) for argument in split_command(preset.command)]
    return argv, prompt


def _write_system_prompt_file(temp_dir: str, system_prompt: str) -> str:
    path = Path(temp_dir) / "system-prompt.txt"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(system_prompt)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return str(path)


def _write_private_json(path: Path, value: object) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _prepare_kiro_agent(
    temp_dir: str,
    preset: PostprocessorPreset,
) -> tuple[str, str]:
    """Create a tool-less, per-run Kiro home and custom agent."""
    kiro_home = Path(temp_dir) / "kiro-home"
    agents_dir = kiro_home / "agents"
    settings_dir = kiro_home / "settings"
    agents_dir.mkdir(parents=True, mode=0o700)
    settings_dir.mkdir(mode=0o700)
    _write_private_json(
        settings_dir / "cli.json",
        {"chat.disableInheritingDefaultResources": True},
    )
    _write_private_json(
        agents_dir / f"{_KIRO_AGENT_ID}.json",
        {
            "name": _KIRO_AGENT_ID,
            "description": "Ephemeral ZenWhisper transcript proofreader",
            "prompt": preset.system_prompt,
            "model": preset.model,
            "tools": [],
            "allowedTools": [],
            "resources": [],
            "mcpServers": {},
            "includeMcpJson": False,
        },
    )
    return _KIRO_AGENT_ID, str(kiro_home)


@contextmanager
def _temporary_postprocess_directory(preset_id: str):
    temp_dir = tempfile.mkdtemp(prefix="zen_whisper_postprocess_")
    try:
        yield temp_dir
    finally:
        try:
            shutil.rmtree(temp_dir)
        except OSError as exc:
            logger.warning(
                "後処理の一時ディレクトリを削除できませんでした: "
                "preset=%s type=%s",
                preset_id,
                type(exc).__name__,
            )


def _process_environment(
    preset: PostprocessorPreset,
    *,
    kiro_home: str = "",
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(preset.environment)
    if preset.adapter == ADAPTER_KIRO:
        # Adapter-owned isolation cannot be overridden by a preset environment.
        environment["KIRO_HOME"] = kiro_home
        environment["KIRO_LOG_NO_COLOR"] = "1"
        environment["NO_COLOR"] = "1"
    return environment


def _finish_timed_out_process(
    process: subprocess.Popen[str],
    preset_id: str,
) -> None:
    """Bound all cleanup waits after terminating a timed-out process tree."""
    try:
        terminate_process_tree(process)
    except Exception as exc:
        logger.warning(
            "プロセスツリー終了処理に失敗しました: preset=%s type=%s",
            preset_id,
            type(exc).__name__,
        )
    try:
        process.communicate(timeout=5.0)
        return
    except Exception as exc:
        logger.warning(
            "タイムアウト後のプロセス回収を継続します: preset=%s type=%s",
            preset_id,
            type(exc).__name__,
        )

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        logger.warning(
            "タイムアウト後も直接プロセスを回収できませんでした: preset=%s",
            preset_id,
        )
    except Exception as exc:
        logger.warning(
            "タイムアウト後の直接プロセス待機に失敗しました: preset=%s type=%s",
            preset_id,
            type(exc).__name__,
        )
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


def _run_preflight(
    preset: PostprocessorPreset,
    cwd: str,
    environment: dict[str, str],
    start_guard: StateGuard | None = None,
    on_process_started: ProcessCallback | None = None,
    on_process_finished: ProcessCallback | None = None,
) -> str:
    if not preset.preflight_command:
        return ""

    process: subprocess.Popen[str] | None = None
    guard = start_guard() if start_guard is not None else nullcontext(True)
    with guard as authorized:
        if not authorized:
            return "後処理設定が変更されたためCLI実行をキャンセルしました"
        try:
            process = subprocess.Popen(
                split_command(preset.preflight_command),
                shell=False,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **subprocess_run_options(),
            )
        except FileNotFoundError:
            return f"{preset.display_name} の実行ファイルが見つかりません"
        except Exception as exc:
            logger.warning(
                "後処理の事前確認で例外が発生しました: preset=%s type=%s",
                preset.preset_id,
                type(exc).__name__,
            )
            return f"{preset.display_name} の事前確認に失敗しました"
        if on_process_started is not None:
            on_process_started(process)

    try:
        try:
            returncode = process.wait(timeout=min(preset.timeout_sec, 10.0))
        except subprocess.TimeoutExpired:
            _finish_timed_out_process(process, preset.preset_id)
            return f"{preset.display_name} の事前確認がタイムアウトしました"
    finally:
        if on_process_finished is not None:
            on_process_finished(process)

    if returncode != 0:
        return preset.preflight_failure_message or (
            f"{preset.display_name} の事前確認に失敗しました"
        )
    return ""


def _collect_kiro_model_ids(value: object) -> set[str]:
    model_ids: set[str] = set()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                model_ids.add(item)
            else:
                model_ids.update(_collect_kiro_model_ids(item))
        return model_ids
    if not isinstance(value, dict):
        return model_ids
    for key, item in value.items():
        normalized_key = str(key).replace("_", "").lower()
        if normalized_key in {"id", "modelid"} and isinstance(item, str):
            model_ids.add(item)
        elif isinstance(item, (dict, list)):
            model_ids.update(_collect_kiro_model_ids(item))
    return model_ids


def _kiro_stderr_reports_fallback(stderr: str) -> bool:
    normalized = " ".join(stderr.lower().split())
    fallback_mentioned = any(
        phrase in normalized
        for phrase in ("fallback", "fall back", "falling back", "fell back")
    )
    default_agent_selected = "default agent" in normalized and any(
        phrase in normalized
        for phrase in (
            "using default agent",
            "use default agent",
            "selected default agent",
            "switching to default agent",
            "switched to default agent",
        )
    )
    return default_agent_selected or (
        fallback_mentioned
        and ("model" in normalized or "agent" in normalized)
    )


def _run_kiro_agent_validation(
    preset: PostprocessorPreset,
    cwd: str,
    environment: dict[str, str],
    agent_path: str,
    start_guard: StateGuard | None = None,
    on_process_started: ProcessCallback | None = None,
    on_process_finished: ProcessCallback | None = None,
) -> str:
    executable = split_command(preset.command)[0]
    process: subprocess.Popen[str] | None = None
    guard = start_guard() if start_guard is not None else nullcontext(True)
    with guard as authorized:
        if not authorized:
            return "後処理設定が変更されたためCLI実行をキャンセルしました"
        try:
            process = subprocess.Popen(
                [executable, "agent", "validate", agent_path],
                shell=False,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **subprocess_run_options(),
            )
        except FileNotFoundError:
            return f"{preset.display_name} の実行ファイルが見つかりません"
        except Exception as exc:
            logger.warning(
                "Kiro agent検証を開始できませんでした: preset=%s type=%s",
                preset.preset_id,
                type(exc).__name__,
            )
            return f"{preset.display_name} のagent検証に失敗しました"
        if on_process_started is not None:
            on_process_started(process)

    try:
        try:
            returncode = process.wait(timeout=min(preset.timeout_sec, 10.0))
        except subprocess.TimeoutExpired:
            _finish_timed_out_process(process, preset.preset_id)
            return f"{preset.display_name} のagent検証がタイムアウトしました"
    finally:
        if on_process_finished is not None:
            on_process_finished(process)

    if returncode != 0:
        return f"{preset.display_name} の一時agent設定を検証できませんでした"
    return ""


def _run_kiro_model_check(
    preset: PostprocessorPreset,
    cwd: str,
    environment: dict[str, str],
    start_guard: StateGuard | None = None,
    on_process_started: ProcessCallback | None = None,
    on_process_finished: ProcessCallback | None = None,
) -> str:
    executable = split_command(preset.command)[0]
    process: subprocess.Popen[str] | None = None
    guard = start_guard() if start_guard is not None else nullcontext(True)
    with guard as authorized:
        if not authorized:
            return "後処理設定が変更されたためCLI実行をキャンセルしました"
        try:
            process = subprocess.Popen(
                [executable, "chat", "--list-models", "--format", "json"],
                shell=False,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **subprocess_run_options(),
            )
        except FileNotFoundError:
            return f"{preset.display_name} の実行ファイルが見つかりません"
        except Exception as exc:
            logger.warning(
                "Kiroモデル一覧の取得を開始できませんでした: "
                "preset=%s type=%s",
                preset.preset_id,
                type(exc).__name__,
            )
            return f"{preset.display_name} のモデル確認に失敗しました"
        if on_process_started is not None:
            on_process_started(process)

    try:
        try:
            stdout, stderr = process.communicate(
                timeout=min(preset.timeout_sec, 10.0),
            )
        except subprocess.TimeoutExpired:
            _finish_timed_out_process(process, preset.preset_id)
            return f"{preset.display_name} のモデル確認がタイムアウトしました"
        except Exception as exc:
            logger.warning(
                "Kiroモデル一覧の取得に失敗しました: preset=%s type=%s",
                preset.preset_id,
                type(exc).__name__,
            )
            return f"{preset.display_name} のモデル確認に失敗しました"
    finally:
        if on_process_finished is not None:
            on_process_finished(process)

    if process.returncode != 0:
        logger.warning(
            "Kiroモデル一覧の取得が失敗しました: "
            "preset=%s returncode=%d stderr_chars=%d",
            preset.preset_id,
            process.returncode,
            len(stderr),
        )
        return preset.preflight_failure_message or (
            f"{preset.display_name} のモデル確認に失敗しました"
        )
    try:
        available_models = _collect_kiro_model_ids(json.loads(stdout))
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning(
            "Kiroモデル一覧をJSONとして解釈できませんでした: preset=%s",
            preset.preset_id,
        )
        return f"{preset.display_name} のモデル確認に失敗しました"
    if preset.model not in available_models:
        logger.warning(
            "指定Kiroモデルを利用できません: preset=%s model=%s",
            preset.preset_id,
            preset.model,
        )
        return f"Kiroモデル {preset.model} を現在のアカウントで利用できません"
    return ""


def run_postprocessor(
    preset: PostprocessorPreset,
    transcript: str,
    profile: Profile | None,
    language: str,
    start_guard: StateGuard | None = None,
    finish_guard: StateGuard | None = None,
    on_process_started: ProcessCallback | None = None,
    on_process_finished: ProcessCallback | None = None,
) -> PostprocessResult:
    """Run one trusted preset without a shell and return stdout as corrected text."""
    with _temporary_postprocess_directory(preset.preset_id) as temp_dir:
        system_prompt_file = ""
        agent = ""
        kiro_home = ""
        try:
            if preset.adapter == ADAPTER_KIRO:
                agent, kiro_home = _prepare_kiro_agent(temp_dir, preset)
            elif preset.system_prompt:
                system_prompt_file = _write_system_prompt_file(
                    temp_dir,
                    preset.system_prompt,
                )
        except OSError as exc:
            logger.warning(
                "後処理system promptの準備に失敗しました: "
                "preset=%s type=%s",
                preset.preset_id,
                type(exc).__name__,
            )
            return PostprocessResult(
                text=transcript,
                succeeded=False,
                applied=True,
                error=f"{preset.display_name} の実行準備に失敗しました",
            )
        environment = _process_environment(preset, kiro_home=kiro_home)
        if (
            preset.adapter == ADAPTER_KIRO
            and not environment.get("KIRO_API_KEY", "").strip()
        ):
            return PostprocessResult(
                text=transcript,
                succeeded=False,
                applied=True,
                error=(
                    preset.preflight_failure_message
                    or "Kiro CLI の非対話実行には KIRO_API_KEY が必要です"
                ),
            )
        argv, prompt = _build_invocation(
            preset,
            transcript,
            profile,
            language,
            system_prompt_file=system_prompt_file,
            agent=agent,
        )
        if (
            preset.input_mode == "argument"
            and command_uses_windows_batch(argv[0], environment)
        ):
            return PostprocessResult(
                text=transcript,
                succeeded=False,
                applied=True,
                error=(
                    f"{preset.display_name} は Windows の .cmd/.bat 経由では"
                    " argument モードを安全に実行できません"
                ),
            )
        preflight_error = _run_preflight(
            preset,
            temp_dir,
            environment,
            start_guard=start_guard,
            on_process_started=on_process_started,
            on_process_finished=on_process_finished,
        )
        if preflight_error:
            logger.warning("後処理の事前確認に失敗しました: preset=%s", preset.preset_id)
            return PostprocessResult(
                text=transcript,
                succeeded=False,
                applied=True,
                error=preflight_error,
            )
        if preset.adapter == ADAPTER_KIRO:
            agent_error = _run_kiro_agent_validation(
                preset,
                temp_dir,
                environment,
                str(Path(kiro_home) / "agents" / f"{agent}.json"),
                start_guard=start_guard,
                on_process_started=on_process_started,
                on_process_finished=on_process_finished,
            )
            if agent_error:
                logger.warning(
                    "Kiro agent検証に失敗しました: preset=%s",
                    preset.preset_id,
                )
                return PostprocessResult(
                    text=transcript,
                    succeeded=False,
                    applied=True,
                    error=agent_error,
                )
            model_error = _run_kiro_model_check(
                preset,
                temp_dir,
                environment,
                start_guard=start_guard,
                on_process_started=on_process_started,
                on_process_finished=on_process_finished,
            )
            if model_error:
                logger.warning(
                    "Kiroモデル確認に失敗しました: preset=%s",
                    preset.preset_id,
                )
                return PostprocessResult(
                    text=transcript,
                    succeeded=False,
                    applied=True,
                    error=model_error,
                )

        process: subprocess.Popen[str] | None = None
        guard = start_guard() if start_guard is not None else nullcontext(True)
        with guard as authorized:
            if not authorized:
                return PostprocessResult(
                    text=transcript,
                    succeeded=False,
                    applied=True,
                    error="後処理設定が変更されたためCLI実行をキャンセルしました",
                )
            stdin = (
                subprocess.PIPE
                if preset.input_mode == "stdin"
                else subprocess.DEVNULL
            )
            try:
                process = subprocess.Popen(
                    argv,
                    shell=False,
                    cwd=temp_dir,
                    env=environment,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **subprocess_run_options(),
                )
            except FileNotFoundError:
                error = f"{preset.display_name} の実行ファイルが見つかりません"
            except Exception as exc:
                logger.warning(
                    "後処理コマンドで例外が発生しました: preset=%s type=%s",
                    preset.preset_id,
                    type(exc).__name__,
                )
                error = f"{preset.display_name} の実行に失敗しました"
            else:
                if on_process_started is not None:
                    on_process_started(process)

        successful_output = ""
        if process is not None:
            run_input = prompt if preset.input_mode == "stdin" else None
            try:
                try:
                    stdout, stderr = process.communicate(
                        input=run_input,
                        timeout=preset.timeout_sec,
                    )
                except subprocess.TimeoutExpired:
                    _finish_timed_out_process(process, preset.preset_id)
                    error = (
                        f"{preset.display_name} が "
                        f"{preset.timeout_sec:g}秒でタイムアウトしました"
                    )
                except Exception as exc:
                    logger.warning(
                        "後処理コマンドの通信に失敗しました: preset=%s type=%s",
                        preset.preset_id,
                        type(exc).__name__,
                    )
                    error = f"{preset.display_name} の実行に失敗しました"
                else:
                    if process.returncode != 0:
                        logger.warning(
                            "後処理コマンドが失敗しました: "
                            "preset=%s returncode=%d stderr_chars=%d",
                            preset.preset_id,
                            process.returncode,
                            len(stderr),
                        )
                        error = (
                            f"{preset.display_name} が終了コード "
                            f"{process.returncode} で失敗しました"
                        )
                    elif (
                        preset.adapter == ADAPTER_KIRO
                        and _kiro_stderr_reports_fallback(stderr)
                    ):
                        logger.warning(
                            "Kiro CLIが警告を出力したため結果を破棄しました: "
                            "preset=%s stderr_chars=%d",
                            preset.preset_id,
                            len(stderr),
                        )
                        error = (
                            "Kiro CLIが指定したagentまたはモデルを"
                            "使用できなかった可能性があります"
                        )
                    else:
                        output = _UNSAFE_OUTPUT_CONTROL_RE.sub(
                            " ",
                            stdout,
                        ).strip()
                        if output:
                            logger.info(
                                "後処理が完了しました: "
                                "preset=%s input_chars=%d output_chars=%d",
                                preset.preset_id,
                                len(transcript),
                                len(output),
                            )
                            successful_output = output
                        else:
                            error = f"{preset.display_name} の出力が空でした"
            finally:
                if on_process_finished is not None:
                    on_process_finished(process)

        if successful_output:
            guard = (
                finish_guard()
                if finish_guard is not None
                else nullcontext(True)
            )
            with guard as authorized:
                if authorized:
                    return PostprocessResult(
                        text=successful_output,
                        succeeded=True,
                        applied=True,
                    )
            error = "後処理設定が変更されたためCLIの結果を破棄しました"

    return PostprocessResult(
        text=transcript,
        succeeded=False,
        applied=True,
        error=error,
    )


def process_transcript(
    text: str,
    profile: Profile | None,
    postprocessor_id: str,
    presets: dict[str, PostprocessorPreset],
    language: str,
    start_guard: StateGuard | None = None,
    finish_guard: StateGuard | None = None,
    on_process_started: ProcessCallback | None = None,
    on_process_finished: ProcessCallback | None = None,
) -> PostprocessResult:
    """Apply explicit replacements, then optionally invoke a command preset."""
    if postprocessor_id == POSTPROCESSOR_OFF:
        return PostprocessResult(text=text, succeeded=True, applied=False)

    corrected = apply_replacements(text, profile)
    if postprocessor_id == POSTPROCESSOR_DICTIONARY:
        return PostprocessResult(
            text=corrected,
            succeeded=True,
            applied=corrected != text,
        )

    preset = presets.get(postprocessor_id)
    if preset is None:
        return PostprocessResult(
            text=corrected,
            succeeded=False,
            applied=True,
            error=f"後処理プリセット '{postprocessor_id}' が見つかりません",
        )

    result = run_postprocessor(
        preset,
        corrected,
        profile,
        language,
        start_guard=start_guard,
        finish_guard=finish_guard,
        on_process_started=on_process_started,
        on_process_finished=on_process_finished,
    )
    if result.succeeded:
        return result
    return PostprocessResult(
        text=corrected,
        succeeded=False,
        applied=True,
        error=result.error,
    )

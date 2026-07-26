"""Structured settings helpers that do not require a visible Tk window."""

from __future__ import annotations

import copy
import tkinter as tk
from dataclasses import fields
from tkinter import ttk

import pytest

from src.config import AppConfig
from src.postprocessing import (
    PostprocessorPreset,
    SUPPORTED_TEMPLATE_PLACEHOLDERS,
)
from src.profiles import Profile
from src.settings import (
    CONFIG_FORM_FIELDS,
    DISPLAY_DEFAULT_MICROPHONE,
    DISPLAY_DISABLED,
    POSTPROCESSOR_PLACEHOLDER_HELP,
    READ_ONLY_CONFIG_FIELDS,
    SettingsSnapshot,
    SettingsWindow,
    apply_changed_config_fields,
    changed_config_fields,
    environment_to_text,
    hotkey_value_to_text,
    lines_to_tuple,
    local_transport_changed,
    parse_environment,
    parse_hotkey_value,
    recognition_selection_text,
    reclassify_changed_local_preset,
    refresh_snapshot_config,
    refresh_snapshot_resources,
)
from src.toml_storage import FileFingerprint


class _Variable:
    def __init__(self, value: object = "") -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class _Text:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self, start: str, end: str) -> str:
        return self.value

    def delete(self, start: str, end: str) -> None:
        self.value = ""

    def insert(self, index: str, value: str) -> None:
        self.value = value


class _Button:
    def __init__(self) -> None:
        self.state = "normal"

    def configure(self, **kwargs: object) -> None:
        if "state" in kwargs:
            self.state = str(kwargs["state"])


class _Root:
    def __init__(self) -> None:
        self.window_state = "normal"
        self.calls: list[str] = []

    def state(self) -> str:
        return self.window_state

    def withdraw(self) -> None:
        self.window_state = "withdrawn"
        self.calls.append("withdraw")

    def deiconify(self) -> None:
        self.window_state = "normal"
        self.calls.append("deiconify")

    def lift(self) -> None:
        self.calls.append("lift")

    def focus_force(self) -> None:
        self.calls.append("focus")


def _stateful_settings_window(
    snapshot: SettingsSnapshot,
    *,
    snapshot_provider=None,
    on_save_config=None,
) -> SettingsWindow:
    window = SettingsWindow(
        snapshot_provider=snapshot_provider or (lambda: snapshot),
        on_save_config=on_save_config
        or (lambda cfg, revision, fingerprint: (True, "")),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    window._snapshot = snapshot
    window._profile_id_by_label = {"オフ": ""}
    window._profile_label_by_id = {"": "オフ"}
    window._postprocessor_id_by_label = {
        "オフ": "off",
        "辞書置換のみ": "dictionary",
    }
    window._postprocessor_label_by_id = {
        "off": "オフ",
        "dictionary": "辞書置換のみ",
    }
    window._vars = {
        "output.paste_delay_ms": _Variable(
            str(snapshot.config.output.paste_delay_ms)
        ),
        "enhancement.profile_label": _Variable("オフ"),
        "profile.editor": _Variable("profile"),
        "profile.id": _Variable("profile"),
        "profile.name": _Variable("Saved profile"),
        "enhancement.postprocessor_label": _Variable("オフ"),
        "postprocessor.editor": _Variable("cli"),
        "postprocessor.id": _Variable("cli"),
        "postprocessor.name": _Variable("Saved CLI"),
        "postprocessor.destination": _Variable("unknown"),
        "postprocessor.input_mode": _Variable("stdin"),
        "postprocessor.timeout": _Variable("30"),
        "postprocessor.preflight": _Variable(""),
        "postprocessor.preflight_message": _Variable(""),
    }
    window._profile_context = _Text("saved context")
    window._profile_terms = []
    window._command_text = _Text("old-cli")
    window._environment_text = _Text("")
    window._prompt_text = _Text("{{transcript}}")
    window._loaded_profile_id = "profile"
    window._loaded_postprocessor_id = "cli"
    window._profile_editor_baseline = window._capture_profile_editor_state()
    window._postprocessor_editor_baseline = (
        window._capture_postprocessor_editor_state()
    )
    window._config_form_baseline = window._capture_config_form_state()
    window._config_source_var = _Variable()
    window._config_changes_var = _Variable()
    window._status_var = _Variable()
    window._save_config_button = _Button()
    window._configure_recognition_choices = lambda **kwargs: None
    window._update_recognition_summary = lambda: None
    return window


def _make_all_settings_dirty(window: SettingsWindow) -> None:
    window._vars["output.paste_delay_ms"].set("250")
    window._vars["profile.name"].set("Unsaved profile")
    assert window._profile_context is not None
    window._profile_context.value = "unsaved context"
    assert window._command_text is not None
    window._command_text.value = "edited-cli"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("shift+space", "shift+space"),
        ("ctrl+space, shift+space", ["ctrl+space", "shift+space"]),
        ("ctrl+space\nshift+space", ["ctrl+space", "shift+space"]),
        ("  ", ""),
    ],
)
def test_parse_hotkey_value(raw, expected):
    assert parse_hotkey_value(raw) == expected


def test_hotkey_value_to_text_keeps_multiple_bindings_editable():
    assert hotkey_value_to_text(["ctrl+space", "shift+space"]) == (
        "ctrl+space, shift+space"
    )


def test_hotkey_value_to_text_keeps_invalid_list_repairable():
    assert hotkey_value_to_text([1, "ctrl+space"]) == "1, ctrl+space"


def test_recognition_selection_text_makes_current_values_explicit():
    assert recognition_selection_text("whisper", "ja", "cuda") == (
        "現在の選択: Whisper / 日本語 (ja) / GPU (CUDA)"
    )


def test_postprocessor_placeholder_help_covers_renderer_contract():
    help_by_name = {
        name: (destination, description)
        for name, destination, description in POSTPROCESSOR_PLACEHOLDER_HELP
    }

    assert set(help_by_name) == set(SUPPORTED_TEMPLATE_PLACEHOLDERS)
    assert help_by_name["transcript"][0] == "prompt"
    assert "必須" in help_by_name["transcript"][1]
    assert help_by_name["prompt"][0] == "command"
    assert "argument" in help_by_name["prompt"][1]


def test_postprocessor_placeholder_button_inserts_into_target_editor():
    class Editor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str] | str] = []

        def insert(self, index: str, value: str) -> None:
            self.calls.append((index, value))

        def see(self, index: str) -> None:
            self.calls.append(index)

        def focus_set(self) -> None:
            self.calls.append("focus")

    prompt_editor = Editor()
    command_editor = Editor()
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda value, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    window._prompt_text = prompt_editor
    window._command_text = command_editor

    window._insert_postprocessor_placeholder("transcript", "prompt")
    window._insert_postprocessor_placeholder("prompt", "command")

    assert prompt_editor.calls == [
        ("insert", "{{transcript}}"),
        "insert",
        "focus",
    ]
    assert command_editor.calls == [
        ("insert", "{{prompt}}"),
        "insert",
        "focus",
    ]


def test_changed_config_fields_reports_only_managed_form_changes_in_order():
    baseline = {
        "hotkey.toggle": "shift+space",
        "output.paste_delay_ms": "100",
        "recording.microphone": "",
    }
    current = {
        **baseline,
        "hotkey.toggle": "win+j",
        "recording.microphone": "USB microphone",
        "unmanaged": "ignored",
    }

    assert changed_config_fields(baseline, current) == (
        "hotkey.toggle",
        "recording.microphone",
    )


def test_settings_form_represents_every_app_config_field():
    cfg = AppConfig()
    config_fields = {
        (section.name, item.name)
        for section in fields(cfg)
        for item in fields(getattr(cfg, section.name))
    }
    editable_fields = {
        (section_name, field_name)
        for section_name, field_name, _label in CONFIG_FORM_FIELDS.values()
    }

    assert config_fields == editable_fields | READ_ONLY_CONFIG_FIELDS
    assert not editable_fields & READ_ONLY_CONFIG_FIELDS


def test_apply_changed_config_fields_preserves_untouched_optional_values():
    baseline = AppConfig()
    baseline.recording.microphone = "USB microphone"
    parsed = copy.deepcopy(baseline)
    parsed.output.paste_delay_ms = 250
    parsed.recording.microphone = ""

    merged = apply_changed_config_fields(
        baseline,
        parsed,
        ("output.paste_delay_ms",),
    )

    assert merged.output.paste_delay_ms == 250
    assert merged.recording.microphone == "USB microphone"
    assert baseline.output.paste_delay_ms == 100


def test_apply_changed_config_fields_updates_enhancement_selection():
    baseline = AppConfig()
    parsed = copy.deepcopy(baseline)
    parsed.enhancement.profile = "zen-whisper"
    parsed.enhancement.postprocessor = "ollama"

    merged = apply_changed_config_fields(
        baseline,
        parsed,
        (
            "enhancement.profile_label",
            "enhancement.postprocessor_label",
        ),
    )

    assert merged.enhancement.profile == "zen-whisper"
    assert merged.enhancement.postprocessor == "ollama"


def test_lines_to_tuple_strips_empties_and_duplicates():
    assert lines_to_tuple(" ZenWhisper \n\nOllama\nZenWhisper\n") == (
        "ZenWhisper",
        "Ollama",
    )


def test_parse_environment_supports_comments_and_equals_in_values():
    assert parse_environment(
        "# local model\nOLLAMA_MODEL=gemma\nURL=http://localhost?a=b\n"
    ) == {
        "OLLAMA_MODEL": "gemma",
        "URL": "http://localhost?a=b",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "MISSING_SEPARATOR",
        "=missing-key",
        "MODEL=one\nMODEL=two",
    ],
)
def test_parse_environment_rejects_ambiguous_lines(raw):
    with pytest.raises(ValueError):
        parse_environment(raw)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_settings_float_parser_rejects_non_finite_values(raw):
    with pytest.raises(ValueError, match="有限"):
        SettingsWindow._parse_float(raw, "値")


def test_environment_round_trip():
    environment = {"OLLAMA_HOST": "http://localhost:11434", "MODEL": "gemma"}

    assert parse_environment(environment_to_text(environment)) == environment


def test_local_transport_change_requires_destination_reclassification():
    original = PostprocessorPreset(
        preset_id="ollama",
        display_name="Ollama",
        command="ollama run local",
        data_destination="local",
        environment={"OLLAMA_HOST": "http://127.0.0.1:11434"},
    )

    changed_command = PostprocessorPreset(
        **{
            **original.__dict__,
            "command": "remote-cli",
        }
    )
    changed_environment = PostprocessorPreset(
        **{
            **original.__dict__,
            "environment": {"OLLAMA_HOST": "https://remote.example"},
        }
    )
    prompt_only = PostprocessorPreset(
        **{
            **original.__dict__,
            "prompt_template": "Fix {{transcript}}",
        }
    )

    assert local_transport_changed(original, changed_command) is True
    assert local_transport_changed(original, changed_environment) is True
    assert local_transport_changed(original, prompt_only) is False


def test_reclassifying_changed_command_preserves_new_dependent_values():
    original = PostprocessorPreset(
        preset_id="local",
        display_name="Local",
        command="old-cli",
        data_destination="local",
        preflight_command="old-cli --version",
        preflight_failure_message="old failure",
        environment={"MODEL": "old"},
    )
    updated = PostprocessorPreset(
        **{
            **original.__dict__,
            "command": "new-cli",
            "preflight_command": "new-cli --version",
            "preflight_failure_message": "new failure",
            "environment": {"MODEL": "new"},
        }
    )

    reclassified, cleared = reclassify_changed_local_preset(
        original,
        updated,
    )

    assert reclassified.data_destination == "unknown"
    assert reclassified.preflight_command == "new-cli --version"
    assert reclassified.preflight_failure_message == "new failure"
    assert reclassified.environment == {"MODEL": "new"}
    assert cleared == ()


def test_reclassifying_changed_command_clears_unchanged_old_dependencies():
    original = PostprocessorPreset(
        preset_id="local",
        display_name="Local",
        command="old-cli",
        data_destination="local",
        preflight_command="old-cli --version",
        preflight_failure_message="old failure",
        environment={"MODEL": "old"},
    )
    updated = PostprocessorPreset(
        **{
            **original.__dict__,
            "command": "new-cli",
        }
    )

    reclassified, cleared = reclassify_changed_local_preset(
        original,
        updated,
    )

    assert reclassified.preflight_command == ""
    assert reclassified.preflight_failure_message == ""
    assert reclassified.environment == {}
    assert cleared == (
        "事前確認コマンド",
        "事前確認の失敗メッセージ",
        "環境変数",
    )


def test_resource_refresh_keeps_form_config_and_revision_stale():
    old_config_fingerprint = FileFingerprint("file", digest="old-config")
    new_config_fingerprint = FileFingerprint("file", digest="new-config")
    new_profile_fingerprint = FileFingerprint("file", digest="new-profile")
    new_postprocessor_fingerprint = FileFingerprint(
        "file",
        digest="new-postprocessors",
    )
    previous_cfg = AppConfig()
    previous_cfg.enhancement.postprocessor = "remote"
    previous = SettingsSnapshot(
        config=previous_cfg,
        profiles={"old": Profile(profile_id="old", name="Old")},
        postprocessors={},
        revision=4,
        config_fingerprint=old_config_fingerprint,
    )
    current_cfg = AppConfig()
    current_cfg.enhancement.postprocessor = "off"
    refreshed = SettingsSnapshot(
        config=current_cfg,
        profiles={"new": Profile(profile_id="new", name="New")},
        postprocessors={
            "local": PostprocessorPreset(
                preset_id="local",
                display_name="Local",
                command="local-cli",
            )
        },
        local_postprocessor_ids=frozenset({"local"}),
        revision=5,
        config_fingerprint=new_config_fingerprint,
        profile_fingerprints={"new": new_profile_fingerprint},
        postprocessors_fingerprint=new_postprocessor_fingerprint,
    )

    combined = refresh_snapshot_resources(previous, refreshed)

    assert combined.config is previous_cfg
    assert combined.config.enhancement.postprocessor == "remote"
    assert combined.revision == 4
    assert combined.profiles == refreshed.profiles
    assert combined.postprocessors == refreshed.postprocessors
    assert combined.local_postprocessor_ids == frozenset({"local"})
    assert combined.config_fingerprint == old_config_fingerprint
    assert combined.profile_fingerprints == {
        "new": new_profile_fingerprint
    }
    assert (
        combined.postprocessors_fingerprint
        == new_postprocessor_fingerprint
    )


def test_config_refresh_keeps_resource_editor_snapshots():
    old_profile_fingerprint = FileFingerprint("file", digest="old-profile")
    old_postprocessor_fingerprint = FileFingerprint(
        "file",
        digest="old-postprocessors",
    )
    new_config_fingerprint = FileFingerprint("file", digest="new-config")
    previous = SettingsSnapshot(
        config=AppConfig(),
        profiles={"old": Profile(profile_id="old", name="Old")},
        postprocessors={
            "local": PostprocessorPreset(
                preset_id="local",
                display_name="Local",
                command="local-cli",
            )
        },
        local_postprocessor_ids=frozenset({"local"}),
        revision=4,
        profile_fingerprints={"old": old_profile_fingerprint},
        postprocessors_fingerprint=old_postprocessor_fingerprint,
    )
    refreshed_config = AppConfig()
    refreshed_config.output.paste_delay_ms = 275
    refreshed = SettingsSnapshot(
        config=refreshed_config,
        profiles={"new": Profile(profile_id="new", name="New")},
        postprocessors={},
        revision=5,
        config_fingerprint=new_config_fingerprint,
    )

    combined = refresh_snapshot_config(previous, refreshed)

    assert combined.config is refreshed_config
    assert combined.revision == 5
    assert combined.config_fingerprint == new_config_fingerprint
    assert combined.profiles is previous.profiles
    assert combined.postprocessors is previous.postprocessors
    assert combined.local_postprocessor_ids == frozenset({"local"})
    assert combined.profile_fingerprints == {
        "old": old_profile_fingerprint
    }
    assert combined.postprocessors_fingerprint == old_postprocessor_fingerprint


def test_invalid_hotkey_snapshot_still_loads_with_repair_status():
    cfg = AppConfig()
    cfg.hotkey.toggle = [1]
    snapshot = SettingsSnapshot(cfg, {}, {})
    window = SettingsWindow(
        snapshot_provider=lambda: snapshot,
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    statuses: list[str] = []
    window._load_config_variables = lambda value: None
    window._refresh_profile_choices = lambda value: None
    window._refresh_postprocessor_choices = lambda value: None
    window._set_status = statuses.append

    window._refresh_snapshot()

    assert window._snapshot is snapshot
    assert "ホットキー設定を修正" in statuses[-1]
    assert "型が不正" in statuses[-1]


def test_config_loader_makes_empty_meanings_and_fixed_values_visible():
    cfg = AppConfig()
    cfg.hotkey.submit_toggle = ""
    cfg.recording.microphone = ""
    cfg.recognition.hallucination_silence_threshold = None
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(cfg, {}, {}),
        on_save_config=lambda value, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )

    class Variable:
        def __init__(self) -> None:
            self.value: object = None

        def set(self, value: object) -> None:
            self.value = value

    keys = (
        "hotkey.submit_toggle",
        "recording.microphone",
        "recording.sample_rate",
        "recognition.hallucination_silence_threshold",
        "overlay.position",
        "overlay.size",
    )
    variables = {key: Variable() for key in keys}
    window._vars = variables
    window._configure_recognition_choices = lambda **kwargs: None

    window._load_config_variables(cfg)

    assert variables["hotkey.submit_toggle"].value == DISPLAY_DISABLED
    assert variables["recording.microphone"].value == (
        DISPLAY_DEFAULT_MICROPHONE
    )
    assert variables["recording.sample_rate"].value == "16000"
    assert variables[
        "recognition.hallucination_silence_threshold"
    ].value == DISPLAY_DISABLED
    assert variables["overlay.position"].value == "bottom-center"
    assert variables["overlay.size"].value == "48"


def test_config_from_form_round_trips_explicit_empty_meanings():
    interpreter = tk.Tcl()
    cfg = AppConfig()
    cfg.hotkey.submit_toggle = ""
    cfg.recording.microphone = ""
    cfg.recognition.hallucination_silence_threshold = None
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(cfg, {}, {}),
        on_save_config=lambda value, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    window._snapshot = SettingsSnapshot(cfg, {}, {})
    for key, (section_name, field_name, _label) in (
        CONFIG_FORM_FIELDS.items()
    ):
        field_value = getattr(getattr(cfg, section_name), field_name)
        variable_type = (
            tk.BooleanVar if isinstance(field_value, bool) else tk.StringVar
        )
        window._vars[key] = variable_type(master=interpreter)
    window._profile_id_by_label = {"オフ": ""}
    window._profile_label_by_id = {"": "オフ"}
    window._postprocessor_id_by_label = {
        "オフ": "off",
        "辞書置換のみ": "dictionary",
    }
    window._postprocessor_label_by_id = {
        "off": "オフ",
        "dictionary": "辞書置換のみ",
    }
    window._configure_recognition_choices = lambda **kwargs: None
    window._load_config_variables(cfg)
    window._vars["enhancement.profile_label"].set("オフ")
    window._vars["enhancement.postprocessor_label"].set("オフ")
    window._config_form_baseline = window._capture_config_form_state()

    window._vars["output.paste_delay_ms"].set("225")
    parsed = window._config_from_form()

    assert parsed.output.paste_delay_ms == 225
    assert parsed.hotkey.submit_toggle == ""
    assert parsed.recording.microphone == ""
    assert parsed.recognition.hallucination_silence_threshold is None


def test_missing_active_resource_ids_remain_visible_in_config_selection():
    interpreter = tk.Tcl()
    cfg = AppConfig()
    cfg.enhancement.profile = "missing-profile"
    cfg.enhancement.postprocessor = "missing-cli"
    snapshot = SettingsSnapshot(cfg, {}, {})
    window = SettingsWindow(
        snapshot_provider=lambda: snapshot,
        on_save_config=lambda value, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    window._vars = {
        "enhancement.profile_label": tk.StringVar(master=interpreter),
        "profile.editor": tk.StringVar(master=interpreter),
        "enhancement.postprocessor_label": tk.StringVar(
            master=interpreter
        ),
        "postprocessor.editor": tk.StringVar(master=interpreter),
    }
    window._load_profile = lambda profile_id: None
    window._load_postprocessor = lambda preset_id: None

    window._refresh_profile_choices(snapshot)
    window._refresh_postprocessor_choices(snapshot)

    profile_label = window._vars["enhancement.profile_label"].get()
    postprocessor_label = window._vars[
        "enhancement.postprocessor_label"
    ].get()
    assert "missing-profile" in profile_label
    assert "見つかりません" in profile_label
    assert window._profile_id_by_label[profile_label] == "missing-profile"
    assert "missing-cli" in postprocessor_label
    assert "見つかりません" in postprocessor_label
    assert (
        window._postprocessor_id_by_label[postprocessor_label]
        == "missing-cli"
    )


def test_settings_form_loads_snapshot_and_tracks_only_real_edits():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display is unavailable")
    root.withdraw()
    cfg = AppConfig()
    cfg.hotkey.toggle = "win+j"
    cfg.hotkey.submit_toggle = ""
    cfg.recording.microphone = ""
    cfg.output.paste_delay_ms = 175
    cfg.enhancement.profile = "project"
    snapshot = SettingsSnapshot(
        cfg,
        {
            "project": Profile(
                profile_id="project",
                name="Project vocabulary",
            )
        },
        {},
    )
    window = SettingsWindow(
        snapshot_provider=lambda: snapshot,
        on_save_config=lambda value, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    try:
        window._root = root
        window._config_source_var = tk.StringVar(master=root)
        window._config_changes_var = tk.StringVar(master=root)
        notebook = ttk.Notebook(root)
        window._build_basic_tab(notebook)
        window._build_recognition_tab(notebook)
        window._build_recording_tab(notebook)
        window._build_advanced_tab(notebook)
        window._build_profile_tab(notebook)
        window._build_postprocessor_tab(notebook)
        window._save_config_button = ttk.Button(root)

        window._refresh_snapshot()
        root.update_idletasks()

        assert window._vars["hotkey.toggle"].get() == "win+j"
        assert (
            window._vars["hotkey.submit_toggle"].get()
            == DISPLAY_DISABLED
        )
        assert (
            window._vars["recording.microphone"].get()
            == DISPLAY_DEFAULT_MICROPHONE
        )
        assert window._vars["output.paste_delay_ms"].get() == "175"
        assert window._recognition_summary_var.get() == (
            "現在の選択: Whisper / 日本語 (ja) / GPU (CUDA)"
        )
        assert "project" in window._vars[
            "enhancement.profile_label"
        ].get()
        assert window._config_changed_keys() == ()
        assert window._save_config_button.instate(["disabled"])

        window._vars["output.paste_delay_ms"].set("250")
        root.update_idletasks()

        assert window._config_changed_keys() == (
            "output.paste_delay_ms",
        )
        assert not window._save_config_button.instate(["disabled"])

        window._vars["output.paste_delay_ms"].set("175")
        root.update_idletasks()

        assert window._config_changed_keys() == ()
        assert window._save_config_button.instate(["disabled"])
    finally:
        root.destroy()


def test_settings_variables_belong_to_settings_root_when_another_root_exists(
    monkeypatch,
):
    foreign_root = tk.Tcl()
    settings_root = tk.Tcl()
    monkeypatch.setattr(tk, "_default_root", foreign_root)
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    window._root = settings_root
    string_var = window._new_string_var("test.string")
    bool_var = window._new_bool_var("test.bool")

    assert string_var._tk is settings_root.tk
    assert bool_var._tk is settings_root.tk
    assert string_var._tk is not foreign_root.tk
    assert bool_var._tk is not foreign_root.tk


def test_pending_show_is_consumed_after_delayed_initialization():
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )

    class AliveThread:
        @staticmethod
        def is_alive() -> bool:
            return True

    window._thread = AliveThread()
    window.show()
    assert window._show_requested.is_set()

    shown: list[bool] = []
    window._show_window = lambda: shown.append(True)
    window._show_requested_window()

    assert shown == [True]
    assert not window._show_requested.is_set()


def test_showing_an_open_window_does_not_reload_unsaved_form():
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )

    class Root:
        def __init__(self) -> None:
            self.calls: list[str] = []

        @staticmethod
        def state() -> str:
            return "normal"

        def deiconify(self) -> None:
            self.calls.append("deiconify")

        def lift(self) -> None:
            self.calls.append("lift")

        def focus_force(self) -> None:
            self.calls.append("focus")

    root = Root()
    refreshed: list[bool] = []
    window._root = root
    window._refresh_snapshot = lambda: refreshed.append(True)

    window._show_window()

    assert refreshed == []
    assert root.calls == ["deiconify", "lift", "focus"]


def test_config_save_success_preserves_unsaved_definition_editors(monkeypatch):
    initial_cfg = AppConfig()
    initial_snapshot = SettingsSnapshot(
        initial_cfg,
        {"profile": Profile(profile_id="profile", name="Saved profile")},
        {
            "cli": PostprocessorPreset(
                preset_id="cli",
                display_name="Saved CLI",
                command="old-cli",
            )
        },
        revision=4,
    )
    saved_cfg = copy.deepcopy(initial_cfg)
    saved_cfg.output.paste_delay_ms = 250
    refreshed_snapshot = SettingsSnapshot(
        saved_cfg,
        initial_snapshot.profiles,
        initial_snapshot.postprocessors,
        revision=5,
    )
    save_calls: list[tuple[AppConfig, int, FileFingerprint]] = []

    def save_config(
        cfg: AppConfig,
        revision: int,
        fingerprint: FileFingerprint,
    ) -> tuple[bool, str]:
        save_calls.append((cfg, revision, fingerprint))
        return True, "設定を保存しました"

    window = _stateful_settings_window(
        initial_snapshot,
        snapshot_provider=lambda: refreshed_snapshot,
        on_save_config=save_config,
    )
    _make_all_settings_dirty(window)
    window._config_from_form = lambda changed_keys: saved_cfg
    window._confirm_config_changes = lambda cfg, changed_keys: True
    window._confirm_external_postprocessor = lambda cfg: True
    monkeypatch.setattr(
        "src.settings.messagebox.showinfo",
        lambda *args, **kwargs: None,
    )

    window._save_config()

    assert len(save_calls) == 1
    assert save_calls[0][1] == 4
    assert window._vars["output.paste_delay_ms"].get() == "250"
    assert window._config_changed_keys() == ()
    assert window._vars["profile.name"].get() == "Unsaved profile"
    assert window._profile_context.value == "unsaved context"
    assert window._command_text.value == "edited-cli"
    assert window._profile_editor_changed() is True
    assert window._postprocessor_editor_changed() is True


def test_config_save_failure_keeps_config_and_definition_edits_dirty(
    monkeypatch,
):
    snapshot = SettingsSnapshot(AppConfig(), {}, {}, revision=7)
    window = _stateful_settings_window(
        snapshot,
        on_save_config=lambda cfg, revision, fingerprint: (
            False,
            "保存を拒否しました",
        ),
    )
    _make_all_settings_dirty(window)
    parsed_cfg = copy.deepcopy(snapshot.config)
    parsed_cfg.output.paste_delay_ms = 250
    window._config_from_form = lambda changed_keys: parsed_cfg
    window._confirm_config_changes = lambda cfg, changed_keys: True
    window._confirm_external_postprocessor = lambda cfg: True
    refreshes: list[bool] = []
    window._refresh_config_snapshot = lambda message="": refreshes.append(True)
    monkeypatch.setattr(
        "src.settings.messagebox.showerror",
        lambda *args, **kwargs: None,
    )

    window._save_config()

    assert refreshes == []
    assert window._config_changed_keys() == ("output.paste_delay_ms",)
    assert window._vars["output.paste_delay_ms"].get() == "250"
    assert window._vars["profile.name"].get() == "Unsaved profile"
    assert window._profile_context.value == "unsaved context"
    assert window._command_text.value == "edited-cli"
    assert window._profile_editor_changed() is True
    assert window._postprocessor_editor_changed() is True


def test_discard_config_changes_preserves_unsaved_definition_editors(
    monkeypatch,
):
    initial_cfg = AppConfig()
    current_cfg = copy.deepcopy(initial_cfg)
    current_cfg.output.paste_delay_ms = 175
    initial_snapshot = SettingsSnapshot(
        initial_cfg,
        {"profile": Profile(profile_id="profile", name="Saved profile")},
        {
            "cli": PostprocessorPreset(
                preset_id="cli",
                display_name="Saved CLI",
                command="old-cli",
            )
        },
        revision=2,
    )
    current_snapshot = SettingsSnapshot(
        current_cfg,
        initial_snapshot.profiles,
        initial_snapshot.postprocessors,
        revision=3,
    )
    window = _stateful_settings_window(
        initial_snapshot,
        snapshot_provider=lambda: current_snapshot,
    )
    _make_all_settings_dirty(window)
    monkeypatch.setattr(
        "src.settings.messagebox.askyesno",
        lambda *args, **kwargs: True,
    )

    window._discard_config_changes()

    assert window._vars["output.paste_delay_ms"].get() == "175"
    assert window._config_changed_keys() == ()
    assert window._vars["profile.name"].get() == "Unsaved profile"
    assert window._profile_context.value == "unsaved context"
    assert window._command_text.value == "edited-cli"
    assert window._profile_editor_changed() is True
    assert window._postprocessor_editor_changed() is True


def test_close_discard_confirmation_and_withdrawn_reopen_reload(monkeypatch):
    window = _stateful_settings_window(SettingsSnapshot(AppConfig(), {}, {}))
    _make_all_settings_dirty(window)
    root = _Root()
    window._root = root
    prompts: list[str] = []
    answers = iter((False, True))

    def askyesno(title: str, message: str, **kwargs: object) -> bool:
        prompts.append(message)
        return next(answers)

    monkeypatch.setattr("src.settings.messagebox.askyesno", askyesno)

    window._hide_window()
    assert root.calls == []
    assert root.state() == "normal"

    window._hide_window()
    assert root.calls == ["withdraw"]
    assert root.state() == "withdrawn"
    assert "config.toml の設定" in prompts[-1]
    assert "プロフィール定義" in prompts[-1]
    assert "CLIプリセット定義" in prompts[-1]

    root.calls.clear()
    window._refresh_snapshot = lambda: root.calls.append("refresh")
    window._show_window()

    assert root.calls == ["refresh", "deiconify", "lift", "focus"]
    assert root.state() == "normal"


def test_definition_switches_refuse_to_discard_unsaved_edits(monkeypatch):
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )

    class Variable:
        def __init__(self, value: object = "") -> None:
            self.value = value

        def get(self) -> object:
            return self.value

        def set(self, value: object) -> None:
            self.value = value

    class Text:
        def __init__(self, value: str = "") -> None:
            self.value = value

        def get(self, start: str, end: str) -> str:
            return self.value

    window._vars = {
        "profile.editor": Variable("old-profile"),
        "profile.id": Variable("old-profile"),
        "profile.name": Variable("Old"),
        "postprocessor.editor": Variable("old-cli"),
        "postprocessor.id": Variable("old-cli"),
        "postprocessor.name": Variable("Old CLI"),
        "postprocessor.destination": Variable("unknown"),
        "postprocessor.input_mode": Variable("stdin"),
        "postprocessor.timeout": Variable("30"),
        "postprocessor.preflight": Variable(""),
        "postprocessor.preflight_message": Variable(""),
    }
    window._profile_context = Text("context")
    window._command_text = Text("old-cli")
    window._environment_text = Text("")
    window._prompt_text = Text("{{transcript}}")
    window._profile_editor_baseline = window._capture_profile_editor_state()
    window._postprocessor_editor_baseline = (
        window._capture_postprocessor_editor_state()
    )
    window._loaded_profile_id = "old-profile"
    window._loaded_postprocessor_id = "old-cli"

    window._vars["profile.name"].set("Edited")
    window._vars["profile.editor"].set("new-profile")
    window._command_text.value = "edited-cli"
    window._vars["postprocessor.editor"].set("new-cli")
    monkeypatch.setattr(
        "src.settings.messagebox.askyesno",
        lambda *args, **kwargs: False,
    )
    loaded_profiles: list[str] = []
    loaded_postprocessors: list[str] = []
    window._load_profile = loaded_profiles.append
    window._load_postprocessor = loaded_postprocessors.append

    window._load_selected_profile()
    window._load_selected_postprocessor()

    assert loaded_profiles == []
    assert loaded_postprocessors == []
    assert window._vars["profile.editor"].get() == "old-profile"
    assert window._vars["postprocessor.editor"].get() == "old-cli"


def test_unavailable_recognition_values_are_not_selectable(monkeypatch):
    import src.settings as settings_module

    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )

    class Variable:
        def __init__(self, value: str = "") -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    class Combo:
        def __init__(self) -> None:
            self.values: list[str] = []

        def configure(self, **kwargs) -> None:
            self.values = list(kwargs["values"])

    monkeypatch.setattr(settings_module.ttk, "Combobox", Combo)
    monkeypatch.setattr(
        settings_module,
        "available_recognition_engines",
        lambda: ("whisper",),
    )
    monkeypatch.setattr(
        settings_module,
        "available_recognition_devices",
        lambda engine: ("cpu",),
    )
    engine_combo = Combo()
    device_combo = Combo()
    window._vars = {
        "recognition.engine": Variable("whisper"),
        "recognition.language": Variable("ja"),
        "recognition.device": Variable("cuda"),
    }
    window._widgets = {
        "recognition.engine": engine_combo,
        "recognition.device": device_combo,
    }
    summary = Variable()
    window._recognition_summary_var = summary

    window._configure_recognition_choices(preserve_current=True)
    window._update_recognition_summary()

    assert engine_combo.values == ["whisper"]
    assert device_combo.values == ["cpu"]
    assert window._vars["recognition.device"].get() == "cuda"
    assert "現在値の実行先は未導入" in summary.get()


def test_unavailable_recognition_engine_is_not_selectable(monkeypatch):
    import src.settings as settings_module

    class Combo:
        def __init__(self) -> None:
            self.values: list[str] = []

        def configure(self, **kwargs: object) -> None:
            self.values = list(kwargs["values"])

    monkeypatch.setattr(settings_module.ttk, "Combobox", Combo)
    monkeypatch.setattr(
        settings_module,
        "available_recognition_engines",
        lambda: ("whisper",),
    )
    monkeypatch.setattr(
        settings_module,
        "available_recognition_devices",
        lambda engine: ("cpu",),
    )
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    engine_combo = Combo()
    device_combo = Combo()
    window._vars = {
        "recognition.engine": _Variable("qwen3-asr"),
        "recognition.language": _Variable("ja"),
        "recognition.device": _Variable("cuda"),
    }
    window._widgets = {
        "recognition.engine": engine_combo,
        "recognition.device": device_combo,
    }
    summary = _Variable()
    window._recognition_summary_var = summary

    window._configure_recognition_choices(preserve_current=True)
    window._update_recognition_summary()

    assert engine_combo.values == ["whisper"]
    assert device_combo.values == []
    assert window._vars["recognition.engine"].get() == "qwen3-asr"
    assert "現在値のエンジンは未導入" in summary.get()


def test_recognition_engine_change_selects_first_available_device(monkeypatch):
    import src.settings as settings_module

    class Combo:
        def __init__(self) -> None:
            self.values: list[str] = []

        def configure(self, **kwargs: object) -> None:
            self.values = list(kwargs["values"])

    monkeypatch.setattr(settings_module.ttk, "Combobox", Combo)
    monkeypatch.setattr(
        settings_module,
        "available_recognition_engines",
        lambda: ("whisper", "reazon-k2"),
    )
    monkeypatch.setattr(
        settings_module,
        "available_recognition_devices",
        lambda engine: ("cpu",) if engine == "reazon-k2" else ("cuda", "cpu"),
    )
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    engine_combo = Combo()
    device_combo = Combo()
    window._vars = {
        "recognition.engine": _Variable("reazon-k2"),
        "recognition.language": _Variable("ja"),
        "recognition.device": _Variable("cuda"),
    }
    window._widgets = {
        "recognition.engine": engine_combo,
        "recognition.device": device_combo,
    }

    window._on_recognition_engine_changed()

    assert engine_combo.values == ["whisper", "reazon-k2"]
    assert device_combo.values == ["cpu"]
    assert window._vars["recognition.device"].get() == "cpu"


def test_mousewheel_over_child_control_scrolls_containing_tab():
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )

    class Canvas:
        master = None

        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        def yview_scroll(self, amount: int, units: str) -> None:
            self.calls.append((amount, units))

    class Widget:
        def __init__(self, master: object) -> None:
            self.master = master

    class Event:
        delta = -120

        def __init__(self, widget: object) -> None:
            self.widget = widget

    canvas = Canvas()
    content = Widget(canvas)
    entry = Widget(content)
    window._scroll_canvases = {"長いタブ": canvas}

    result = window._on_scrollable_mousewheel(Event(entry))

    assert result == "break"
    assert canvas.calls == [(1, "units")]


def test_scrollable_tab_reaches_content_below_small_viewport():
    from src.settings import SettingsWindow

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display is unavailable")
    root.geometry("860x640")
    window = SettingsWindow(
        snapshot_provider=lambda: SettingsSnapshot(AppConfig(), {}, {}),
        on_save_config=lambda cfg, revision, fingerprint: (True, ""),
        on_save_profile=lambda profile, fingerprint: (True, ""),
        on_save_postprocessor=lambda preset, fingerprint: (True, ""),
    )
    try:
        window._root = root
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        content = window._add_scrollable_tab(notebook, "長いタブ")
        for index in range(80):
            ttk.Label(content, text=f"row {index}").pack()
        root.update_idletasks()

        canvas = window._scroll_canvases["長いタブ"]
        before = canvas.yview()
        canvas.yview_moveto(1.0)
        root.update_idletasks()
        after = canvas.yview()

        assert before[0] == 0.0
        assert after[0] > 0.0
        assert after[1] == 1.0
    finally:
        root.destroy()

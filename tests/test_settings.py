"""Structured settings helpers that do not require a visible Tk window."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest

from src.config import AppConfig
from src.postprocessing import PostprocessorPreset
from src.profiles import Profile
from src.settings import (
    SettingsSnapshot,
    SettingsWindow,
    environment_to_text,
    hotkey_value_to_text,
    lines_to_tuple,
    local_transport_changed,
    parse_environment,
    parse_hotkey_value,
    reclassify_changed_local_preset,
    refresh_snapshot_resources,
)
from src.toml_storage import FileFingerprint


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

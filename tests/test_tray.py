"""Tray UI behavior tests."""

from __future__ import annotations

import pytest


pytest.importorskip("pystray")
pytest.importorskip("PIL")


def test_toggle_startup_notifies_failure_when_state_unchanged(monkeypatch):
    import src.tray as tray_module

    app = object.__new__(tray_module.TrayApp)
    notices = []
    monkeypatch.setattr(tray_module, "is_registered", lambda: False)
    monkeypatch.setattr(tray_module, "toggle_startup", lambda: False)
    monkeypatch.setattr(app, "notify", lambda message: notices.append(message))

    app._toggle_startup(None, None)

    assert notices == ["スタートアップ: 変更失敗"]


def test_postprocessor_label_keeps_destination_visible():
    import src.tray as tray_module
    from src.postprocessing import PostprocessorPreset

    app = object.__new__(tray_module.TrayApp)
    app._postprocessor = "codex"
    app._postprocessors = {
        "codex": PostprocessorPreset(
            preset_id="codex",
            display_name="Codex 校正",
            command="codex",
            data_destination="remote",
        )
    }

    assert app._postprocessor_label() == "（外部送信）Codex 校正"


def test_long_postprocessor_name_keeps_destination_in_tooltip():
    import src.tray as tray_module
    from src.postprocessing import PostprocessorPreset

    app = object.__new__(tray_module.TrayApp)
    app._postprocessor = "remote"
    app._microphone = "OS既定"
    app._postprocessors = {
        "remote": PostprocessorPreset(
            preset_id="remote",
            display_name="長" * 200,
            command="remote",
            data_destination="remote",
        )
    }

    title = app._title_text()

    assert len(title) == 127
    assert "外部送信" in title


def test_enhancement_submenus_build_from_data_definitions():
    import src.tray as tray_module
    from src.postprocessing import PostprocessorPreset
    from src.profiles import Profile

    app = object.__new__(tray_module.TrayApp)
    app._profile = ""
    app._profiles = {"coding": Profile(profile_id="coding", name="Coding")}
    app._postprocessor = "off"
    app._postprocessors = {
        "custom": PostprocessorPreset(
            preset_id="custom",
            display_name="Custom",
            command="custom",
        )
    }
    app._on_set_profile = None
    app._on_set_postprocessor = None

    assert len(app._build_profile_menu().items) == 3
    assert len(app._build_postprocessor_menu().items) == 4

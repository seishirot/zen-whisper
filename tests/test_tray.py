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


def test_settings_menu_dispatches_callback():
    import src.tray as tray_module

    calls = []
    app = object.__new__(tray_module.TrayApp)
    app._on_open_settings = lambda: calls.append("open")

    app._open_settings(None, None)

    assert calls == ["open"]


def test_rejected_engine_change_keeps_tray_selection(monkeypatch):
    import src.tray as tray_module

    app = object.__new__(tray_module.TrayApp)
    app._engine = "whisper"
    app._device = "cuda"
    app._qwen3_model = "large"
    app._on_set_engine = lambda engine, model, device: False
    refreshed = []
    monkeypatch.setattr(app, "refresh_menu", lambda: refreshed.append(True))
    monkeypatch.setattr(app, "_update_title", lambda: None)

    handler = app._set_engine("reazon-k2", device="cpu")
    handler(None, None)

    assert app._engine == "whisper"
    assert app._device == "cuda"
    assert refreshed == [True]


@pytest.mark.parametrize(
    (
        "state_attribute",
        "callback_attribute",
        "handler_factory",
        "old_value",
        "new_value",
    ),
    [
        ("_language", "_on_set_language", "_set_lang", "ja", "en"),
        (
            "_microphone",
            "_on_set_microphone",
            "_set_microphone",
            "Old Mic",
            "New Mic",
        ),
        ("_profile", "_on_set_profile", "_set_profile", "old", "new"),
        (
            "_postprocessor",
            "_on_set_postprocessor",
            "_set_postprocessor",
            "off",
            "remote",
        ),
    ],
)
def test_rejected_tray_change_keeps_selection(
    monkeypatch,
    state_attribute,
    callback_attribute,
    handler_factory,
    old_value,
    new_value,
):
    import src.tray as tray_module

    app = object.__new__(tray_module.TrayApp)
    setattr(app, state_attribute, old_value)
    setattr(app, callback_attribute, lambda value: False)
    refreshed = []
    monkeypatch.setattr(app, "refresh_menu", lambda: refreshed.append(True))
    monkeypatch.setattr(app, "_update_title", lambda: None)

    handler = getattr(app, handler_factory)(new_value)
    handler(None, None)

    assert getattr(app, state_attribute) == old_value
    assert refreshed == [True]


def test_sound_toggle_uses_app_callback_and_keeps_state_on_failure(
    monkeypatch,
):
    import src.tray as tray_module
    from src.config import FeedbackConfig

    app = object.__new__(tray_module.TrayApp)
    app._feedback_config = FeedbackConfig(sound_enabled=True)
    app._on_save_config = None
    requested = []
    app._on_set_sound_enabled = lambda enabled: requested.append(enabled) or False
    notices = []
    monkeypatch.setattr(app, "notify", notices.append)

    app._toggle_sound(None, None)

    assert requested == [False]
    assert app._feedback_config.sound_enabled is True
    assert notices == ["サウンド設定の保存に失敗しました"]


def test_apply_settings_refreshes_tray_snapshot(monkeypatch):
    import src.tray as tray_module
    from src.config import AppConfig
    from src.postprocessing import PostprocessorPreset
    from src.profiles import Profile

    cfg = AppConfig()
    cfg.recognition.language = "en"
    cfg.recognition.engine = "reazon-k2"
    cfg.recognition.device = "cpu"
    cfg.recording.microphone = "USB Mic"
    cfg.enhancement.profile = "coding"
    cfg.enhancement.postprocessor = "local"
    profiles = {"coding": Profile(profile_id="coding", name="Coding")}
    postprocessors = {
        "local": PostprocessorPreset(
            preset_id="local",
            display_name="Local",
            command="ollama run local",
            data_destination="local",
        )
    }
    app = object.__new__(tray_module.TrayApp)
    app._icon = None
    refreshed = []
    monkeypatch.setattr(
        app,
        "refresh_menu",
        lambda: refreshed.append("menu"),
    )
    monkeypatch.setattr(
        app,
        "_update_title",
        lambda: refreshed.append("title"),
    )

    app.apply_settings(cfg, profiles, postprocessors)

    assert app._language == "en"
    assert app._engine == "reazon-k2"
    assert app._device == "cpu"
    assert app._microphone == "USB Mic"
    assert app._profiles == profiles
    assert app._postprocessors == postprocessors
    assert app._profile == "coding"
    assert app._postprocessor == "local"
    assert refreshed == ["menu", "title"]

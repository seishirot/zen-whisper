"""src.config のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.config import (
    ENGINE_QWEN3_ASR,
    ENGINE_REAZON_K2,
    ENGINE_WHISPER,
    AppConfig,
    HotkeyConfig,
    RecognitionConfig,
    load_config,
    save_config,
)
from src.toml_storage import file_fingerprint


def _expected_default_device() -> str:
    return "mlx" if sys.platform == "darwin" else "cuda"


class TestRecognitionConfig:
    """RecognitionConfig のテスト。"""

    def test_device_default_is_platform_explicit(self):
        cfg = RecognitionConfig()
        assert cfg.device == _expected_default_device()

    def test_engine_default_is_whisper(self):
        cfg = RecognitionConfig()
        assert cfg.engine == ENGINE_WHISPER

    def test_engine_accepts_values(self):
        for engine in (ENGINE_WHISPER, ENGINE_REAZON_K2, ENGINE_QWEN3_ASR):
            cfg = RecognitionConfig(engine=engine)
            assert cfg.engine == engine

    def test_device_accepts_values(self):
        for device in ("cuda", "cpu", "mlx"):
            cfg = RecognitionConfig(device=device)
            assert cfg.device == device

    def test_reazon_defaults(self):
        cfg = RecognitionConfig()
        assert cfg.reazon_language == "ja"
        assert cfg.reazon_precision == "fp32"
        assert cfg.reazon_chunk_sec == 25.0
        assert cfg.reazon_trailing_silence_sec == 0.5
        assert cfg.cpu_threads == 4

    def test_default_model_size(self):
        cfg = RecognitionConfig()
        assert cfg.model_size == "large-v3-turbo"

    def test_default_language(self):
        cfg = RecognitionConfig()
        assert cfg.language == "ja"


class TestHotkeyConfig:
    """HotkeyConfig のテスト。"""

    def test_submit_toggle_default_is_disabled(self):
        cfg = HotkeyConfig()
        assert cfg.submit_toggle == ""


class TestAppConfig:
    """AppConfig のテスト。"""

    def test_default_construction(self):
        cfg = AppConfig()
        assert cfg.recognition.device == _expected_default_device()
        assert cfg.recognition.engine == ENGINE_WHISPER
        assert cfg.hotkey.toggle == "shift+space"
        assert cfg.hotkey.submit_toggle == ""
        assert cfg.output.restore_clipboard is True
        assert cfg.enhancement.profile == ""
        assert cfg.enhancement.postprocessor == "off"

    def test_validate_default_has_no_warnings(self):
        cfg = AppConfig()
        warnings = cfg.validate()
        assert warnings == []

    def test_validate_reports_malformed_types_without_raising(self):
        cfg = AppConfig()
        cfg.recognition.model_size = 123
        cfg.recognition.no_speech_threshold = "invalid"
        cfg.recording.max_recording_sec = "invalid"
        cfg.feedback.volume = "invalid"
        cfg.logging.level = 42

        warnings = cfg.validate()

        assert any("model_size" in warning for warning in warnings)
        assert any("no_speech_threshold" in warning for warning in warnings)
        assert any("max_recording_sec" in warning for warning in warnings)
        assert any("volume" in warning for warning in warnings)
        assert any("logging.level" in warning for warning in warnings)

    @pytest.mark.parametrize(
        "value",
        [float("nan"), float("inf"), -float("inf")],
    )
    def test_validate_rejects_non_finite_numbers(self, value):
        cfg = AppConfig()
        cfg.recording.max_recording_sec = value
        cfg.recognition.reazon_chunk_sec = value

        warnings = cfg.validate()

        assert any("max_recording_sec" in warning for warning in warnings)
        assert any("reazon_chunk_sec" in warning for warning in warnings)


class TestLoadConfig:
    """load_config のテスト。"""

    def test_load_nonexistent_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "does_not_exist.toml")
        assert cfg.recognition.device == _expected_default_device()
        assert cfg.recognition.engine == ENGINE_WHISPER
        assert cfg.recognition.model_size == "large-v3-turbo"

    def test_load_with_device_field(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[recognition]\ndevice = "mlx"\n')
        cfg = load_config(toml_path)
        assert cfg.recognition.device == "mlx"

    def test_load_submit_toggle_string(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[hotkey]\nsubmit_toggle = "ctrl+shift+space"\n')
        cfg = load_config(toml_path)
        assert cfg.hotkey.submit_toggle == "ctrl+shift+space"

    def test_load_submit_toggle_list(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            '[hotkey]\nsubmit_toggle = ["ctrl+shift+space", "win+enter"]\n'
        )
        cfg = load_config(toml_path)
        assert cfg.hotkey.submit_toggle == ["ctrl+shift+space", "win+enter"]

    def test_load_enhancement_selection(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            '[enhancement]\nprofile = "coding"\npostprocessor = "ollama"\n'
        )
        cfg = load_config(toml_path)
        assert cfg.enhancement.profile == "coding"
        assert cfg.enhancement.postprocessor == "ollama"

    def test_save_and_reload_enhancement_selection(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        cfg = AppConfig()
        cfg.enhancement.profile = "coding"
        cfg.enhancement.postprocessor = "codex"

        assert save_config(cfg, toml_path) is True
        loaded = load_config(toml_path)

        assert loaded.enhancement.profile == "coding"
        assert loaded.enhancement.postprocessor == "codex"

    def test_optional_hallucination_threshold_round_trips_as_off(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        cfg = AppConfig()
        cfg.recognition.hallucination_silence_threshold = None

        assert save_config(cfg, toml_path) is True
        assert 'hallucination_silence_threshold = "off"' in (
            toml_path.read_text(encoding="utf-8")
        )
        assert (
            load_config(toml_path).recognition.hallucination_silence_threshold
            is None
        )

    def test_load_explicit_off_hallucination_threshold(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            '[recognition]\nhallucination_silence_threshold = "off"\n',
            encoding="utf-8",
        )

        cfg = load_config(toml_path)

        assert cfg.recognition.hallucination_silence_threshold is None

    def test_invalid_enhancement_types_normalize_to_off(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            "[enhancement]\nprofile = []\npostprocessor = []\n",
            encoding="utf-8",
        )

        cfg = load_config(toml_path)

        assert cfg.enhancement.profile == ""
        assert cfg.enhancement.postprocessor == "off"

    def test_non_table_enhancement_section_is_ignored(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text("enhancement = []\n", encoding="utf-8")

        cfg = load_config(toml_path)

        assert cfg.enhancement.profile == ""
        assert cfg.enhancement.postprocessor == "off"

    def test_load_legacy_auto_normalizes_to_explicit_defaults(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[recognition]\nengine = "auto"\ndevice = "auto"\n')
        cfg = load_config(toml_path)
        assert cfg.recognition.engine == ENGINE_WHISPER
        assert cfg.recognition.device == _expected_default_device()

    def test_load_normalizes_recording_sample_rate_to_asr_rate(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text("[recording]\nsample_rate = 48000\n")
        cfg = load_config(toml_path)
        assert cfg.recording.sample_rate == 16000

    def test_save_config_returns_false_on_write_failure(self, tmp_path):
        file_instead_of_directory = tmp_path / "file"
        file_instead_of_directory.write_text("not a directory", encoding="utf-8")
        missing_dir_path = file_instead_of_directory / "config.toml"

        assert save_config(AppConfig(), missing_dir_path) is False

    def test_save_config_leaves_no_temporary_file(self, tmp_path):
        toml_path = tmp_path / "config.toml"

        assert save_config(AppConfig(), toml_path) is True

        assert list(Path(tmp_path).glob(".config.toml.*.tmp")) == []

    def test_save_config_preserves_malformed_existing_file(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        malformed = b"[recognition\nengine = 'whisper'\n"
        toml_path.write_bytes(malformed)

        assert save_config(AppConfig(), toml_path) is False

        assert toml_path.read_bytes() == malformed
        assert list(Path(tmp_path).glob(".config.toml.*.tmp")) == []

    def test_save_config_preserves_unknown_future_fields(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            (
                'future_top_level = "keep"\n'
                "[recognition]\n"
                'engine = "whisper"\n'
                'future_decoder = "keep"\n'
            ),
            encoding="utf-8",
        )
        cfg = AppConfig()
        cfg.recognition.engine = ENGINE_REAZON_K2

        assert save_config(cfg, toml_path) is True
        loaded_text = toml_path.read_text(encoding="utf-8")

        assert 'future_top_level = "keep"' in loaded_text
        assert 'future_decoder = "keep"' in loaded_text
        assert 'engine = "reazon-k2"' in loaded_text

    def test_save_config_rejects_changed_file_fingerprint(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            '[recognition]\nlanguage = "ja"\n',
            encoding="utf-8",
        )
        expected = file_fingerprint(toml_path)
        external = '[recognition]\nlanguage = "en"\n'
        toml_path.write_text(external, encoding="utf-8")

        assert (
            save_config(
                AppConfig(),
                toml_path,
                expected_fingerprint=expected,
            )
            is False
        )
        assert toml_path.read_text(encoding="utf-8") == external

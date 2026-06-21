"""src.config のテスト。"""

from __future__ import annotations

import sys

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

    def test_validate_default_has_no_warnings(self):
        cfg = AppConfig()
        warnings = cfg.validate()
        assert warnings == []


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
        missing_dir_path = tmp_path / "missing" / "config.toml"

        assert save_config(AppConfig(), missing_dir_path) is False

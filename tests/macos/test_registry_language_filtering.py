from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_swift_registry_filters_language_by_engine() -> None:
    registry = (SWIFT_SRC / "ModelRegistry.swift").read_text(encoding="utf-8")
    settings = (SWIFT_SRC / "SettingsStore.swift").read_text(encoding="utf-8")

    assert "func validLanguage(_ value: String?, for engineID: String) -> String" in registry
    assert "func supportedLanguages(for engineID: String) -> [(id: String, label: String)]" in registry
    assert "language.engines[engineID] == nil ? nil" in registry
    assert "let language = registry.validLanguage(defaults.string(forKey: Key.language), for: engine)" in settings
    assert "defaults.set(registry.validLanguage(snapshot.language, for: engine), forKey: Key.language)" in settings
    assert 'registry.backendLanguage("ja", engineID: "mlx-qwen3-asr")' in (
        REPO_ROOT / "macos/ZenWhisper/ZenWhisperTests/CoreTests.swift"
    ).read_text(encoding="utf-8")

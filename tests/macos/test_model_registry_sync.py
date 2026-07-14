from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_swift_and_backend_model_registries_stay_in_sync() -> None:
    swift_registry = json.loads(
        (
            REPO_ROOT
            / "macos/ZenWhisper/ZenWhisper/Resources/model_registry.json"
        ).read_text(encoding="utf-8")
    )
    backend_registry = json.loads(
        (
            REPO_ROOT
            / "macos/backend/src/zen_whisper_mac_backend/resources/model_registry.json"
        ).read_text(encoding="utf-8")
    )

    assert swift_registry == backend_registry

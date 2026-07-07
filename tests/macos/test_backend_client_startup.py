from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_CLIENT = REPO_ROOT / "macos/ZenWhisper/ZenWhisper/BackendClient.swift"


def test_backend_client_does_not_hide_protocol_errors_until_health_timeout() -> None:
    text = BACKEND_CLIENT.read_text(encoding="utf-8")
    wait_for_health = text[
        text.index("func waitForHealth"):
        text.index("func preload")
    ]

    assert "catch let error as BackendProtocolError" in wait_for_health
    assert "throw error" in wait_for_health
    assert wait_for_health.index("catch let error as BackendProtocolError") < wait_for_health.index("lastError = error")

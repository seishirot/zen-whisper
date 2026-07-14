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

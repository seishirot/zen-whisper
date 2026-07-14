"""src.startup ファサードのテスト。"""

from __future__ import annotations

import sys

import pytest

from src.startup import is_registered, register, toggle, unregister


class TestStartupFacade:
    """startup モジュールのファサードが正しくインポートされるテスト。"""

    def test_is_registered_callable(self):
        assert callable(is_registered)

    def test_register_callable(self):
        assert callable(register)

    def test_unregister_callable(self):
        assert callable(unregister)

    def test_toggle_callable(self):
        assert callable(toggle)

    def test_is_registered_returns_bool(self):
        result = is_registered()
        assert isinstance(result, bool)

    def test_toggle_returns_unchanged_state_when_register_fails(self, monkeypatch):
        import src.startup as startup

        monkeypatch.setattr(startup, "is_registered", lambda: False)
        monkeypatch.setattr(startup, "register", lambda: False)

        assert startup.toggle() is False

    def test_toggle_returns_unchanged_state_when_unregister_fails(self, monkeypatch):
        import src.startup as startup

        monkeypatch.setattr(startup, "is_registered", lambda: True)
        monkeypatch.setattr(startup, "unregister", lambda: False)

        assert startup.toggle() is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
class TestWindowsStartup:
    """Windows スタートアップ登録のテスト。"""

    def test_platform_module_functions(self):
        from src.platform.windows import (
            is_startup_registered,
            register_startup,
            unregister_startup,
        )
        assert callable(is_startup_registered)
        assert callable(register_startup)
        assert callable(unregister_startup)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
class TestDarwinStartup:
    """macOS スタートアップ登録のテスト。"""

    def test_platform_module_functions(self):
        from src.platform.darwin import (
            is_startup_registered,
            register_startup,
            unregister_startup,
        )
        assert callable(is_startup_registered)
        assert callable(register_startup)
        assert callable(unregister_startup)

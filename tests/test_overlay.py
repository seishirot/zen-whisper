"""src.overlay のテスト。"""

from __future__ import annotations

import sys

import pytest

from src.overlay import _FONT_FAMILY, _get_active_monitor_rect


class TestFontFamily:
    """プラットフォーム別フォント設定のテスト。"""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_font(self):
        assert _FONT_FAMILY == "Segoe UI"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_mac_font(self):
        assert _FONT_FAMILY == "Helvetica Neue"


class TestGetActiveMonitorRect:
    """モニター情報取得のテスト。"""

    def test_returns_4_tuple(self):
        result = _get_active_monitor_rect()
        assert isinstance(result, tuple)
        assert len(result) == 4

    def test_all_elements_are_int(self):
        left, top, right, bottom = _get_active_monitor_rect()
        assert isinstance(left, int)
        assert isinstance(top, int)
        assert isinstance(right, int)
        assert isinstance(bottom, int)

    def test_right_greater_than_left(self):
        left, top, right, bottom = _get_active_monitor_rect()
        assert right > left

    def test_bottom_greater_than_top(self):
        left, top, right, bottom = _get_active_monitor_rect()
        assert bottom > top

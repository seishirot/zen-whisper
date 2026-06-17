"""src.overlay のテスト。"""

from __future__ import annotations

import sys

import pytest

import src.overlay as overlay_module
from src.overlay import (
    _FONT_FAMILY,
    _format_elapsed_seconds,
    _get_active_monitor_rect,
    _is_recording_state,
    OverlayIndicator,
)
from src.tray import TrayState


class _FakeCanvas:
    def __init__(self):
        self.next_id = 100
        self.itemconfigs = []

    def itemconfig(self, item_id, **kwargs):
        self.itemconfigs.append((item_id, kwargs))

    def delete(self, item_id):
        pass

    def create_oval(self, *args, **kwargs):
        return self._next_item_id()

    def create_arc(self, *args, **kwargs):
        return self._next_item_id()

    def create_line(self, *args, **kwargs):
        return self._next_item_id()

    def _next_item_id(self):
        item_id = self.next_id
        self.next_id += 1
        return item_id


class _FakeRoot:
    def __init__(self):
        self.after_calls = []
        self.cancelled = []
        self.deiconify_count = 0
        self.lift_count = 0

    def after(self, delay_ms, callback):
        if delay_ms == 0:
            callback()
            return "after#immediate"
        after_id = f"after#{len(self.after_calls) + 1}"
        self.after_calls.append((after_id, delay_ms, callback))
        return after_id

    def after_cancel(self, after_id):
        self.cancelled.append(after_id)

    def deiconify(self):
        self.deiconify_count += 1

    def lift(self):
        self.lift_count += 1


def _make_overlay_for_timer_tests():
    overlay = object.__new__(OverlayIndicator)
    overlay._cfg = type("Cfg", (), {"enabled": True})()
    overlay._on_click = None
    overlay._root = _FakeRoot()
    overlay._body_canvas = _FakeCanvas()
    overlay._mic_ids = []
    overlay._label_id = 1
    overlay._current_state = TrayState.IDLE
    overlay._positioned = True
    overlay._recording_started_at = None
    overlay._timer_after_id = None
    overlay._drag_x = 0
    overlay._drag_y = 0
    return overlay


class TestRecordingTimer:
    """録音経過タイマー表示のテスト。"""

    @pytest.mark.parametrize(
        ("elapsed_seconds", "expected"),
        [
            (0, "00:00"),
            (0.9, "00:00"),
            (5, "00:05"),
            (65, "01:05"),
            (600, "10:00"),
            (-1, "00:00"),
        ],
    )
    def test_format_elapsed_seconds(self, elapsed_seconds, expected):
        assert _format_elapsed_seconds(elapsed_seconds) == expected

    @pytest.mark.parametrize(
        "state",
        [
            TrayState.RECORDING,
            TrayState.SPEECH_DETECTED,
        ],
    )
    def test_recording_states_show_timer(self, state):
        assert _is_recording_state(state) is True

    @pytest.mark.parametrize(
        "state",
        [
            TrayState.IDLE,
            TrayState.LOADING,
            TrayState.TRANSCRIBING,
        ],
    )
    def test_non_recording_states_do_not_show_timer(self, state):
        assert _is_recording_state(state) is False

    def test_recording_timer_does_not_reset_during_speech_state_transition(self, monkeypatch):
        overlay = _make_overlay_for_timer_tests()
        times = iter([100.0, 100.0, 103.0])
        monkeypatch.setattr(overlay_module.time, "monotonic", lambda: next(times))

        overlay.set_state(TrayState.RECORDING)
        started_at = overlay._recording_started_at

        overlay.set_state(TrayState.SPEECH_DETECTED)

        assert overlay._recording_started_at == started_at
        assert overlay._timer_after_id == "after#1"
        assert len(overlay._root.after_calls) == 1
        assert overlay._body_canvas.itemconfigs[0] == (
            1,
            {"text": "00:00", "fill": "#FFFFFF"},
        )

    @pytest.mark.parametrize(
        ("next_state", "expected_label", "expected_fill"),
        [
            (TrayState.TRANSCRIBING, "処理中…", "#FFFFFF"),
            (TrayState.IDLE, "待機中", "#AAAAAA"),
        ],
    )
    def test_recording_timer_stops_when_leaving_recording_state(
        self,
        monkeypatch,
        next_state,
        expected_label,
        expected_fill,
    ):
        overlay = _make_overlay_for_timer_tests()
        monkeypatch.setattr(overlay_module.time, "monotonic", lambda: 100.0)

        overlay.set_state(TrayState.RECORDING)
        overlay.set_state(next_state)

        assert overlay._recording_started_at is None
        assert overlay._timer_after_id is None
        assert overlay._root.cancelled == ["after#1"]
        assert overlay._body_canvas.itemconfigs[-1] == (
            1,
            {"text": expected_label, "fill": expected_fill},
        )


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

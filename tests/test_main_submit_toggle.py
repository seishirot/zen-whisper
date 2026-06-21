"""src.main の submit_toggle 状態遷移テスト。"""

from __future__ import annotations

import threading

import src.main as main_module
from src.main import App


class _ReadyTranscriber:
    is_ready = True


class _Tray:
    def notify(self, message: str) -> None:
        pass


class _ThreadStub:
    started_targets: list[object] = []

    def __init__(self, target, daemon: bool) -> None:
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        self.started_targets.append(self.target)


def _make_recording_app() -> App:
    app = App.__new__(App)
    app._lock = threading.Lock()
    app._is_recording = True
    app._is_capturing = True
    app._submit_after_paste = False
    app._stop_event = threading.Event()
    app.transcriber = _ReadyTranscriber()
    app.tray = _Tray()
    return app


def _make_idle_app() -> App:
    app = App.__new__(App)
    app._lock = threading.Lock()
    app._is_recording = False
    app._is_capturing = False
    app._submit_after_paste = False
    app._stop_event = threading.Event()
    app._stop_event.set()
    app.transcriber = _ReadyTranscriber()
    app.tray = _Tray()
    return app


def test_submit_toggle_arms_enter_only_while_capturing() -> None:
    app = _make_recording_app()

    app._on_toggle(submit_after_paste=True)

    assert app._stop_event.is_set()
    assert app._submit_after_paste is True


def test_submit_toggle_after_capture_does_not_arm_enter() -> None:
    app = _make_recording_app()
    app._is_capturing = False

    app._on_toggle(submit_after_paste=True)

    assert app._stop_event.is_set()
    assert app._submit_after_paste is False


def test_submit_toggle_after_normal_stop_request_does_not_arm_enter() -> None:
    app = _make_recording_app()

    app._on_toggle()
    app._on_toggle(submit_after_paste=True)

    assert app._stop_event.is_set()
    assert app._submit_after_paste is False


def test_start_clears_stale_stop_event_before_worker_starts(monkeypatch) -> None:
    app = _make_idle_app()
    _ThreadStub.started_targets = []
    monkeypatch.setattr(main_module.threading, "Thread", _ThreadStub)

    app._on_toggle()

    assert app._is_recording is True
    assert app._is_capturing is True
    assert app._stop_event.is_set() is False
    assert len(_ThreadStub.started_targets) == 1
    assert _ThreadStub.started_targets[0].__self__ is app

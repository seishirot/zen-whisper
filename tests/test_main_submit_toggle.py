"""src.main の submit_toggle 状態遷移テスト。"""

from __future__ import annotations

import threading

import src.main as main_module
from src.config import AppConfig
from src.main import App
from src.postprocessing import PostprocessResult
from src.postprocessing import PostprocessorPreset


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


def _init_enhancement_state(app: App) -> None:
    app._enhancement_lock = threading.RLock()
    app._enhancement_generation = 0
    app._active_postprocessor_process = None


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


def test_postprocess_failure_suppresses_submit_after_paste(monkeypatch) -> None:
    notices = []
    app = App.__new__(App)
    app.postprocessors = {}
    app.tray = _Tray()
    app.tray.notify = notices.append
    monkeypatch.setattr(
        main_module,
        "process_transcript",
        lambda *args, **kwargs: PostprocessResult(
            text="dictionary fallback",
            succeeded=False,
            applied=True,
            error="command failed",
        ),
    )

    text, submit = app._apply_postprocessing(
        "raw",
        None,
        "missing",
        "ja",
        submit_after_paste=True,
    )

    assert text == "dictionary fallback"
    assert submit is False
    assert "Enter送信はキャンセル" in notices[0]


def test_remote_postprocessor_notice_lists_transmitted_data() -> None:
    app = App.__new__(App)
    app.postprocessors = {
        "remote": PostprocessorPreset(
            preset_id="remote",
            display_name="Remote",
            command="remote",
            data_destination="remote",
        )
    }

    notice = app._postprocessor_notice("remote")

    assert "外部送信" in notice
    assert "認識結果、選択プロフィールの文脈、辞書データ" in notice


def test_pipeline_rechecks_remote_selection_after_transcription(monkeypatch) -> None:
    pasted = []
    postprocess_calls = []
    app = App.__new__(App)
    app.cfg = AppConfig()
    app.cfg.enhancement.postprocessor = "remote"
    app.profiles = {}
    app.postprocessors = {
        "remote": PostprocessorPreset(
            preset_id="remote",
            display_name="Remote",
            command="remote",
            data_destination="remote",
        )
    }
    app._lock = threading.Lock()
    _init_enhancement_state(app)
    app._stop_event = threading.Event()
    app._is_recording = True
    app._is_capturing = True
    app._submit_after_paste = False
    app._language = "ja"
    app.tray = _Tray()
    app.sound = type(
        "_Sound",
        (),
        {"play_start": lambda self: None, "play_stop": lambda self: None},
    )()
    app._set_state = lambda state: None
    app._on_save_config = lambda: True

    class _SwitchingTranscriber:
        is_ready = True

        def transcribe(self, *args, **kwargs):
            app._on_set_postprocessor("off")
            return "raw transcript"

    app.transcriber = _SwitchingTranscriber()
    monkeypatch.setattr(main_module, "record", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        main_module,
        "process_transcript",
        lambda *args, **kwargs: postprocess_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        main_module,
        "paste",
        lambda text, output, **kwargs: pasted.append((text, kwargs)),
    )

    app._pipeline()

    assert postprocess_calls == []
    assert len(pasted) == 1
    assert pasted[0][0] == "raw transcript"
    assert pasted[0][1]["submit_after_paste"] is False


def test_stale_enhancement_generation_prevents_cli_start(monkeypatch) -> None:
    popen_calls = []
    notices = []
    app = App.__new__(App)
    app.cfg = AppConfig()
    app.cfg.enhancement.postprocessor = "remote"
    app.postprocessors = {
        "remote": PostprocessorPreset(
            preset_id="remote",
            display_name="Remote",
            command="remote",
            data_destination="remote",
        )
    }
    app.tray = _Tray()
    app.tray.notify = notices.append
    _init_enhancement_state(app)
    app._enhancement_generation = 1
    monkeypatch.setattr(
        main_module.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )

    text, submit = app._apply_postprocessing(
        "raw transcript",
        None,
        "remote",
        "ja",
        submit_after_paste=True,
        expected_generation=0,
    )

    assert popen_calls == []
    assert text == "raw transcript"
    assert submit is False
    assert "設定が変更されたため" in notices[0]


def test_switching_postprocessor_cancels_registered_process(monkeypatch) -> None:
    stopped = []
    process = object()
    app = App.__new__(App)
    app.cfg = AppConfig()
    app.cfg.enhancement.postprocessor = "remote"
    app.postprocessors = {}
    app.profiles = {}
    app.tray = _Tray()
    app._on_save_config = lambda: True
    _init_enhancement_state(app)
    app._active_postprocessor_process = process
    monkeypatch.setattr(
        app,
        "_terminate_postprocessor_process",
        stopped.append,
    )

    app._on_set_postprocessor("off")

    assert stopped == [process]
    assert app.cfg.enhancement.postprocessor == "off"
    assert app._enhancement_generation == 1
    assert app._active_postprocessor_process is None


def test_switch_discards_success_if_registered_process_cannot_be_stopped(
    monkeypatch,
) -> None:
    notices = []
    app = App.__new__(App)
    app.cfg = AppConfig()
    app.cfg.enhancement.postprocessor = "remote"
    app.postprocessors = {
        "remote": PostprocessorPreset(
            preset_id="remote",
            display_name="Remote",
            command="remote",
            data_destination="remote",
        )
    }
    app.profiles = {}
    app.tray = _Tray()
    app.tray.notify = notices.append
    app._on_save_config = lambda: True
    _init_enhancement_state(app)
    app._terminate_postprocessor_process = lambda process: None

    class FakeProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            app._on_set_postprocessor("off")
            return "stale remote result", ""

    monkeypatch.setattr(main_module.subprocess, "Popen", FakeProcess)

    text, submit = app._apply_postprocessing(
        "raw transcript",
        None,
        "remote",
        "ja",
        submit_after_paste=True,
        expected_generation=0,
    )

    assert text == "raw transcript"
    assert submit is False
    assert "結果を破棄" in notices[-1]

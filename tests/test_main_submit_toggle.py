"""src.main の submit_toggle 状態遷移テスト。"""

from __future__ import annotations

import copy
import threading
import time

import src.main as main_module
from src.config import AppConfig
from src.main import App
from src.postprocessing import PostprocessResult
from src.postprocessing import PostprocessorPreset
from src.toml_storage import (
    FileFingerprint,
    MISSING_FILE_FINGERPRINT,
)


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
    app._model_load_lock = threading.Lock()
    app._model_loading = False
    app._is_recording = True
    app._is_capturing = True
    app._submit_after_paste = False
    app._stop_event = threading.Event()
    app.transcriber = _ReadyTranscriber()
    app.tray = _Tray()
    return app


def _init_enhancement_state(app: App) -> None:
    app._config_lock = threading.RLock()
    app._settings_revision = 0
    app._config_fingerprint = None
    app._profile_fingerprints = {}
    app._postprocessors_fingerprint = MISSING_FILE_FINGERPRINT
    app._enhancement_lock = threading.RLock()
    app._enhancement_generation = 0
    app._active_postprocessor_process = None


def _make_idle_app() -> App:
    app = App.__new__(App)
    app._lock = threading.Lock()
    app._model_load_lock = threading.Lock()
    app._model_loading = False
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


def test_toggle_is_rejected_while_latest_model_is_loading() -> None:
    app = _make_idle_app()
    notices = []
    app._model_loading = True
    app.tray.notify = notices.append

    app._on_toggle()

    assert app._is_recording is False
    assert notices == ["モデルを準備中です。しばらくお待ちください。"]


def test_recording_can_still_be_stopped_while_model_load_flag_is_set() -> None:
    app = _make_recording_app()
    app._model_loading = True

    app._on_toggle()

    assert app._stop_event.is_set()


def test_new_recording_checks_model_loading_inside_recording_lock() -> None:
    app = _make_idle_app()
    app.transcriber = type("_NotReady", (), {"is_ready": False})()
    events = []

    class TraceLock:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            events.append(self.name)

        def __exit__(self, exc_type, exc, traceback):
            return False

    app._lock = TraceLock("recording")
    app._model_load_lock = TraceLock("model")

    app._on_toggle()

    assert events[:2] == ["recording", "model"]


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


class _SettingsTray:
    def __init__(self) -> None:
        self.applied = []
        self.notices = []

    def apply_settings(self, cfg, profiles, postprocessors) -> None:
        self.applied.append((cfg, profiles, postprocessors))

    def notify(self, message: str) -> None:
        self.notices.append(message)


class _SettingsOverlay:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _make_settings_app() -> App:
    app = App.__new__(App)
    app.cfg = AppConfig()
    app.profiles = {}
    app.postprocessors = {}
    app._lock = threading.Lock()
    app._is_recording = False
    app._is_capturing = False
    app._submit_after_paste = False
    app._shutdown = False
    app._stop_event = threading.Event()
    app._language = app.cfg.recognition.language
    app._config_lock = threading.RLock()
    app._settings_revision = 0
    app._config_fingerprint = None
    app._profile_fingerprints = {}
    app._postprocessors_fingerprint = MISSING_FILE_FINGERPRINT
    app._enhancement_lock = threading.RLock()
    app._enhancement_generation = 0
    app._active_postprocessor_process = None
    app._model_load_lock = threading.Lock()
    app._model_worker_lock = threading.Lock()
    app._model_load_generation = 0
    app._model_loading = False
    app.transcriber = _ReadyTranscriber()
    app.tray = _SettingsTray()
    app.overlay = _SettingsOverlay()
    app.sound = object()
    return app


def test_settings_save_is_rejected_while_pipeline_is_active(monkeypatch):
    app = _make_settings_app()
    app._is_recording = True
    saved = []
    monkeypatch.setattr(
        main_module,
        "save_config",
        lambda cfg: saved.append(cfg) or True,
    )

    succeeded, message = app._on_settings_save_config(AppConfig())

    assert succeeded is False
    assert "録音・文字起こし・校正" in message
    assert saved == []


def test_logging_destination_creates_parent_and_is_writable(tmp_path):
    log_path = tmp_path / "nested" / "zen-whisper.log"

    error = main_module._logging_destination_error(str(log_path))

    assert error == ""
    assert log_path.is_file()


def test_logging_destination_rejects_directory(tmp_path):
    error = main_module._logging_destination_error(str(tmp_path))

    assert "ディレクトリ" in error


def test_startup_warning_tolerates_unwritable_stderr(monkeypatch):
    class BrokenStream:
        def write(self, value):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    monkeypatch.setattr(main_module.sys, "stderr", BrokenStream())

    main_module._write_startup_warning("warning")


def test_engine_switch_is_rejected_while_pipeline_is_active():
    app = _make_settings_app()
    app._is_recording = True
    original_engine = app.cfg.recognition.engine

    accepted = app._on_set_engine(
        main_module.ENGINE_REAZON_K2,
        device="cpu",
    )

    assert accepted is False
    assert app.cfg.recognition.engine == original_engine
    assert "終わってから" in app.tray.notices[-1]


def test_sound_toggle_persists_under_config_revision_lock(monkeypatch):
    app = _make_settings_app()
    monkeypatch.setattr(
        main_module,
        "save_config",
        lambda cfg, **kwargs: True,
    )

    assert app._on_set_sound_enabled(False) is True

    assert app.cfg.feedback.sound_enabled is False
    assert app._settings_revision == 1


def test_settings_save_applies_live_and_marks_restart_fields(monkeypatch):
    app = _make_settings_app()
    new_cfg = copy.deepcopy(app.cfg)
    new_cfg.hotkey.toggle = "ctrl+space"
    new_cfg.recognition.model_size = "medium"
    load_messages = []
    monkeypatch.setattr(
        main_module,
        "save_config",
        lambda cfg, **kwargs: True,
    )
    monkeypatch.setattr(
        main_module,
        "SoundPlayer",
        lambda cfg: ("sound", cfg),
    )
    monkeypatch.setattr(
        app,
        "_load_model_async",
        lambda notify_message=None: load_messages.append(notify_message),
    )

    succeeded, message = app._on_settings_save_config(new_cfg)

    assert succeeded is True
    assert app.cfg is new_cfg
    assert app._language == new_cfg.recognition.language
    assert app.tray.applied == [(new_cfg, {}, {})]
    assert load_messages == ["設定変更によりモデルを再読み込みしています"]
    assert "ホットキー" in message
    assert "再起動後" in message


def test_qwen_max_tokens_change_requires_model_reload():
    before = AppConfig()
    before.recognition.engine = main_module.ENGINE_QWEN3_ASR
    after = copy.deepcopy(before)
    after.recognition.qwen3_max_new_tokens += 1

    assert App._recognition_reload_key(before) != (
        App._recognition_reload_key(after)
    )


def test_inactive_backend_settings_do_not_reload_current_model():
    whisper = AppConfig()
    changed_qwen = copy.deepcopy(whisper)
    changed_qwen.recognition.qwen3_max_new_tokens += 1
    changed_qwen.recognition.qwen3_attn_implementation = "eager"

    qwen = AppConfig()
    qwen.recognition.engine = main_module.ENGINE_QWEN3_ASR
    changed_whisper = copy.deepcopy(qwen)
    changed_whisper.recognition.model_size = "tiny"
    changed_whisper.recognition.compute_type = "int8"

    assert App._recognition_reload_key(whisper) == (
        App._recognition_reload_key(changed_qwen)
    )
    assert App._recognition_reload_key(qwen) == (
        App._recognition_reload_key(changed_whisper)
    )


def test_inactive_whisper_device_settings_do_not_reload_model():
    cuda = AppConfig()
    cuda.recognition.device = "cuda"
    changed_cuda = copy.deepcopy(cuda)
    changed_cuda.recognition.cpu_threads += 1

    cpu = AppConfig()
    cpu.recognition.device = "cpu"
    changed_cpu = copy.deepcopy(cpu)
    changed_cpu.recognition.compute_type = "float32"

    mlx = AppConfig()
    mlx.recognition.device = "mlx"
    changed_mlx = copy.deepcopy(mlx)
    changed_mlx.recognition.compute_type = "int8"
    changed_mlx.recognition.cpu_threads += 1

    assert App._recognition_reload_key(cuda) == (
        App._recognition_reload_key(changed_cuda)
    )
    assert App._recognition_reload_key(cpu) == (
        App._recognition_reload_key(changed_cpu)
    )
    assert App._recognition_reload_key(mlx) == (
        App._recognition_reload_key(changed_mlx)
    )


def test_stale_settings_revision_cannot_restore_disabled_remote_cli(
    monkeypatch,
):
    app = _make_settings_app()
    remote = PostprocessorPreset(
        preset_id="remote",
        display_name="Remote",
        command="remote-cli",
        data_destination="remote",
    )
    app.postprocessors = {"remote": remote}
    stale_cfg = copy.deepcopy(app.cfg)
    stale_cfg.enhancement.postprocessor = "remote"
    with app._config_lock:
        app.cfg.enhancement.postprocessor = "off"
        app._settings_revision = 1
    saved = []
    monkeypatch.setattr(
        main_module,
        "save_config",
        lambda cfg: saved.append(cfg) or True,
    )

    succeeded, message = app._on_settings_save_config(
        stale_cfg,
        expected_revision=0,
    )

    assert succeeded is False
    assert "再読込" in message
    assert app.cfg.enhancement.postprocessor == "off"
    assert saved == []


def test_config_save_rejects_external_file_change(monkeypatch):
    app = _make_settings_app()
    expected = FileFingerprint("file", digest="before")
    current = FileFingerprint("file", digest="after")
    saved = []
    monkeypatch.setattr(
        main_module,
        "config_file_fingerprint",
        lambda: current,
    )
    monkeypatch.setattr(
        main_module,
        "save_config",
        lambda cfg, **kwargs: saved.append(cfg) or True,
    )

    succeeded, message = app._on_settings_save_config(
        copy.deepcopy(app.cfg),
        expected_revision=0,
        expected_fingerprint=expected,
    )

    assert succeeded is False
    assert "設定画面の外" in message
    assert saved == []


def test_tray_save_rejects_external_file_change(monkeypatch):
    app = _make_settings_app()
    expected = FileFingerprint("file", digest="before")
    current = FileFingerprint("file", digest="after")
    app._config_fingerprint = expected
    saved = []
    monkeypatch.setattr(
        main_module,
        "config_file_fingerprint",
        lambda: current,
    )
    monkeypatch.setattr(
        main_module,
        "save_config",
        lambda cfg, **kwargs: saved.append(cfg) or True,
    )

    assert app._on_save_config() is False
    assert saved == []


def test_engine_switch_rolls_back_when_save_fails(monkeypatch):
    app = _make_settings_app()
    previous = copy.deepcopy(app.cfg.recognition)
    loads = []
    monkeypatch.setattr(app, "_on_save_config", lambda: False)
    monkeypatch.setattr(
        main_module,
        "recognition_configuration_error",
        lambda cfg: "",
    )
    monkeypatch.setattr(
        app,
        "_load_model_async",
        lambda notify_message=None: loads.append(notify_message),
    )

    accepted = app._on_set_engine(
        main_module.ENGINE_REAZON_K2,
        device="cpu",
    )

    assert accepted is False
    assert app.cfg.recognition == previous
    assert loads == []


def test_profile_save_rejects_external_target_change(monkeypatch):
    app = _make_settings_app()
    profile = main_module.Profile(profile_id="other", name="Other")
    expected = FileFingerprint("file", digest="before")
    current = FileFingerprint("file", digest="after")
    saved = []
    monkeypatch.setattr(main_module, "load_profiles", lambda: {})
    monkeypatch.setattr(
        main_module,
        "profile_file_fingerprint",
        lambda profile_id: current,
    )
    monkeypatch.setattr(
        main_module,
        "save_profile",
        lambda value, **kwargs: saved.append(value),
    )

    succeeded, message = app._on_settings_save_profile(
        profile,
        expected_fingerprint=expected,
    )

    assert succeeded is False
    assert "設定画面の外" in message
    assert saved == []


def test_postprocessor_save_rejects_external_file_change(monkeypatch):
    app = _make_settings_app()
    preset = PostprocessorPreset(
        preset_id="other",
        display_name="Other",
        command="local-cli",
    )
    expected = FileFingerprint("file", digest="before")
    current = FileFingerprint("file", digest="after")
    saved = []
    monkeypatch.setattr(main_module, "load_postprocessors", lambda: {})
    monkeypatch.setattr(
        main_module,
        "postprocessors_file_fingerprint",
        lambda: current,
    )
    monkeypatch.setattr(
        main_module,
        "save_postprocessor",
        lambda value, **kwargs: saved.append(value),
    )

    succeeded, message = app._on_settings_save_postprocessor(
        preset,
        expected_fingerprint=expected,
    )

    assert succeeded is False
    assert "設定画面の外" in message
    assert saved == []


def test_settings_save_holds_recording_lock_until_runtime_apply(
    monkeypatch,
):
    app = _make_settings_app()
    app.transcriber = type("_NotReady", (), {"is_ready": False})()
    cfg = copy.deepcopy(app.cfg)
    cfg.output.paste_delay_ms += 1
    save_started = threading.Event()
    allow_save = threading.Event()
    save_finished = threading.Event()
    toggle_finished = threading.Event()

    def blocking_save(value, **kwargs):
        save_started.set()
        assert allow_save.wait(timeout=5)
        return True

    monkeypatch.setattr(main_module, "save_config", blocking_save)
    monkeypatch.setattr(
        main_module,
        "SoundPlayer",
        lambda value: object(),
    )

    def save_settings():
        app._on_settings_save_config(cfg, expected_revision=0)
        save_finished.set()

    def toggle():
        app._on_toggle()
        toggle_finished.set()

    save_thread = threading.Thread(target=save_settings)
    toggle_thread = threading.Thread(target=toggle)
    save_thread.start()
    assert save_started.wait(timeout=5)
    toggle_thread.start()

    assert toggle_finished.wait(timeout=0.1) is False
    assert app._is_recording is False

    allow_save.set()
    assert save_finished.wait(timeout=5)
    assert toggle_finished.wait(timeout=5)
    save_thread.join(timeout=5)
    toggle_thread.join(timeout=5)


def test_settings_snapshot_isolated_from_runtime_state(monkeypatch):
    app = _make_settings_app()
    app.postprocessors = {
        "local": PostprocessorPreset(
            preset_id="local",
            display_name="Local",
            command="ollama run model",
            data_destination="local",
            environment={"MODEL": "one"},
        )
    }
    monkeypatch.setattr(
        main_module,
        "load_local_postprocessor_ids",
        lambda: frozenset({"local"}),
    )

    snapshot = app._settings_snapshot()
    snapshot.config.recognition.language = "en"
    snapshot.postprocessors["local"].environment["MODEL"] = "two"

    assert app.cfg.recognition.language == "ja"
    assert app.postprocessors["local"].environment["MODEL"] == "one"
    assert snapshot.local_postprocessor_ids == frozenset({"local"})
    assert snapshot.revision == 0


def test_saving_active_profile_cancels_stale_postprocessor(monkeypatch):
    from src.profiles import Profile

    app = _make_settings_app()
    app.cfg.enhancement.profile = "coding"
    process = object()
    app._active_postprocessor_process = process
    profile = Profile(profile_id="coding", name="Coding")
    app.profiles = {"coding": profile}
    stopped = []
    monkeypatch.setattr(main_module, "save_profile", lambda value: None)
    monkeypatch.setattr(
        main_module,
        "load_profiles",
        lambda: {"coding": profile},
    )
    monkeypatch.setattr(
        app,
        "_terminate_postprocessor_process",
        stopped.append,
    )

    succeeded, message = app._on_settings_save_profile(profile)

    assert succeeded is True
    assert "Coding" in message
    assert app.profiles == {"coding": profile}
    assert app._enhancement_generation == 1
    assert stopped == [process]


def test_saving_active_postprocessor_cancels_stale_process(monkeypatch):
    preset = PostprocessorPreset(
        preset_id="local",
        display_name="Local",
        command="ollama run model",
        data_destination="local",
    )
    app = _make_settings_app()
    app.cfg.enhancement.postprocessor = "local"
    app.postprocessors = {"local": preset}
    process = object()
    app._active_postprocessor_process = process
    stopped = []
    monkeypatch.setattr(
        main_module,
        "save_postprocessor",
        lambda value: None,
    )
    monkeypatch.setattr(
        main_module,
        "load_postprocessors",
        lambda: {"local": preset},
    )
    monkeypatch.setattr(
        app,
        "_terminate_postprocessor_process",
        stopped.append,
    )

    succeeded, message = app._on_settings_save_postprocessor(preset)

    assert succeeded is True
    assert "Local" in message
    assert app.postprocessors == {"local": preset}
    assert app._enhancement_generation == 1
    assert stopped == [process]


def test_profile_save_rejects_external_change_to_active_profile(
    monkeypatch,
):
    from src.profiles import Profile

    app = _make_settings_app()
    original = Profile(profile_id="active", name="Original")
    changed = Profile(profile_id="active", name="Changed externally")
    app.cfg.enhancement.profile = "active"
    app.profiles = {"active": original}
    saved = []
    monkeypatch.setattr(
        main_module,
        "load_profiles",
        lambda: {"active": changed},
    )
    monkeypatch.setattr(
        main_module,
        "save_profile",
        lambda profile: saved.append(profile),
    )

    succeeded, message = app._on_settings_save_profile(
        Profile(profile_id="other", name="Other")
    )

    assert succeeded is False
    assert "設定画面の外" in message
    assert saved == []
    assert app.profiles == {"active": original}


def test_postprocessor_save_rejects_external_destination_change(
    monkeypatch,
):
    original = PostprocessorPreset(
        preset_id="active",
        display_name="Active",
        command="local-cli",
        data_destination="local",
    )
    changed = PostprocessorPreset(
        preset_id="active",
        display_name="Active",
        command="remote-cli",
        data_destination="remote",
    )
    app = _make_settings_app()
    app.cfg.enhancement.postprocessor = "active"
    app.postprocessors = {"active": original}
    saved = []
    monkeypatch.setattr(
        main_module,
        "load_postprocessors",
        lambda: {"active": changed},
    )
    monkeypatch.setattr(
        main_module,
        "save_postprocessor",
        lambda preset: saved.append(preset),
    )

    succeeded, message = app._on_settings_save_postprocessor(
        PostprocessorPreset(
            preset_id="other",
            display_name="Other",
            command="other-cli",
        )
    )

    assert succeeded is False
    assert "送信先" in message
    assert saved == []
    assert app.postprocessors == {"active": original}


def test_latest_model_load_wins_when_older_load_finishes_first(
    monkeypatch,
):
    app = _make_settings_app()
    app._shutdown = False
    app.transcriber = "initial"
    states = []
    app._set_state = states.append
    targets = []
    candidates = []

    class CapturedThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            targets.append(self.target)

    class Candidate:
        is_ready = True

        def __init__(self):
            self.engine_label = f"candidate-{len(candidates)}"
            self.loaded_model = ""
            candidates.append(self)

        def load_model(self, cfg, on_timeout=None):
            self.loaded_model = cfg.model_size

    monkeypatch.setattr(main_module.threading, "Thread", CapturedThread)
    monkeypatch.setattr(main_module, "Transcriber", Candidate)
    monkeypatch.setattr(main_module, "preload_vad", lambda: None)

    app.cfg.recognition.model_size = "old"
    app._load_model_async()
    app.cfg.recognition.model_size = "new"
    app._load_model_async()

    targets[0]()
    assert app.transcriber == "initial"
    assert app._model_loading is True
    assert candidates == []

    targets[1]()
    assert app.transcriber is candidates[0]
    assert candidates[0].loaded_model == "new"
    assert app._model_loading is False
    assert states[-1] is main_module.TrayState.IDLE
    assert sum("モデルのロードが完了" in notice for notice in app.tray.notices) == 1


def test_model_reload_unloads_published_backend_before_candidate_load(
    monkeypatch,
):
    app = _make_settings_app()
    app._set_state = lambda state: None
    targets = []
    events = []

    class Existing:
        is_ready = True

        def unload(self):
            events.append("old-unload")

    class Candidate:
        is_ready = True
        engine_label = "candidate"

        def load_model(self, cfg, on_timeout=None):
            events.append("candidate-load")

        def unload(self):
            events.append("candidate-unload")

    class CapturedThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            targets.append(self.target)

    app.transcriber = Existing()
    monkeypatch.setattr(main_module.threading, "Thread", CapturedThread)
    monkeypatch.setattr(main_module, "Transcriber", Candidate)
    monkeypatch.setattr(main_module, "preload_vad", lambda: None)

    app._load_model_async()
    targets[0]()

    assert events[:2] == ["old-unload", "candidate-load"]
    assert app.transcriber.is_ready is True
    assert app._model_loading is False


def test_model_loading_recovers_when_worker_thread_cannot_start(
    monkeypatch,
):
    app = _make_settings_app()
    states = []
    unloaded = []
    app._set_state = states.append
    app.transcriber = type(
        "Existing",
        (),
        {
            "is_ready": True,
            "unload": lambda self: unloaded.append(True),
        },
    )()

    class FailingThread:
        def __init__(self, target, daemon):
            pass

        def start(self):
            raise RuntimeError("cannot start")

    monkeypatch.setattr(main_module.threading, "Thread", FailingThread)

    app._load_model_async()

    assert app._model_loading is False
    assert unloaded == [True]
    assert states[-1] is main_module.TrayState.IDLE
    assert "開始できませんでした" in app.tray.notices[-1]


def test_model_loading_recovers_when_transcriber_construction_fails(
    monkeypatch,
):
    app = _make_settings_app()
    states = []
    unloaded = []
    app._set_state = states.append
    app.transcriber = type(
        "Existing",
        (),
        {
            "is_ready": True,
            "unload": lambda self: unloaded.append(True),
        },
    )()

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class FailingTranscriber:
        def __init__(self):
            raise RuntimeError("cannot construct")

    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main_module, "Transcriber", FailingTranscriber)

    app._load_model_async()

    assert app._model_loading is False
    assert unloaded == [True]
    assert states[-1] is main_module.TrayState.IDLE
    assert "ロードに失敗" in app.tray.notices[-1]


def test_model_loads_are_serialized_and_queued_stale_load_is_skipped(
    monkeypatch,
):
    app = _make_settings_app()
    app._set_state = lambda state: None
    first_started = threading.Event()
    release_first = threading.Event()
    latest_completed = threading.Event()
    second_created = threading.Event()
    active_loads = 0
    max_active_loads = 0
    candidate_count = 0
    counter_lock = threading.Lock()

    class Candidate:
        is_ready = True

        def __init__(self):
            nonlocal candidate_count
            self.index = candidate_count
            candidate_count += 1
            self.engine_label = f"candidate-{self.index}"
            if self.index == 1:
                second_created.set()

        def load_model(self, cfg, on_timeout=None):
            nonlocal active_loads, max_active_loads
            with counter_lock:
                active_loads += 1
                max_active_loads = max(max_active_loads, active_loads)
            try:
                if self.index == 0:
                    first_started.set()
                    assert release_first.wait(timeout=5)
                else:
                    latest_completed.set()
            finally:
                with counter_lock:
                    active_loads -= 1

    monkeypatch.setattr(main_module, "Transcriber", Candidate)
    monkeypatch.setattr(main_module, "preload_vad", lambda: None)

    app.cfg.recognition.model_size = "first"
    app._load_model_async()
    assert first_started.wait(timeout=5)

    app.cfg.recognition.model_size = "stale-queued"
    app._load_model_async()
    app.cfg.recognition.model_size = "latest"
    app._load_model_async()

    assert second_created.wait(timeout=0.1) is False
    release_first.set()
    assert latest_completed.wait(timeout=5)
    deadline = time.monotonic() + 5
    while app._model_loading and time.monotonic() < deadline:
        time.sleep(0.01)

    assert app._model_loading is False
    assert candidate_count == 2
    assert max_active_loads == 1
    assert app.transcriber.engine_label == "candidate-1"

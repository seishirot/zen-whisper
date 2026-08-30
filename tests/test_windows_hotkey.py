"""Threaded RegisterHotKey controller tests using a fake Win32 adapter."""

from __future__ import annotations

import queue
import threading
import time

import pytest

from src.config import HotkeyConfig
from src.hotkey import _WindowsHotkeyManager
from src.windows_hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    WM_APP_HOTKEY_COMMAND,
    WM_HOTKEY,
    HotkeyCommandError,
    HotkeyRegistrationError,
    WindowsHotkeyController,
)


class FakeWindowsHotkeyApi:
    def __init__(self) -> None:
        self.messages: queue.Queue[tuple[int, int, int, int, int]] = (
            queue.Queue()
        )
        self.queue_ready = False
        self.owner_thread_id = 0
        self.registered: dict[int, tuple[int, int]] = {}
        self.register_calls: list[tuple[int, int, int, int]] = []
        self.unregister_calls: list[tuple[int, int]] = []
        self.post_calls: list[tuple[int, int, int, int]] = []
        self.fail_register: dict[tuple[int, int], int] = {}
        self.fail_unregister_once: dict[int, int] = {}
        self.drop_next_post = False
        self.fail_next_post = False
        self.delay_next_post = 0.0

    def ensure_message_queue(self) -> None:
        self.queue_ready = True

    def current_thread_id(self) -> int:
        self.owner_thread_id = threading.get_ident()
        return self.owner_thread_id

    def register_hot_key(
        self,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> tuple[bool, int]:
        self.register_calls.append(
            (threading.get_ident(), hotkey_id, modifiers, virtual_key)
        )
        winerror = self.fail_register.get((modifiers, virtual_key), 0)
        if winerror:
            return False, winerror
        self.registered[hotkey_id] = (modifiers, virtual_key)
        return True, 0

    def unregister_hot_key(self, hotkey_id: int) -> tuple[bool, int]:
        self.unregister_calls.append((threading.get_ident(), hotkey_id))
        winerror = self.fail_unregister_once.pop(hotkey_id, 0)
        if winerror:
            return False, winerror
        self.registered.pop(hotkey_id, None)
        return True, 0

    def post_thread_message(
        self,
        thread_id: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> tuple[bool, int]:
        self.post_calls.append((thread_id, message, wparam, lparam))
        if self.fail_next_post:
            self.fail_next_post = False
            return False, 1444
        if self.drop_next_post:
            self.drop_next_post = False
            return True, 0
        if self.delay_next_post:
            delay = self.delay_next_post
            self.delay_next_post = 0.0
            timer = threading.Timer(
                delay,
                lambda: self.messages.put(
                    (1, message, wparam, lparam, 0)
                ),
            )
            timer.daemon = True
            timer.start()
            return True, 0
        self.messages.put((1, message, wparam, lparam, 0))
        return True, 0

    def get_message(self) -> tuple[int, int, int, int, int]:
        return self.messages.get(timeout=3.0)

    def format_error(self, winerror: int) -> str:
        return f"fake error {winerror}"

    def emit_hotkey(self, hotkey_id: int) -> None:
        self.messages.put((1, WM_HOTKEY, hotkey_id, 0, 0))

    def hotkey_id(self, modifiers: int, virtual_key: int) -> int:
        expected = (modifiers | MOD_NOREPEAT, virtual_key)
        return next(
            hotkey_id
            for hotkey_id, registration in self.registered.items()
            if registration == expected
        )


def _config() -> HotkeyConfig:
    return HotkeyConfig(
        toggle="shift+space",
        submit_toggle="ctrl+shift+space",
        switch_lang="shift+alt+space",
    )


def _manager(
    api: FakeWindowsHotkeyApi,
    calls: list[str],
) -> _WindowsHotkeyManager:
    controller = WindowsHotkeyController(
        api,
        command_timeout=0.15,
        settle_timeout=0.02,
        join_timeout=0.3,
    )
    return _WindowsHotkeyManager(
        lambda: calls.append("toggle"),
        lambda: calls.append("switch_lang"),
        lambda: calls.append("submit_toggle"),
        controller=controller,
    )


def _wait_for_calls(calls: list[str], count: int) -> None:
    deadline = time.monotonic() + 1.0
    while len(calls) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(calls) == count


def test_registration_and_unregistration_stay_on_owner_thread() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)

    status = manager.start(_config())

    assert status.healthy is True
    assert api.queue_ready is True
    assert len(api.register_calls) == 3
    assert all(call[0] == api.owner_thread_id for call in api.register_calls)
    assert all(call[2] & MOD_NOREPEAT for call in api.register_calls)

    assert manager.stop() is True


def test_post_start_message_loop_failure_notifies_runtime_error() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    notified = threading.Event()
    statuses = []
    assert manager.start(_config()).healthy is True
    manager.set_runtime_error_callback(
        lambda status: (statuses.append(status), notified.set())
    )

    api.messages.put((-1, 0, 0, 0, 1234))

    assert notified.wait(timeout=1.0)
    assert statuses[-1].healthy is False
    assert statuses[-1].restart_required is True
    assert "1234" in statuses[-1].message
    assert manager.status() == statuses[-1]
    assert manager.stop() is True
    assert api.registered == {}
    assert all(call[0] == api.owner_thread_id for call in api.unregister_calls)
    assert manager.stop() is True


def test_one_message_dispatches_once_and_actions_remain_separate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    caplog.set_level("INFO", logger="src.windows_hotkey")
    manager.start(_config())
    try:
        toggle_id = api.hotkey_id(MOD_SHIFT, 0x20)
        submit_id = api.hotkey_id(MOD_CONTROL | MOD_SHIFT, 0x20)
        switch_id = api.hotkey_id(MOD_SHIFT | MOD_ALT, 0x20)

        api.emit_hotkey(toggle_id)
        api.emit_hotkey(submit_id)
        api.emit_hotkey(switch_id)
        _wait_for_calls(calls, 3)
        assert calls == ["toggle", "submit_toggle", "switch_lang"]
        assert "WM_HOTKEY受信: action=toggle, combo=shift+space" in caplog.text
        assert (
            "WM_HOTKEY受信: action=submit_toggle, combo=ctrl+shift+space"
            in caplog.text
        )

        api.emit_hotkey(toggle_id)
        api.emit_hotkey(toggle_id)
        _wait_for_calls(calls, 5)
        assert calls[-2:] == ["toggle", "toggle"]
    finally:
        manager.stop()


def test_startup_partial_failure_rolls_back_and_same_config_can_retry() -> None:
    api = FakeWindowsHotkeyApi()
    api.fail_register[(MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, 0x20)] = 1409
    calls: list[str] = []
    manager = _manager(api, calls)

    status = manager.start(_config())
    assert status.healthy is False
    assert "ctrl+shift+space" in status.message
    assert "1409" in status.message
    assert api.registered == {}

    del api.fail_register[(MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, 0x20)]
    transaction = manager.prepare_reconfigure(_config())
    transaction.commit()

    assert manager.status().healthy is True
    assert len(api.registered) == 3
    manager.stop()


def test_prepare_conflict_rolls_back_additions_and_keeps_old_mapping() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    manager.start(_config())
    old_ids = set(api.registered)
    changed = HotkeyConfig(
        toggle="ctrl+space",
        submit_toggle="win+a",
        switch_lang="shift+alt+space",
    )
    api.fail_register[(MOD_WIN | MOD_NOREPEAT, ord("A"))] = 1409

    with pytest.raises(HotkeyRegistrationError) as raised:
        manager.prepare_reconfigure(changed)

    assert raised.value.combo == "win+a"
    assert set(api.registered) == old_ids
    assert manager.status().healthy is True

    api.emit_hotkey(api.hotkey_id(MOD_SHIFT, 0x20))
    _wait_for_calls(calls, 1)
    assert calls == ["toggle"]
    manager.stop()


def test_prepared_ids_are_inactive_until_commit_and_abort_restores_old() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    manager.start(_config())
    changed = HotkeyConfig(
        toggle="ctrl+space",
        submit_toggle="",
        switch_lang="shift+alt+space",
    )

    transaction = manager.prepare_reconfigure(changed)
    old_id = api.hotkey_id(MOD_SHIFT, 0x20)
    staged_id = api.hotkey_id(MOD_CONTROL, 0x20)
    api.emit_hotkey(old_id)
    api.emit_hotkey(staged_id)
    time.sleep(0.03)
    assert calls == []

    transaction.abort()
    api.emit_hotkey(old_id)
    _wait_for_calls(calls, 1)
    assert calls == ["toggle"]
    assert staged_id not in api.registered
    manager.stop()


def test_commit_switches_mapping_then_removes_obsolete_ids() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    manager.start(_config())
    old_toggle_id = api.hotkey_id(MOD_SHIFT, 0x20)
    changed = HotkeyConfig(
        toggle="ctrl+space",
        submit_toggle="",
        switch_lang="shift+alt+space",
    )

    transaction = manager.prepare_reconfigure(changed)
    new_toggle_id = api.hotkey_id(MOD_CONTROL, 0x20)
    transaction.commit()

    assert old_toggle_id not in api.registered
    api.emit_hotkey(new_toggle_id)
    _wait_for_calls(calls, 1)
    assert calls == ["toggle"]
    manager.stop()


def test_action_swap_reuses_ids_without_double_registration() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    manager.start(_config())
    register_count = len(api.register_calls)
    shift_id = api.hotkey_id(MOD_SHIFT, 0x20)
    ctrl_shift_id = api.hotkey_id(MOD_CONTROL | MOD_SHIFT, 0x20)
    swapped = HotkeyConfig(
        toggle="ctrl+shift+space",
        submit_toggle="shift+space",
        switch_lang="shift+alt+space",
    )

    transaction = manager.prepare_reconfigure(swapped)
    transaction.commit()

    assert len(api.register_calls) == register_count
    api.emit_hotkey(shift_id)
    api.emit_hotkey(ctrl_shift_id)
    _wait_for_calls(calls, 2)
    assert calls == ["submit_toggle", "toggle"]
    manager.stop()


def test_timeout_is_not_resent_and_recovery_uses_authoritative_config() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    manager.start(_config())
    changed = HotkeyConfig(
        toggle="ctrl+space",
        submit_toggle="",
        switch_lang="shift+alt+space",
    )
    api.drop_next_post = True

    with pytest.raises(HotkeyCommandError):
        manager.prepare_reconfigure(changed)
    timed_out_command = api.post_calls[-1][2]
    assert manager.status().healthy is False

    status = manager.recover(_config())

    assert status.healthy is True
    assert sum(
        1
        for _thread_id, message, command_id, _lparam in api.post_calls
        if message == WM_APP_HOTKEY_COMMAND
        and command_id == timed_out_command
    ) == 1
    assert api.hotkey_id(MOD_SHIFT, 0x20)
    manager.stop()


def test_timeout_waits_once_for_late_completion_without_resending() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    controller = WindowsHotkeyController(
        api,
        command_timeout=0.03,
        settle_timeout=0.1,
        join_timeout=0.3,
    )
    manager = _WindowsHotkeyManager(
        lambda: calls.append("toggle"),
        lambda: calls.append("switch_lang"),
        lambda: calls.append("submit_toggle"),
        controller=controller,
    )
    manager.start(_config())
    changed = HotkeyConfig(
        toggle="ctrl+space",
        submit_toggle="",
        switch_lang="shift+alt+space",
    )
    api.delay_next_post = 0.05

    with pytest.raises(HotkeyCommandError):
        manager.prepare_reconfigure(changed)
    timed_out_command = api.post_calls[-1][2]

    status = manager.recover(_config())

    assert status.healthy is True
    assert sum(
        1
        for _thread_id, message, command_id, _lparam in api.post_calls
        if message == WM_APP_HOTKEY_COMMAND
        and command_id == timed_out_command
    ) == 1
    assert api.hotkey_id(MOD_SHIFT, 0x20)
    manager.stop()


def test_post_failure_marks_degraded_and_can_rebuild() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    manager.start(_config())
    api.fail_next_post = True

    with pytest.raises(HotkeyCommandError) as raised:
        manager.prepare_reconfigure(_config())

    assert "1444" in str(raised.value)
    assert manager.status().healthy is False
    assert manager.recover(_config()).healthy is True
    manager.stop()


def test_commit_unregister_failure_disables_dispatch_then_rebuilds() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    manager.start(_config())
    obsolete_id = api.hotkey_id(MOD_SHIFT, 0x20)
    changed = HotkeyConfig(
        toggle="ctrl+space",
        submit_toggle="",
        switch_lang="shift+alt+space",
    )
    transaction = manager.prepare_reconfigure(changed)
    api.fail_unregister_once[obsolete_id] = 5

    with pytest.raises(HotkeyCommandError):
        transaction.commit()

    assert manager.status().healthy is False
    api.emit_hotkey(api.hotkey_id(MOD_CONTROL, 0x20))
    time.sleep(0.03)
    assert calls == []
    assert manager.recover(changed).healthy is True
    manager.stop()


def test_recovery_does_not_start_second_thread_if_old_thread_will_not_stop() -> None:
    api = FakeWindowsHotkeyApi()
    calls: list[str] = []
    manager = _manager(api, calls)
    manager.start(_config())
    registration_count = len(api.register_calls)
    api.drop_next_post = True

    status = manager.recover(_config())

    assert status.healthy is False
    assert status.restart_required is True
    assert len(api.register_calls) == registration_count

    dropped_stop_id = api.post_calls[-1][2]
    api.messages.put(
        (1, WM_APP_HOTKEY_COMMAND, dropped_stop_id, 0, 0)
    )
    deadline = time.monotonic() + 1.0
    while api.registered and time.monotonic() < deadline:
        time.sleep(0.005)
    assert manager.stop() is True

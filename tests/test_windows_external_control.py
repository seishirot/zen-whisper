"""Tests for the Windows named-semaphore external command transport."""

from __future__ import annotations

import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from src.external_control import (
    AlreadyRunningError,
    ExternalCommand,
    ExternalControlError,
    SignalResult,
)
from src.platform import is_windows
from src.platform.windows_external_control import (
    SUBMIT_TOGGLE_SEMAPHORE_NAME,
    WindowsControlServer,
    _WAIT_OBJECT_0,
    _WAIT_TIMEOUT,
    signal_command,
)


class _FakeNamedSemaphoreApi:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._handles: dict[int, str] = {}
        self._next_handle = 1

    def _new_handle(self, name: str) -> int:
        handle = self._next_handle
        self._next_handle += 1
        self._handles[handle] = name
        return handle

    def create_counting_semaphore(self, name: str) -> tuple[int, bool]:
        with self._lock:
            created = name not in self._semaphores
            if created:
                self._semaphores[name] = threading.Semaphore(0)
            return self._new_handle(name), created

    def open_for_signal(self, name: str) -> int | None:
        with self._lock:
            if name not in self._semaphores:
                return None
            return self._new_handle(name)

    def release(self, handle: int) -> None:
        with self._lock:
            name = self._handles[handle]
            semaphore = self._semaphores[name]
        semaphore.release()

    def wait(self, handle: int, timeout_ms: int) -> int:
        with self._lock:
            name = self._handles[handle]
            semaphore = self._semaphores[name]
        if semaphore.acquire(timeout=timeout_ms / 1000):
            return _WAIT_OBJECT_0
        return _WAIT_TIMEOUT

    def close(self, handle: int) -> None:
        with self._lock:
            name = self._handles.pop(handle)
            if name not in self._handles.values():
                self._semaphores.pop(name)


@pytest.mark.parametrize("command", list(ExternalCommand))
def test_signal_reports_not_running(command: ExternalCommand) -> None:
    api = _FakeNamedSemaphoreApi()

    assert signal_command(command, api=api) is SignalResult.NOT_RUNNING


@pytest.mark.parametrize("command", list(ExternalCommand))
def test_signal_before_listener_start_is_delivered_once(
    command: ExternalCommand,
) -> None:
    api = _FakeNamedSemaphoreApi()
    server = WindowsControlServer.claim(api=api)
    received = threading.Event()
    calls = []

    assert signal_command(command, api=api) is SignalResult.SENT
    server.start(lambda actual: calls.append(actual) or received.set())

    assert received.wait(timeout=2.0)
    assert calls == [command]
    server.close()


def test_multiple_signals_before_listener_start_are_all_delivered() -> None:
    api = _FakeNamedSemaphoreApi()
    server = WindowsControlServer.claim(api=api)
    received = threading.Event()
    calls = []

    for _ in range(3):
        assert (
            signal_command(ExternalCommand.TOGGLE, api=api)
            is SignalResult.SENT
        )

    def callback(command: ExternalCommand) -> None:
        calls.append(command)
        if len(calls) == 3:
            received.set()

    server.start(callback)

    assert received.wait(timeout=2.0)
    assert calls == [ExternalCommand.TOGGLE] * 3
    server.close()


def test_signals_during_blocked_callback_are_queued() -> None:
    api = _FakeNamedSemaphoreApi()
    server = WindowsControlServer.claim(api=api)
    callback_started = threading.Event()
    unblock_callback = threading.Event()
    all_received = threading.Event()
    calls = []

    def callback(command: ExternalCommand) -> None:
        calls.append(command)
        if len(calls) == 1:
            callback_started.set()
            unblock_callback.wait(timeout=2.0)
        if len(calls) == 3:
            all_received.set()

    try:
        server.start(callback)
        assert (
            signal_command(ExternalCommand.TOGGLE, api=api)
            is SignalResult.SENT
        )
        assert callback_started.wait(timeout=2.0)

        for _ in range(2):
            assert (
                signal_command(ExternalCommand.TOGGLE, api=api)
                is SignalResult.SENT
            )
        unblock_callback.set()

        assert all_received.wait(timeout=2.0)
        assert calls == [ExternalCommand.TOGGLE] * 3
    finally:
        unblock_callback.set()
        server.close()


def test_second_server_claim_is_rejected_until_owner_closes() -> None:
    api = _FakeNamedSemaphoreApi()
    server = WindowsControlServer.claim(api=api)

    with pytest.raises(AlreadyRunningError):
        WindowsControlServer.claim(api=api)

    server.close()
    replacement = WindowsControlServer.claim(api=api)
    replacement.close()


def test_secondary_semaphore_collision_releases_new_primary_semaphore() -> None:
    api = _FakeNamedSemaphoreApi()
    foreign_handle, created = api.create_counting_semaphore(
        SUBMIT_TOGGLE_SEMAPHORE_NAME
    )
    assert created is True

    with pytest.raises(ExternalControlError):
        WindowsControlServer.claim(api=api)

    assert (
        signal_command(ExternalCommand.TOGGLE, api=api)
        is SignalResult.NOT_RUNNING
    )
    assert (
        signal_command(ExternalCommand.SUBMIT_TOGGLE, api=api)
        is SignalResult.SENT
    )
    api.close(foreign_handle)


def test_close_wakes_listener_without_dispatching() -> None:
    api = _FakeNamedSemaphoreApi()
    server = WindowsControlServer.claim(api=api)
    calls = []
    server.start(lambda command: calls.append(command))

    server.close()

    assert calls == []
    for command in ExternalCommand:
        assert signal_command(command, api=api) is SignalResult.NOT_RUNNING


@pytest.mark.skipif(not is_windows(), reason="Windows only")
def test_real_named_semaphore_round_trip() -> None:
    semaphore_names = {
        command: rf"Local\ZenWhisper.{command.value}.Test.{uuid.uuid4()}"
        for command in ExternalCommand
    }
    server = WindowsControlServer.claim(semaphore_names=semaphore_names)
    received = {command: threading.Event() for command in ExternalCommand}
    try:
        server.start(lambda command: received[command].set())

        for command in ExternalCommand:
            assert (
                signal_command(command, semaphore_names=semaphore_names)
                is SignalResult.SENT
            )
            assert received[command].wait(timeout=2.0)
    finally:
        server.close()

    for command in ExternalCommand:
        assert (
            signal_command(command, semaphore_names=semaphore_names)
            is SignalResult.NOT_RUNNING
        )


@pytest.mark.skipif(not is_windows(), reason="Windows only")
def test_real_named_semaphore_preserves_multiple_pending_signals() -> None:
    semaphore_names = {
        command: rf"Local\ZenWhisper.{command.value}.Test.{uuid.uuid4()}"
        for command in ExternalCommand
    }
    server = WindowsControlServer.claim(semaphore_names=semaphore_names)
    all_received = threading.Event()
    calls = []

    for _ in range(3):
        assert (
            signal_command(
                ExternalCommand.TOGGLE,
                semaphore_names=semaphore_names,
            )
            is SignalResult.SENT
        )

    def callback(command: ExternalCommand) -> None:
        calls.append(command)
        if len(calls) == 3:
            all_received.set()

    try:
        server.start(callback)
        assert all_received.wait(timeout=2.0)
        assert calls == [ExternalCommand.TOGGLE] * 3
    finally:
        server.close()


@pytest.mark.skipif(not is_windows(), reason="Windows only")
@pytest.mark.parametrize(
    "executable_name,arguments,expected_command",
    [
        ("zen-whisper-toggle.exe", [], ExternalCommand.TOGGLE),
        ("zen-whisper.exe", ["--toggle"], ExternalCommand.TOGGLE),
        (
            "zen-whisper-submit-toggle.exe",
            [],
            ExternalCommand.SUBMIT_TOGGLE,
        ),
        (
            "zen-whisper.exe",
            ["--submit-toggle"],
            ExternalCommand.SUBMIT_TOGGLE,
        ),
    ],
)
def test_installed_gui_entrypoint_signals_across_processes(
    executable_name: str,
    arguments: list[str],
    expected_command: ExternalCommand,
) -> None:
    executable = Path(sys.executable).with_name(executable_name)
    if not executable.exists():
        pytest.skip(f"installed GUI entry point is unavailable: {executable}")

    try:
        server = WindowsControlServer.claim()
    except AlreadyRunningError:
        pytest.skip(
            "a real ZenWhisper instance already owns the control semaphores"
        )

    received = threading.Event()
    commands = []
    try:
        server.start(
            lambda command: commands.append(command) or received.set()
        )
        completed = subprocess.run(
            [str(executable), *arguments],
            check=False,
            timeout=10,
        )

        assert completed.returncode == 0
        assert received.wait(timeout=2.0)
        assert commands == [expected_command]
    finally:
        server.close()


@pytest.mark.skipif(not is_windows(), reason="Windows only")
def test_duplicate_normal_gui_entrypoint_exits_without_toggling() -> None:
    executable = Path(sys.executable).with_name("zen-whisper.exe")
    if not executable.exists():
        pytest.skip(f"installed GUI entry point is unavailable: {executable}")

    try:
        server = WindowsControlServer.claim()
    except AlreadyRunningError:
        pytest.skip(
            "a real ZenWhisper instance already owns the control semaphores"
        )

    received = threading.Event()
    try:
        server.start(lambda _command: received.set())
        completed = subprocess.run(
            [str(executable)],
            check=False,
            timeout=10,
        )

        assert completed.returncode == 0
        assert received.wait(timeout=0.3) is False
    finally:
        server.close()

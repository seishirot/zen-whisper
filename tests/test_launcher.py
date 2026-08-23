"""Tests for the lightweight installed entry points."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import src.launcher as launcher
from src.external_control import (
    AlreadyRunningError,
    ExternalCommand,
    SignalResult,
)


class _Server:
    def __init__(self) -> None:
        self.callback: Callable[[ExternalCommand], None] | None = None
        self.closed = False

    def start(self, callback: Callable[[ExternalCommand], None]) -> None:
        self.callback = callback

    def close(self) -> None:
        self.closed = True


class _App:
    def __init__(self, server: _Server) -> None:
        self.server = server
        self.toggle_calls: list[bool] = []
        self.ran = False

    def _on_toggle(self, submit_after_paste: bool = False) -> None:
        self.toggle_calls.append(submit_after_paste)

    def run(self) -> None:
        self.ran = True
        assert self.server.callback is not None
        self.server.callback(ExternalCommand.TOGGLE)
        self.server.callback(ExternalCommand.SUBMIT_TOGGLE)


@pytest.mark.parametrize(
    ("arguments", "expected_command"),
    [
        (["--toggle"], ExternalCommand.TOGGLE),
        (["--submit-toggle"], ExternalCommand.SUBMIT_TOGGLE),
    ],
)
def test_command_signals_without_loading_application(
    monkeypatch,
    arguments: list[str],
    expected_command: ExternalCommand,
) -> None:
    loaded = []
    commands = []
    monkeypatch.setattr(
        launcher,
        "signal_command",
        lambda command: commands.append(command) or SignalResult.SENT,
    )
    monkeypatch.setattr(
        launcher,
        "_load_app_factory",
        lambda: loaded.append(True),
    )

    assert launcher.main(arguments) == launcher.EXIT_OK
    assert commands == [expected_command]
    assert loaded == []


@pytest.mark.parametrize(
    ("entrypoint", "expected_command"),
    [
        (launcher.toggle_main, ExternalCommand.TOGGLE),
        (launcher.submit_toggle_main, ExternalCommand.SUBMIT_TOGGLE),
    ],
)
def test_argument_free_entrypoint_signals(
    monkeypatch,
    entrypoint: Callable[[], int],
    expected_command: ExternalCommand,
) -> None:
    calls = []
    monkeypatch.setattr(
        launcher,
        "signal_command",
        lambda command: calls.append(command) or SignalResult.SENT,
    )

    assert entrypoint() == launcher.EXIT_OK
    assert calls == [expected_command]


@pytest.mark.parametrize("argument", ["--toggle", "--submit-toggle"])
def test_command_reports_missing_running_instance(
    monkeypatch,
    argument: str,
) -> None:
    monkeypatch.setattr(
        launcher,
        "signal_command",
        lambda _command: SignalResult.NOT_RUNNING,
    )

    assert launcher.main([argument]) == launcher.EXIT_NOT_RUNNING


def test_external_command_options_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc_info:
        launcher.main(["--toggle", "--submit-toggle"])

    assert exc_info.value.code == 2


def test_normal_launch_starts_receiver_and_dispatches_commands(monkeypatch) -> None:
    server = _Server()
    app = _App(server)
    monkeypatch.setattr(launcher, "claim_control_server", lambda: server)

    result = launcher.main([], app_factory=lambda: app)

    assert result == launcher.EXIT_OK
    assert app.ran is True
    assert app.toggle_calls == [False, True]
    assert server.closed is True


def test_duplicate_normal_launch_exits_without_loading_application(
    monkeypatch,
) -> None:
    loaded = []

    def already_running():
        raise AlreadyRunningError("already running")

    monkeypatch.setattr(launcher, "claim_control_server", already_running)

    assert launcher.main([], app_factory=lambda: loaded.append(True)) == 0
    assert loaded == []


def test_receiver_is_closed_when_application_raises(monkeypatch) -> None:
    server = _Server()
    monkeypatch.setattr(launcher, "claim_control_server", lambda: server)

    class FailingApp(_App):
        def run(self) -> None:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        launcher.run_application(lambda: FailingApp(server))

    assert server.closed is True

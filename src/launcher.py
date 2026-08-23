"""Lightweight installed entry points for ZenWhisper."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from typing import Protocol

from src.external_control import (
    AlreadyRunningError,
    ExternalCommand,
    ExternalControlServer,
    ExternalControlError,
    SignalResult,
    claim_control_server,
    signal_command,
)

logger = logging.getLogger("zen-whisper")

# Windows gui_scripts have no standard streams. argparse and a few imported
# libraries still expect file-like objects to exist.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

EXIT_OK = 0
EXIT_NOT_RUNNING = 3
EXIT_CONTROL_ERROR = 4
EXIT_UNSUPPORTED = 5


class _Application(Protocol):
    def _on_toggle(self, submit_after_paste: bool = False) -> None: ...

    def run(self) -> None: ...


def _write_error(message: str) -> None:
    try:
        if sys.stderr is not None:
            sys.stderr.write(message.rstrip() + "\n")
            sys.stderr.flush()
    except Exception:
        pass


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="zen-whisper")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument(
        "--toggle",
        action="store_true",
        help="起動済みZenWhisperの録音状態を切り替えて終了します",
    )
    commands.add_argument(
        "--submit-toggle",
        action="store_true",
        help="起動済みZenWhisperの録音状態を切り替え、停止時にEnterを送信します",
    )
    return parser.parse_args(argv)


def _execute_command(command: ExternalCommand) -> int:
    try:
        result = signal_command(command)
    except ExternalControlError as exc:
        _write_error(f"zen-whisper: 外部コマンド送信に失敗しました: {exc}")
        return EXIT_CONTROL_ERROR
    except OSError as exc:
        _write_error(f"zen-whisper: 外部コマンド送信に失敗しました: {exc}")
        return EXIT_CONTROL_ERROR

    if result is SignalResult.SENT:
        return EXIT_OK
    if result is SignalResult.NOT_RUNNING:
        _write_error("zen-whisper: 起動済みのZenWhisperが見つかりません")
        return EXIT_NOT_RUNNING

    _write_error("zen-whisper: 外部コマンドはWindowsでのみ利用できます")
    return EXIT_UNSUPPORTED


def _load_app_factory() -> Callable[[], _Application]:
    # Keep the command-only process light: application dependencies are imported
    # only after command parsing and primary-instance ownership succeeds.
    from src.main import App

    return App


def run_application(
    app_factory: Callable[[], _Application] | None = None,
) -> int:
    server: ExternalControlServer | None = None
    try:
        try:
            server = claim_control_server()
        except AlreadyRunningError:
            return EXIT_OK
        except (ExternalControlError, OSError) as exc:
            _write_error(
                f"zen-whisper: 外部コマンド受信の初期化に失敗しました: {exc}"
            )
            return EXIT_CONTROL_ERROR

        factory = app_factory or _load_app_factory()
        app = factory()

        if server is not None:
            def on_external_command(command: ExternalCommand) -> None:
                submit_after_paste = command is ExternalCommand.SUBMIT_TOGGLE
                logger.info(
                    "外部コマンドを受信しました: command=%s",
                    command.value,
                )
                app._on_toggle(submit_after_paste=submit_after_paste)

            server.start(on_external_command)
            logger.info("外部コマンドの受信を開始しました")

        app.run()
        return EXIT_OK
    finally:
        if server is not None:
            server.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    app_factory: Callable[[], _Application] | None = None,
) -> int:
    """Normal GUI entry point, with an optional lightweight command mode."""
    args = _parse_args(argv)
    if args.toggle:
        return _execute_command(ExternalCommand.TOGGLE)
    if args.submit_toggle:
        return _execute_command(ExternalCommand.SUBMIT_TOGGLE)
    return run_application(app_factory)


def toggle_main() -> int:
    """Argument-free GUI entry point for launchers such as Logi Options+."""
    return _execute_command(ExternalCommand.TOGGLE)


def submit_toggle_main() -> int:
    """Argument-free submit-toggle entry point for application launchers."""
    return _execute_command(ExternalCommand.SUBMIT_TOGGLE)


if __name__ == "__main__":
    raise SystemExit(main())

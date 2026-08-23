"""External commands that control the running ZenWhisper process."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Protocol

from src.platform import is_windows


class ExternalControlError(RuntimeError):
    """Raised when the local control channel cannot be used safely."""


class AlreadyRunningError(ExternalControlError):
    """Raised when another ZenWhisper instance owns the control channel."""


class ExternalCommand(Enum):
    """Commands accepted by a running ZenWhisper instance."""

    TOGGLE = "toggle"
    SUBMIT_TOGGLE = "submit_toggle"


class SignalResult(Enum):
    """Outcome of signaling a running ZenWhisper instance."""

    SENT = "sent"
    NOT_RUNNING = "not_running"
    UNSUPPORTED = "unsupported"


class ExternalControlServer(Protocol):
    """Lifecycle exposed by the platform-specific command receiver."""

    def start(self, callback: Callable[[ExternalCommand], None]) -> None: ...

    def close(self) -> None: ...


def claim_control_server() -> ExternalControlServer | None:
    """Claim the per-session command channels for the primary app instance."""
    if not is_windows():
        return None

    from src.platform.windows_external_control import WindowsControlServer

    return WindowsControlServer.claim()


def signal_command(command: ExternalCommand) -> SignalResult:
    """Signal a command without synthesizing keyboard input."""
    if not is_windows():
        return SignalResult.UNSUPPORTED

    from src.platform.windows_external_control import signal_command as windows_signal

    return windows_signal(command)

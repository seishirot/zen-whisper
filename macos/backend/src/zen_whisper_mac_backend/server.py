"""Unix-domain socket entrypoint for the native macOS backend."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

from zen_whisper_mac_backend.logging_config import setup_logging
from zen_whisper_mac_backend.protocol import (
    MAX_LINE_BYTES,
    decode_line,
    encode_message,
    error_response,
)
from zen_whisper_mac_backend.service import BackendService

logger = logging.getLogger(__name__)
MAX_HANDLER_THREADS = 8


def _parent_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return True
    if os.getppid() == 1 and parent_pid != 1:
        return False
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _start_parent_monitor(service: BackendService, parent_pid: int | None) -> None:
    if parent_pid is None:
        return

    def monitor() -> None:
        while not service.should_shutdown:
            if not _parent_is_alive(parent_pid):
                logger.info("Parent process exited; shutting down backend")
                service.should_shutdown = True
                return
            time.sleep(0.5)

    threading.Thread(target=monitor, daemon=True).start()


def _handle_connection(
    connection: socket.socket,
    service: BackendService,
    handler_slots: threading.BoundedSemaphore,
) -> None:
    try:
        with connection:
            connection.settimeout(1.0)
            reader = connection.makefile("rb")
            writer = connection.makefile("wb")
            try:
                line = reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                request = decode_line(line)
                response = service.handle(request)
            except socket.timeout:
                logger.info("Protocol error: client read timed out")
                return
            except Exception as exc:
                logger.info("Protocol error: %s", exc)
                response = error_response(
                    "unknown",
                    "PROTOCOL_ERROR",
                    str(exc),
                    recoverable=True,
                )
            try:
                writer.write(encode_message(response))
                writer.flush()
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                logger.info("Client disconnected before response could be sent")
    finally:
        handler_slots.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--log-dir")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--auth-token-stdin", action="store_true")
    args = parser.parse_args(argv)

    app_support = Path(
        os.environ.get(
            "ZEN_WHISPER_APP_SUPPORT",
            str(Path.home() / "Library/Application Support/zen-whisper"),
        )
    )
    log_dir = Path(args.log_dir) if args.log_dir else Path.home() / "Library/Logs/zen-whisper"
    setup_logging(log_dir)
    auth_token = sys.stdin.readline().rstrip("\n") if args.auth_token_stdin else None
    if not auth_token:
        logger.error("backend auth token is required")
        return 2

    socket_path = Path(args.socket_path)
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    app_support.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)

    service = BackendService(auth_token=auth_token)
    _start_parent_monitor(service, args.parent_pid)
    handler_slots = threading.BoundedSemaphore(MAX_HANDLER_THREADS)
    logger.info("Starting backend socket at %s", socket_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(16)
        server.settimeout(0.5)
        while not service.should_shutdown:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            if not handler_slots.acquire(blocking=False):
                logger.info("Protocol error: too many concurrent clients")
                connection.close()
                continue
            thread = threading.Thread(
                target=_handle_connection,
                args=(connection, service, handler_slots),
                daemon=True,
            )
            thread.start()
    socket_path.unlink(missing_ok=True)
    logger.info("Backend stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

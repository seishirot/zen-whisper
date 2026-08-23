"""Windows named-semaphore transport for local ZenWhisper commands."""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Mapping, Protocol

from src.external_control import (
    AlreadyRunningError,
    ExternalCommand,
    ExternalControlError,
    SignalResult,
)

logger = logging.getLogger(__name__)

# Local\ keeps separate interactive/RDP logon sessions from controlling each
# other. V2 uses counting semaphores so rapid launcher actions cannot coalesce.
TOGGLE_SEMAPHORE_NAME = r"Local\ZenWhisper.Toggle.v2"
SUBMIT_TOGGLE_SEMAPHORE_NAME = r"Local\ZenWhisper.SubmitToggle.v2"
CONTROL_SEMAPHORE_NAMES: Mapping[ExternalCommand, str] = {
    ExternalCommand.TOGGLE: TOGGLE_SEMAPHORE_NAME,
    ExternalCommand.SUBMIT_TOGGLE: SUBMIT_TOGGLE_SEMAPHORE_NAME,
}

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ALREADY_EXISTS = 183
_SEMAPHORE_MODIFY_STATE = 0x0002
_MAX_PENDING_SIGNALS = 0x7FFFFFFF
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_WAIT_SLICE_MS = 200

SemaphoreHandle = int


class _NamedSemaphoreApi(Protocol):
    def create_counting_semaphore(
        self,
        name: str,
    ) -> tuple[SemaphoreHandle, bool]: ...

    def open_for_signal(self, name: str) -> SemaphoreHandle | None: ...

    def release(self, handle: SemaphoreHandle) -> None: ...

    def wait(self, handle: SemaphoreHandle, timeout_ms: int) -> int: ...

    def close(self, handle: SemaphoreHandle) -> None: ...


class _Win32NamedSemaphoreApi:
    """Small ctypes wrapper kept isolated from the cross-platform facade."""

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._create_semaphore = kernel32.CreateSemaphoreW
        self._create_semaphore.argtypes = [
            wintypes.LPVOID,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.LPCWSTR,
        ]
        self._create_semaphore.restype = wintypes.HANDLE

        self._open_semaphore = kernel32.OpenSemaphoreW
        self._open_semaphore.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._open_semaphore.restype = wintypes.HANDLE

        self._release_semaphore = kernel32.ReleaseSemaphore
        self._release_semaphore.argtypes = [
            wintypes.HANDLE,
            wintypes.LONG,
            ctypes.POINTER(wintypes.LONG),
        ]
        self._release_semaphore.restype = wintypes.BOOL

        self._wait_for_single_object = kernel32.WaitForSingleObject
        self._wait_for_single_object.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        self._wait_for_single_object.restype = wintypes.DWORD

        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

    @staticmethod
    def _error(operation: str, code: int | None = None) -> OSError:
        error_code = ctypes.get_last_error() if code is None else code
        return OSError(
            error_code,
            f"{operation}: {ctypes.FormatError(error_code).strip()}",
        )

    def create_counting_semaphore(
        self,
        name: str,
    ) -> tuple[SemaphoreHandle, bool]:
        ctypes.set_last_error(0)
        handle = self._create_semaphore(
            None,
            0,
            _MAX_PENDING_SIGNALS,
            name,
        )
        if not handle:
            raise self._error("CreateSemaphoreW failed")
        existed = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        return int(handle), not existed

    def open_for_signal(self, name: str) -> SemaphoreHandle | None:
        ctypes.set_last_error(0)
        handle = self._open_semaphore(_SEMAPHORE_MODIFY_STATE, False, name)
        if handle:
            return int(handle)
        error_code = ctypes.get_last_error()
        if error_code == _ERROR_FILE_NOT_FOUND:
            return None
        raise self._error("OpenSemaphoreW failed", error_code)

    def release(self, handle: SemaphoreHandle) -> None:
        if not self._release_semaphore(handle, 1, None):
            raise self._error("ReleaseSemaphore failed")

    def wait(self, handle: SemaphoreHandle, timeout_ms: int) -> int:
        result = int(self._wait_for_single_object(handle, timeout_ms))
        if result == _WAIT_FAILED:
            raise self._error("WaitForSingleObject failed")
        return result

    def close(self, handle: SemaphoreHandle) -> None:
        if not self._close_handle(handle):
            raise self._error("CloseHandle failed")


def _default_api() -> _NamedSemaphoreApi:
    return _Win32NamedSemaphoreApi()


class WindowsControlServer:
    """Own and wait on the primary instance's counting command semaphores."""

    def __init__(
        self,
        api: _NamedSemaphoreApi,
        handles: Mapping[ExternalCommand, SemaphoreHandle],
    ) -> None:
        self._api = api
        self._handles = dict(handles)
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._closed = False

    @classmethod
    def claim(
        cls,
        *,
        semaphore_names: Mapping[
            ExternalCommand, str
        ] = CONTROL_SEMAPHORE_NAMES,
        api: _NamedSemaphoreApi | None = None,
    ) -> WindowsControlServer:
        semaphore_api = api or _default_api()
        handles: dict[ExternalCommand, SemaphoreHandle] = {}
        try:
            for command in ExternalCommand:
                handle, created = semaphore_api.create_counting_semaphore(
                    semaphore_names[command]
                )
                if not created:
                    try:
                        semaphore_api.close(handle)
                    except OSError:
                        logger.debug(
                            "既存外部コマンドsemaphoreのhandleを閉じられませんでした",
                            exc_info=True,
                        )
                    if command is ExternalCommand.TOGGLE:
                        raise AlreadyRunningError("ZenWhisperは既に起動しています")
                    raise ExternalControlError(
                        "貼り付け＋Enter用の外部コマンドsemaphoreが既に存在します"
                    )
                handles[command] = handle
        except Exception:
            for owned_handle in handles.values():
                try:
                    semaphore_api.close(owned_handle)
                except OSError:
                    logger.debug(
                        "初期化失敗時に外部コマンドhandleを閉じられませんでした",
                        exc_info=True,
                    )
            raise
        return cls(semaphore_api, handles)

    def start(self, callback: Callable[[ExternalCommand], None]) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise ExternalControlError(
                    "終了済みの外部コマンド受信を再開できません"
                )
            if self._threads:
                raise ExternalControlError(
                    "外部コマンド受信は既に開始しています"
                )
            self._threads = [
                threading.Thread(
                    target=self._wait_loop,
                    args=(command, handle, callback),
                    name=f"zen-whisper-external-{command.value}",
                    daemon=True,
                )
                for command, handle in self._handles.items()
            ]
            for thread in self._threads:
                thread.start()

    def _wait_loop(
        self,
        command: ExternalCommand,
        handle: SemaphoreHandle,
        callback: Callable[[ExternalCommand], None],
    ) -> None:
        while not self._stop_event.is_set():
            try:
                result = self._api.wait(handle, _WAIT_SLICE_MS)
            except OSError:
                if not self._stop_event.is_set():
                    logger.exception(
                        "外部コマンドsemaphoreの待機に失敗しました: command=%s",
                        command.value,
                    )
                return

            if self._stop_event.is_set():
                return
            if result == _WAIT_TIMEOUT:
                continue
            if result != _WAIT_OBJECT_0:
                logger.error(
                    "外部コマンドsemaphoreが予期しない待機結果を返しました: "
                    "command=%s, result=0x%08X",
                    command.value,
                    result,
                )
                return

            try:
                callback(command)
            except Exception:
                logger.exception(
                    "外部コマンドコールバックの実行に失敗しました: command=%s",
                    command.value,
                )

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_event.set()
            threads = list(self._threads)

        for handle in self._handles.values():
            try:
                self._api.release(handle)
            except OSError:
                logger.debug(
                    "終了時に外部コマンドsemaphoreを解放できませんでした",
                    exc_info=True,
                )

        current_thread = threading.current_thread()
        for thread in threads:
            if thread is not current_thread:
                thread.join(timeout=2.0)
        if any(thread.is_alive() for thread in threads):
            logger.warning(
                "外部コマンド受信スレッドを時間内に終了できませんでした。"
                "待機中handleはプロセス終了時に解放されます"
            )
            # Closing a handle while WaitForSingleObject is pending has
            # undefined behavior. Leaking until process exit is safer.
            return

        for handle in self._handles.values():
            try:
                self._api.close(handle)
            except OSError:
                logger.warning(
                    "外部コマンドsemaphoreのhandleを閉じられませんでした",
                    exc_info=True,
                )


def signal_command(
    command: ExternalCommand,
    *,
    semaphore_names: Mapping[ExternalCommand, str] = CONTROL_SEMAPHORE_NAMES,
    api: _NamedSemaphoreApi | None = None,
) -> SignalResult:
    """Queue one command using only semaphore modify-state access."""
    semaphore_api = api or _default_api()
    handle = semaphore_api.open_for_signal(semaphore_names[command])
    if handle is None:
        return SignalResult.NOT_RUNNING
    try:
        semaphore_api.release(handle)
        return SignalResult.SENT
    finally:
        try:
            semaphore_api.close(handle)
        except OSError:
            logger.debug(
                "送信側の外部コマンドhandleを閉じられませんでした",
                exc_info=True,
            )

"""Windows ``RegisterHotKey`` controller.

All Win32 registration calls are owned by one message-loop thread.  The
controller exposes a small two-phase transaction so config.toml can remain the
authoritative source if saving or runtime reconfiguration fails.
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import threading
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
WM_APP_HOTKEY_COMMAND = 0x8000 + 0x5A
PM_NOREMOVE = 0x0000

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MIN_HOTKEY_ID = 0x1000
_MAX_HOTKEY_ID = 0xBFFF


@dataclass(frozen=True)
class HotkeyRuntimeStatus:
    """Current health of the host-side hotkey runtime."""

    healthy: bool
    message: str = ""
    restart_required: bool = False


class HotkeyRuntimeError(RuntimeError):
    """Base error for runtime registration and command failures."""

    def __init__(
        self,
        message: str,
        *,
        state_unknown: bool = False,
        restart_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.state_unknown = state_unknown
        self.restart_required = restart_required


class HotkeyRegistrationError(HotkeyRuntimeError):
    """A concrete shortcut could not be registered."""

    def __init__(self, combo: str, winerror: int, detail: str = "") -> None:
        self.combo = combo
        self.winerror = winerror
        suffix = f": {detail}" if detail else ""
        super().__init__(
            f"ホットキー「{combo}」を登録できませんでした "
            f"(Win32 error {winerror}{suffix})"
        )


class HotkeyCommandError(HotkeyRuntimeError):
    """The message-loop command result is unknown or failed."""


@dataclass(frozen=True)
class WindowsHotkeyBinding:
    """One parsed shortcut and its application action."""

    combo: str
    modifiers: int
    virtual_key: int
    action: str
    callback: Callable[[], None] = field(compare=False, repr=False)

    @property
    def key(self) -> tuple[int, int]:
        return self.modifiers, self.virtual_key


class WindowsHotkeyApi(Protocol):
    """Small Win32 seam used by the controller and its tests."""

    def ensure_message_queue(self) -> None: ...

    def current_thread_id(self) -> int: ...

    def register_hot_key(
        self,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> tuple[bool, int]: ...

    def unregister_hot_key(self, hotkey_id: int) -> tuple[bool, int]: ...

    def post_thread_message(
        self,
        thread_id: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> tuple[bool, int]: ...

    def get_message(self) -> tuple[int, int, int, int, int]:
        """Return ``(result, message, wparam, lparam, winerror)``."""
        ...

    def format_error(self, winerror: int) -> str: ...


class _MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("lPrivate", wintypes.DWORD),
    )


class CtypesWindowsHotkeyApi:
    """ctypes-backed Win32 adapter.  Construct only on Windows."""

    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._user32.PeekMessageW.argtypes = (
            ctypes.POINTER(_MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = (
            ctypes.POINTER(_MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.RegisterHotKey.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
        )
        self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self._user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        self._kernel32.GetCurrentThreadId.argtypes = ()
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def ensure_message_queue(self) -> None:
        message = _MSG()
        ctypes.set_last_error(0)
        self._user32.PeekMessageW(
            ctypes.byref(message),
            None,
            0,
            0,
            PM_NOREMOVE,
        )

    def current_thread_id(self) -> int:
        return int(self._kernel32.GetCurrentThreadId())

    def register_hot_key(
        self,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> tuple[bool, int]:
        ctypes.set_last_error(0)
        succeeded = bool(
            self._user32.RegisterHotKey(
                None,
                hotkey_id,
                modifiers,
                virtual_key,
            )
        )
        return succeeded, 0 if succeeded else ctypes.get_last_error()

    def unregister_hot_key(self, hotkey_id: int) -> tuple[bool, int]:
        ctypes.set_last_error(0)
        succeeded = bool(self._user32.UnregisterHotKey(None, hotkey_id))
        return succeeded, 0 if succeeded else ctypes.get_last_error()

    def post_thread_message(
        self,
        thread_id: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> tuple[bool, int]:
        ctypes.set_last_error(0)
        succeeded = bool(
            self._user32.PostThreadMessageW(
                thread_id,
                message,
                wparam,
                lparam,
            )
        )
        return succeeded, 0 if succeeded else ctypes.get_last_error()

    def get_message(self) -> tuple[int, int, int, int, int]:
        message = _MSG()
        ctypes.set_last_error(0)
        result = int(self._user32.GetMessageW(ctypes.byref(message), None, 0, 0))
        winerror = ctypes.get_last_error() if result == -1 else 0
        return (
            result,
            int(message.message),
            int(message.wParam),
            int(message.lParam),
            winerror,
        )

    def format_error(self, winerror: int) -> str:
        if not winerror:
            return "不明なWin32エラー"
        try:
            return ctypes.FormatError(winerror).strip()
        except (OSError, ValueError):
            return "Win32エラーの詳細を取得できません"


@dataclass
class _ControllerCommand:
    command_id: int
    kind: str
    bindings: tuple[WindowsHotkeyBinding, ...] = ()
    transaction_id: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    result: object | None = None
    error: BaseException | None = None


@dataclass
class _PreparedState:
    transaction_id: int
    desired: tuple[WindowsHotkeyBinding, ...]
    staged: dict[tuple[int, int], int]
    previous_status: HotkeyRuntimeStatus


class WindowsHotkeyTransaction:
    """Prepared runtime registration changes awaiting save/abort."""

    def __init__(
        self,
        controller: WindowsHotkeyController,
        transaction_id: int,
    ) -> None:
        self._controller = controller
        self._transaction_id = transaction_id
        self._closed = False

    def commit(self) -> None:
        if self._closed:
            raise HotkeyRuntimeError("ホットキー再設定は既に完了しています")
        try:
            self._controller._finish_reconfigure(
                "commit",
                self._transaction_id,
            )
        finally:
            self._closed = True

    def abort(self) -> None:
        if self._closed:
            return
        try:
            self._controller._finish_reconfigure(
                "abort",
                self._transaction_id,
            )
        finally:
            self._closed = True


class WindowsHotkeyController:
    """Own ``RegisterHotKey`` registrations and the ``WM_HOTKEY`` loop."""

    def __init__(
        self,
        api: WindowsHotkeyApi | None = None,
        *,
        command_timeout: float = 5.0,
        settle_timeout: float = 1.0,
        join_timeout: float = 5.0,
    ) -> None:
        self._api = api or CtypesWindowsHotkeyApi()
        self._command_timeout = command_timeout
        self._settle_timeout = settle_timeout
        self._join_timeout = join_timeout

        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._ready = threading.Event()
        self._start_done = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._status = HotkeyRuntimeStatus(False, "ホットキーは未起動です")
        self._runtime_error_callback: (
            Callable[[HotkeyRuntimeStatus], None] | None
        ) = None
        self._dispatch_enabled = False
        self._generation = 0
        self._normal_stop = False

        self._command_ids = itertools.count(1)
        self._transaction_ids = itertools.count(1)
        self._next_hotkey_id = _MIN_HOTKEY_ID
        self._pending: dict[int, _ControllerCommand] = {}
        self._uncertain_command: _ControllerCommand | None = None

        # The following dictionaries are owned by the message-loop thread.
        self._registered: dict[int, WindowsHotkeyBinding] = {}
        self._active: dict[tuple[int, int], tuple[int, WindowsHotkeyBinding]] = {}
        self._dispatch: dict[int, tuple[int, WindowsHotkeyBinding]] = {}
        self._prepared: _PreparedState | None = None

    def status(self) -> HotkeyRuntimeStatus:
        with self._state_lock:
            return self._status

    def set_runtime_error_callback(
        self,
        callback: Callable[[HotkeyRuntimeStatus], None] | None,
    ) -> None:
        """Install the user-visible notifier after startup status is checked."""
        with self._state_lock:
            self._runtime_error_callback = callback

    def start(
        self,
        bindings: Sequence[WindowsHotkeyBinding],
    ) -> HotkeyRuntimeStatus:
        desired = tuple(bindings)
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                raise HotkeyRuntimeError("Windowsホットキーは既に起動しています")
            self._start_thread_locked(desired)
            return self.status()

    def prepare_reconfigure(
        self,
        bindings: Sequence[WindowsHotkeyBinding],
    ) -> WindowsHotkeyTransaction:
        transaction_id = next(self._transaction_ids)
        command = self._submit(
            "prepare",
            bindings=tuple(bindings),
            transaction_id=transaction_id,
        )
        if command.result != transaction_id:
            raise HotkeyRuntimeError("ホットキー再設定の応答が一致しません")
        return WindowsHotkeyTransaction(self, transaction_id)

    def recover(
        self,
        bindings: Sequence[WindowsHotkeyBinding],
    ) -> HotkeyRuntimeStatus:
        """Rebuild from an authoritative config after an uncertain command."""
        desired = tuple(bindings)
        with self._lifecycle_lock:
            with self._state_lock:
                self._dispatch_enabled = False
            uncertain = self._uncertain_command
            if uncertain is not None and not uncertain.done.is_set():
                uncertain.done.wait(self._settle_timeout)
            if not self._stop_thread_locked():
                return self.status()
            self._uncertain_command = None
            self._start_thread_locked(desired)
            return self.status()

    def stop(self) -> bool:
        """Idempotently unregister every shortcut and join the owner thread."""
        with self._lifecycle_lock:
            with self._state_lock:
                self._dispatch_enabled = False
            return self._stop_thread_locked()

    def _start_thread_locked(
        self,
        bindings: tuple[WindowsHotkeyBinding, ...],
    ) -> None:
        self._ready = threading.Event()
        self._start_done = threading.Event()
        self._thread_id = 0
        self._normal_stop = False
        self._registered = {}
        self._active = {}
        self._dispatch = {}
        self._prepared = None
        with self._state_lock:
            self._dispatch_enabled = False
            self._status = HotkeyRuntimeStatus(False, "ホットキーを登録中です")
        thread = threading.Thread(
            target=self._thread_main,
            args=(bindings,),
            daemon=True,
            name="zen-whisper-hotkey",
        )
        self._thread = thread
        thread.start()
        if not self._ready.wait(self._command_timeout):
            self._set_degraded(
                "Windowsホットキーのメッセージキューを準備できませんでした",
                restart_required=True,
            )
            return
        if not self._start_done.wait(self._command_timeout):
            self._set_degraded(
                "Windowsホットキーの初期登録が完了しませんでした",
                restart_required=True,
            )

    def _stop_thread_locked(self) -> bool:
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._thread = None
            return True

        command = _ControllerCommand(next(self._command_ids), "stop")
        delivered = self._post_command(command)
        if delivered:
            command.done.wait(self._command_timeout)
        thread.join(self._join_timeout)
        if thread.is_alive():
            self._set_degraded(
                "旧ホットキースレッドを停止できません。ZenWhisperを再起動してください",
                restart_required=True,
            )
            return False
        self._thread = None
        return True

    def _submit(
        self,
        kind: str,
        *,
        bindings: tuple[WindowsHotkeyBinding, ...] = (),
        transaction_id: int = 0,
    ) -> _ControllerCommand:
        command = _ControllerCommand(
            next(self._command_ids),
            kind,
            bindings,
            transaction_id,
        )
        if not self._post_command(command):
            raise command.error or HotkeyCommandError(
                "ホットキー制御コマンドを配送できません",
                state_unknown=True,
            )
        if not command.done.wait(self._command_timeout):
            self._uncertain_command = command
            error = HotkeyCommandError(
                f"ホットキー制御コマンド {command.command_id} がタイムアウトしました",
                state_unknown=True,
            )
            self._set_degraded(str(error))
            raise error
        if command.error is not None:
            raise command.error
        return command

    def _finish_reconfigure(self, kind: str, transaction_id: int) -> None:
        self._submit(kind, transaction_id=transaction_id)

    def _post_command(self, command: _ControllerCommand) -> bool:
        with self._state_lock:
            thread_id = self._thread_id
        if not thread_id:
            error = HotkeyCommandError(
                "ホットキーのメッセージスレッドが起動していません",
                state_unknown=True,
                restart_required=True,
            )
            command.error = error
            command.done.set()
            self._set_degraded(str(error), restart_required=True)
            return False
        with self._pending_lock:
            self._pending[command.command_id] = command
        succeeded, winerror = self._api.post_thread_message(
            thread_id,
            WM_APP_HOTKEY_COMMAND,
            command.command_id,
            0,
        )
        if succeeded:
            return True
        detail = self._api.format_error(winerror)
        error = HotkeyCommandError(
            "ホットキー制御コマンドを配送できませんでした "
            f"(Win32 error {winerror}: {detail})",
            state_unknown=True,
        )
        command.error = error
        self._uncertain_command = command
        self._set_degraded(str(error))
        return False

    def _thread_main(
        self,
        initial: tuple[WindowsHotkeyBinding, ...],
    ) -> None:
        try:
            self._api.ensure_message_queue()
            with self._state_lock:
                self._thread_id = self._api.current_thread_id()
            self._ready.set()
            try:
                self._owner_install_initial(initial)
            except HotkeyRuntimeError as exc:
                self._set_degraded(str(exc), restart_required=exc.restart_required)
                logger.error("Windowsホットキー初期登録失敗: %s", exc)
            finally:
                self._start_done.set()

            while True:
                result, message, wparam, lparam, winerror = self._api.get_message()
                if result == -1:
                    detail = self._api.format_error(winerror)
                    raise HotkeyCommandError(
                        "Windowsホットキーメッセージの受信に失敗しました "
                        f"(Win32 error {winerror}: {detail})",
                        state_unknown=True,
                        restart_required=True,
                    )
                if result == 0:
                    break
                if message == WM_APP_HOTKEY_COMMAND:
                    if not self._owner_execute_command(wparam):
                        break
                elif message == WM_HOTKEY:
                    self._owner_dispatch_hotkey(wparam, lparam)
        except BaseException as exc:
            if not self._normal_stop:
                logger.exception("Windowsホットキースレッドが停止しました")
                self._set_degraded(str(exc), restart_required=True)
                self._notify_runtime_error()
        finally:
            self._owner_unregister_everything()
            with self._state_lock:
                self._dispatch_enabled = False
                self._thread_id = 0
                if self._normal_stop:
                    self._status = HotkeyRuntimeStatus(False, "ホットキーを停止しました")
            self._ready.set()
            self._start_done.set()
            self._fail_pending_commands()

    def _owner_install_initial(
        self,
        bindings: tuple[WindowsHotkeyBinding, ...],
    ) -> None:
        staged: dict[tuple[int, int], int] = {}
        try:
            for binding in bindings:
                staged[binding.key] = self._owner_register(binding)
        except HotkeyRegistrationError as exc:
            rollback_errors = self._owner_unregister_ids(staged.values())
            if rollback_errors:
                raise HotkeyCommandError(
                    f"{exc}。部分登録の解除にも失敗しました: "
                    + " / ".join(rollback_errors),
                    state_unknown=True,
                    restart_required=True,
                ) from exc
            raise

        self._generation += 1
        self._active = {
            binding.key: (staged[binding.key], binding)
            for binding in bindings
        }
        self._dispatch = {
            hotkey_id: (self._generation, binding)
            for hotkey_id, binding in self._active.values()
        }
        self._set_healthy("Windowsホットキーを登録しました")

    def _owner_execute_command(self, command_id: int) -> bool:
        with self._pending_lock:
            command = self._pending.pop(command_id, None)
        if command is None:
            logger.warning("未知のホットキーコマンドを無視します: %s", command_id)
            return True
        keep_running = True
        try:
            if command.kind == "prepare":
                command.result = self._owner_prepare(
                    command.bindings,
                    command.transaction_id,
                )
            elif command.kind == "commit":
                self._owner_commit(command.transaction_id)
            elif command.kind == "abort":
                self._owner_abort(command.transaction_id)
            elif command.kind == "stop":
                self._normal_stop = True
                with self._state_lock:
                    self._dispatch_enabled = False
                keep_running = False
            else:
                raise HotkeyRuntimeError(
                    f"未知のホットキーコマンドです: {command.kind}"
                )
        except BaseException as exc:
            command.error = exc
        finally:
            command.done.set()
        return keep_running

    def _owner_prepare(
        self,
        desired: tuple[WindowsHotkeyBinding, ...],
        transaction_id: int,
    ) -> int:
        if self._prepared is not None:
            raise HotkeyRuntimeError("別のホットキー再設定が処理中です")
        previous_status = self.status()
        desired_keys = {binding.key for binding in desired}
        staged: dict[tuple[int, int], int] = {}
        try:
            for binding in desired:
                if binding.key not in self._active:
                    staged[binding.key] = self._owner_register(binding)
        except HotkeyRegistrationError as exc:
            rollback_errors = self._owner_unregister_ids(staged.values())
            if rollback_errors:
                error = HotkeyCommandError(
                    f"{exc}。追加登録の解除にも失敗しました: "
                    + " / ".join(rollback_errors),
                    state_unknown=True,
                )
                self._set_degraded(str(error))
                raise error from exc
            self._restore_status(previous_status)
            raise

        # The desired set is read here to make duplicate registration bugs
        # visible during tests even though validation normally rejects them.
        if len(desired_keys) != len(desired):
            self._owner_unregister_ids(staged.values())
            self._restore_status(previous_status)
            raise HotkeyRuntimeError("ホットキー登録内容が重複しています")

        self._prepared = _PreparedState(
            transaction_id,
            desired,
            staged,
            previous_status,
        )
        with self._state_lock:
            self._dispatch_enabled = False
        return transaction_id

    def _owner_commit(self, transaction_id: int) -> None:
        prepared = self._require_prepared(transaction_id)
        desired_keys = {binding.key for binding in prepared.desired}
        next_active: dict[
            tuple[int, int], tuple[int, WindowsHotkeyBinding]
        ] = {}
        for binding in prepared.desired:
            current = self._active.get(binding.key)
            hotkey_id = (
                current[0]
                if current is not None
                else prepared.staged[binding.key]
            )
            next_active[binding.key] = (hotkey_id, binding)

        obsolete_ids = [
            hotkey_id
            for key, (hotkey_id, _binding) in self._active.items()
            if key not in desired_keys
        ]
        self._generation += 1
        self._active = next_active
        self._dispatch = {
            hotkey_id: (self._generation, binding)
            for hotkey_id, binding in next_active.values()
        }
        self._prepared = None

        errors = self._owner_unregister_ids(obsolete_ids)
        if errors:
            error = HotkeyCommandError(
                "不要な旧ホットキーを解除できませんでした: "
                + " / ".join(errors),
                state_unknown=True,
            )
            self._set_degraded(str(error))
            raise error
        self._set_healthy("Windowsホットキーの変更を反映しました")

    def _owner_abort(self, transaction_id: int) -> None:
        prepared = self._require_prepared(transaction_id)
        self._prepared = None
        errors = self._owner_unregister_ids(prepared.staged.values())
        if errors:
            error = HotkeyCommandError(
                "追加したホットキーを取り消せませんでした: "
                + " / ".join(errors),
                state_unknown=True,
            )
            self._set_degraded(str(error))
            raise error
        self._restore_status(prepared.previous_status)

    def _require_prepared(self, transaction_id: int) -> _PreparedState:
        prepared = self._prepared
        if prepared is None or prepared.transaction_id != transaction_id:
            raise HotkeyRuntimeError("ホットキー再設定トランザクションが一致しません")
        return prepared

    def _owner_register(self, binding: WindowsHotkeyBinding) -> int:
        hotkey_id = self._owner_allocate_id()
        succeeded, winerror = self._api.register_hot_key(
            hotkey_id,
            binding.modifiers | MOD_NOREPEAT,
            binding.virtual_key,
        )
        if not succeeded:
            raise HotkeyRegistrationError(
                binding.combo,
                winerror,
                self._api.format_error(winerror),
            )
        self._registered[hotkey_id] = binding
        return hotkey_id

    def _owner_allocate_id(self) -> int:
        for _ in range(_MAX_HOTKEY_ID - _MIN_HOTKEY_ID + 1):
            candidate = self._next_hotkey_id
            self._next_hotkey_id += 1
            if self._next_hotkey_id > _MAX_HOTKEY_ID:
                self._next_hotkey_id = _MIN_HOTKEY_ID
            if candidate not in self._registered:
                return candidate
        raise HotkeyRuntimeError("利用可能なWindowsホットキーIDがありません")

    def _owner_unregister_ids(self, hotkey_ids: Sequence[int]) -> list[str]:
        errors: list[str] = []
        for hotkey_id in tuple(hotkey_ids):
            if hotkey_id not in self._registered:
                continue
            succeeded, winerror = self._api.unregister_hot_key(hotkey_id)
            if succeeded:
                self._registered.pop(hotkey_id, None)
                continue
            errors.append(
                f"ID {hotkey_id} (Win32 error {winerror}: "
                f"{self._api.format_error(winerror)})"
            )
        return errors

    def _owner_unregister_everything(self) -> None:
        errors = self._owner_unregister_ids(tuple(self._registered))
        if errors:
            logger.error("ホットキー終了時の解除失敗: %s", " / ".join(errors))
        self._prepared = None
        self._active = {}
        self._dispatch = {}

    def _owner_dispatch_hotkey(self, hotkey_id: int, lparam: int) -> None:
        dispatch = self._dispatch.get(hotkey_id)
        modifiers = lparam & 0xFFFF
        virtual_key = (lparam >> 16) & 0xFFFF
        if dispatch is None:
            logger.warning(
                "未登録のWM_HOTKEYを受信: id=%d, modifiers=0x%04X, vk=0x%04X",
                hotkey_id,
                modifiers,
                virtual_key,
            )
            return
        _generation, observed_binding = dispatch
        logger.info(
            "WM_HOTKEY受信: action=%s, combo=%s, id=%d, "
            "modifiers=0x%04X, vk=0x%04X",
            observed_binding.action,
            observed_binding.combo,
            hotkey_id,
            modifiers,
            virtual_key,
        )
        with self._state_lock:
            enabled = self._dispatch_enabled and self._status.healthy
        if not enabled or self._prepared is not None:
            return
        generation, binding = dispatch
        threading.Thread(
            target=self._dispatch_callback,
            args=(generation, binding),
            daemon=True,
            name=f"zen-whisper-hotkey-{binding.action}",
        ).start()

    def _dispatch_callback(
        self,
        generation: int,
        binding: WindowsHotkeyBinding,
    ) -> None:
        with self._state_lock:
            if (
                not self._dispatch_enabled
                or not self._status.healthy
                or generation != self._generation
            ):
                return
        try:
            binding.callback()
        except Exception:
            logger.exception("ホットキーaction実行失敗: %s", binding.action)

    def _restore_status(self, status: HotkeyRuntimeStatus) -> None:
        with self._state_lock:
            self._status = status
            self._dispatch_enabled = status.healthy

    def _set_healthy(self, message: str) -> None:
        with self._state_lock:
            self._status = HotkeyRuntimeStatus(True, message)
            self._dispatch_enabled = True

    def _set_degraded(
        self,
        message: str,
        *,
        restart_required: bool = False,
    ) -> None:
        with self._state_lock:
            self._status = HotkeyRuntimeStatus(
                False,
                message,
                restart_required,
            )
            self._dispatch_enabled = False

    def _notify_runtime_error(self) -> None:
        with self._state_lock:
            callback = self._runtime_error_callback
            status = self._status
        if callback is None:
            return
        try:
            callback(status)
        except Exception:
            logger.exception("Windowsホットキー停止通知に失敗しました")

    def _fail_pending_commands(self) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for command in pending:
            if command.error is None:
                command.error = HotkeyCommandError(
                    "ホットキースレッドがコマンド完了前に停止しました",
                    state_unknown=True,
                    restart_required=True,
                )
            command.done.set()

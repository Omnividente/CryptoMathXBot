from __future__ import annotations

import errno
import hashlib
import importlib
import os
import threading
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO


class AlreadyRunningError(RuntimeError):
    """Raised when another bot process owns the instance lock."""


class InstanceLockError(RuntimeError):
    """Raised when the instance lock cannot be created or inspected."""



_HELD_PATHS: set[Path] = set()
_HELD_PATHS_LOCK = threading.Lock()
_ERROR_ALREADY_EXISTS = 183


class SingleInstanceLock:
    """An OS-backed lock released automatically when the process exits.

    Windows uses a named kernel mutex because ``msvcrt.locking`` prevents even
    diagnostic reads of the lock file and has surprising same-process handle
    semantics. POSIX keeps the simple advisory ``flock`` implementation.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._normalized_path = path.resolve(strict=False)
        self._file: BinaryIO | None = None
        self._mutex_handle: int | None = None

    def __enter__(self) -> SingleInstanceLock:
        with _HELD_PATHS_LOCK:
            if self._normalized_path in _HELD_PATHS:
                raise AlreadyRunningError("another CryptoMathXBot instance is running")
            _HELD_PATHS.add(self._normalized_path)

        handle: BinaryIO | None = None
        try:
            if os.name == "nt":
                self._mutex_handle = _acquire_windows_mutex(self._normalized_path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("a+b")
            if os.name != "nt":
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                _lock_posix_file(handle)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
        except AlreadyRunningError:
            if handle is not None:
                handle.close()
            self._release_windows_mutex()
            with _HELD_PATHS_LOCK:
                _HELD_PATHS.discard(self._normalized_path)
            raise
        except OSError as exc:
            if handle is not None:
                handle.close()
            self._release_windows_mutex()
            with _HELD_PATHS_LOCK:
                _HELD_PATHS.discard(self._normalized_path)
            raise InstanceLockError("CryptoMathXBot instance lock is unavailable") from exc

        self._file = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._file is not None:
                try:
                    if os.name != "nt":
                        _unlock_posix_file(self._file)
                finally:
                    self._file.close()
                    self._file = None
            self._release_windows_mutex()
        finally:
            with _HELD_PATHS_LOCK:
                _HELD_PATHS.discard(self._normalized_path)

    def _release_windows_mutex(self) -> None:
        if self._mutex_handle is None:
            return
        _close_windows_mutex(self._mutex_handle)
        self._mutex_handle = None


def _lock_posix_file(handle: BinaryIO) -> None:
    fcntl: Any = importlib.import_module("fcntl")

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise AlreadyRunningError("another CryptoMathXBot instance is running") from exc
        raise


def _unlock_posix_file(handle: BinaryIO) -> None:
    fcntl: Any = importlib.import_module("fcntl")

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_windows_mutex(path: Path) -> int:
    ctypes: Any = importlib.import_module("ctypes")
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    mutex_name = _windows_mutex_name(path)
    handle = kernel32.CreateMutexW(None, True, mutex_name)
    if not handle:
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise AlreadyRunningError("another CryptoMathXBot instance is running")
    value = int(handle)
    if value == 0:
        kernel32.CloseHandle(handle)
        raise OSError("CreateMutexW returned an invalid handle")
    return value


def _windows_mutex_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).casefold().encode("utf-8")).hexdigest()
    return "Global\\CryptoMathXBot-" + digest


def _close_windows_mutex(handle: int) -> None:
    ctypes: Any = importlib.import_module("ctypes")
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle(wintypes.HANDLE(handle))

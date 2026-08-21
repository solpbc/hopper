# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Exclusive, process-local capability for Hopper state-file authority."""

from __future__ import annotations

import errno as errno_module
import fcntl
import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NoReturn

_PID_READ_LIMIT = 256
_TYPE_DISPLAY_LIMIT = 128
_MESSAGE_DISPLAY_LIMIT = 512
_PID_DISPLAY_LIMIT = 512


class AuthorityStatus(str, Enum):
    """Finite acquisition outcomes."""

    ACQUIRED = "acquired"
    HELD = "held"
    FAILED = "failed"


class ReleaseStatus(str, Enum):
    """Finite release outcomes."""

    RELEASED = "released"
    RELEASE_UNKNOWN = "release_unknown"


class AuthorityOperation(str, Enum):
    """Non-overlapping operations reported by the primitive."""

    ACQUIRE = "acquire"
    PREPARE_PID = "prepare_pid"
    OPEN = "open"
    FLOCK = "flock"
    READ_PID = "read_pid"
    SEEK = "seek"
    TRUNCATE = "truncate"
    WRITE_PID = "write_pid"
    CLEANUP_CLOSE = "cleanup_close"
    RELEASE = "release"
    UNLOCK = "unlock"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class AuthorityDiagnostic:
    """Immutable, bounded exception facts safe to render in a terminal."""

    operation: AuthorityOperation
    exception_type: str
    message: str
    errno: int


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    """Immutable terminal release observation."""

    status: ReleaseStatus
    path: Path
    operation: AuthorityOperation
    diagnostics: tuple[AuthorityDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """Immutable acquisition observation and, on success, its sole capability."""

    status: AuthorityStatus
    path: Path
    operation: AuthorityOperation
    diagnostics: tuple[AuthorityDiagnostic, ...] = ()
    prior_pid_text: str = ""
    handle: StateAuthorityHandle | None = None


def _escape_text(value: object, limit: int) -> str:
    """Return a bounded ASCII rendering with every control byte escaped."""
    try:
        text = str(value)
    except Exception:
        text = "<unprintable>"
    escaped = text.encode("unicode_escape", errors="backslashreplace").decode("ascii")
    return escaped[:limit]


def _diagnostic(operation: AuthorityOperation, error: Exception) -> AuthorityDiagnostic:
    error_type = f"{type(error).__module__}.{type(error).__qualname__}"
    try:
        raw_errno = getattr(error, "errno", None)
    except Exception:
        raw_errno = None
    return AuthorityDiagnostic(
        operation=operation,
        exception_type=_escape_text(error_type, _TYPE_DISPLAY_LIMIT),
        message=_escape_text(error, _MESSAGE_DISPLAY_LIMIT),
        errno=raw_errno if isinstance(raw_errno, int) else 0,
    )


def _read_prior_pid(fd: int) -> tuple[str, AuthorityDiagnostic | None]:
    try:
        raw = os.pread(fd, _PID_READ_LIMIT, 0)
    except Exception as error:
        return "", _diagnostic(AuthorityOperation.READ_PID, error)
    decoded = raw.decode("utf-8", errors="backslashreplace")
    return _escape_text(decoded, _PID_DISPLAY_LIMIT), None


def _close_after_unsuccessful_acquire(
    fd: int,
) -> tuple[AuthorityDiagnostic, ...]:
    try:
        os.close(fd)
    except Exception as error:
        return (_diagnostic(AuthorityOperation.CLEANUP_CLOSE, error),)
    return ()


class StateAuthorityHandle:
    """Opaque, noncopyable authority capability with linearized release."""

    __slots__ = (
        "_condition",
        "_fd",
        "_path",
        "_release_result",
        "_release_started",
    )

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd
        self._condition = threading.Condition()
        self._release_started = False
        self._release_result: ReleaseResult | None = None

    def __repr__(self) -> str:
        return "<StateAuthorityHandle>"

    def __copy__(self) -> NoReturn:
        raise TypeError("state authority capability cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("state authority capability cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("state authority capability cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        del protocol
        raise TypeError("state authority capability cannot be serialized")

    def release(self) -> ReleaseResult:
        """Release once; concurrent and later callers share one terminal result."""
        with self._condition:
            if self._release_result is not None:
                return self._release_result
            if self._release_started:
                while self._release_result is None:
                    self._condition.wait()
                return self._release_result
            self._release_started = True
            fd = self._fd

        diagnostics: list[AuthorityDiagnostic] = []
        unlock_succeeded = False
        close_succeeded = False
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            unlock_succeeded = True
        except Exception as error:
            diagnostics.append(_diagnostic(AuthorityOperation.UNLOCK, error))

        try:
            os.close(fd)
            close_succeeded = True
        except Exception as error:
            diagnostics.append(_diagnostic(AuthorityOperation.CLOSE, error))

        status = (
            ReleaseStatus.RELEASED
            if unlock_succeeded or close_succeeded
            else ReleaseStatus.RELEASE_UNKNOWN
        )
        result = ReleaseResult(
            status=status,
            path=self._path,
            operation=AuthorityOperation.RELEASE,
            diagnostics=tuple(diagnostics),
        )
        with self._condition:
            self._release_result = result
            self._condition.notify_all()
        return result


def acquire_state_authority(
    path: str | os.PathLike[str], *, pid: int | None = None
) -> AcquisitionResult:
    """Attempt nonblocking exclusive authority over one persistent state inode."""
    exact_path = Path(path)
    try:
        pid_value = os.getpid() if pid is None else pid
        if isinstance(pid_value, bool) or not isinstance(pid_value, int) or pid_value <= 0:
            raise ValueError("PID must be a positive integer")
        pid_bytes = str(pid_value).encode("ascii")
    except Exception as error:
        return AcquisitionResult(
            status=AuthorityStatus.FAILED,
            path=exact_path,
            operation=AuthorityOperation.PREPARE_PID,
            diagnostics=(_diagnostic(AuthorityOperation.PREPARE_PID, error),),
        )

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        fd = os.open(exact_path, flags, 0o600)
    except Exception as error:
        return AcquisitionResult(
            status=AuthorityStatus.FAILED,
            path=exact_path,
            operation=AuthorityOperation.OPEN,
            diagnostics=(_diagnostic(AuthorityOperation.OPEN, error),),
        )

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception as error:
        held = isinstance(error, BlockingIOError) or (
            isinstance(error, OSError) and error.errno in {errno_module.EACCES, errno_module.EAGAIN}
        )
        prior_pid_text = ""
        diagnostics: list[AuthorityDiagnostic] = []
        if held:
            prior_pid_text, read_diagnostic = _read_prior_pid(fd)
            if read_diagnostic is not None:
                diagnostics.append(read_diagnostic)
        else:
            diagnostics.append(_diagnostic(AuthorityOperation.FLOCK, error))
        diagnostics.extend(_close_after_unsuccessful_acquire(fd))
        return AcquisitionResult(
            status=AuthorityStatus.HELD if held else AuthorityStatus.FAILED,
            path=exact_path,
            operation=AuthorityOperation.FLOCK,
            diagnostics=tuple(diagnostics),
            prior_pid_text=prior_pid_text,
        )

    handle = StateAuthorityHandle(exact_path, fd)
    diagnostics = []
    prior_pid_text, read_diagnostic = _read_prior_pid(fd)
    if read_diagnostic is not None:
        diagnostics.append(read_diagnostic)

    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except Exception as error:
        diagnostics.append(_diagnostic(AuthorityOperation.SEEK, error))
        return AcquisitionResult(
            AuthorityStatus.ACQUIRED,
            exact_path,
            AuthorityOperation.ACQUIRE,
            tuple(diagnostics),
            prior_pid_text,
            handle,
        )

    try:
        os.ftruncate(fd, 0)
    except Exception as error:
        diagnostics.append(_diagnostic(AuthorityOperation.TRUNCATE, error))
        return AcquisitionResult(
            AuthorityStatus.ACQUIRED,
            exact_path,
            AuthorityOperation.ACQUIRE,
            tuple(diagnostics),
            prior_pid_text,
            handle,
        )

    written = 0
    while written < len(pid_bytes):
        try:
            count = os.write(fd, pid_bytes[written:])
            if count <= 0:
                raise OSError(errno_module.EIO, "PID write made no progress")
            written += count
        except Exception as error:
            diagnostics.append(_diagnostic(AuthorityOperation.WRITE_PID, error))
            break

    return AcquisitionResult(
        status=AuthorityStatus.ACQUIRED,
        path=exact_path,
        operation=AuthorityOperation.ACQUIRE,
        diagnostics=tuple(diagnostics),
        prior_pid_text=prior_pid_text,
        handle=handle,
    )

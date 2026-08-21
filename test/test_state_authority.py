# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import copy
import errno
import fcntl
import gc
import os
import pickle
import threading
from pathlib import Path

import pytest

from hopper.state_authority import (
    AcquisitionResult,
    AuthorityOperation,
    AuthorityStatus,
    ReleaseStatus,
    acquire_state_authority,
)


def _old_open_and_lock(path: Path):
    stream = open(path, "a+")
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return stream


def _old_contender(path: Path) -> bool:
    stream = open(path, "a+")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return False
    stream.close()
    return True


def _assert_acquired(result: AcquisitionResult):
    assert result.status is AuthorityStatus.ACQUIRED
    assert result.operation is AuthorityOperation.ACQUIRE
    assert result.handle is not None
    return result.handle


def test_acquire_preserves_inode_writes_pid_and_excludes_old_contender(tmp_path):
    path = tmp_path / "server.pid"
    path.write_bytes(b"impossible sentinel")
    inode = path.stat().st_ino

    result = acquire_state_authority(path, pid=314159)
    handle = _assert_acquired(result)

    assert result.path == path
    assert result.prior_pid_text == "impossible sentinel"
    assert result.diagnostics == ()
    assert path.stat().st_ino == inode
    assert path.read_bytes() == b"314159"
    assert not _old_contender(path)

    released = handle.release()
    assert released.status is ReleaseStatus.RELEASED
    assert released.path == path
    assert released.operation is AuthorityOperation.RELEASE
    assert released.diagnostics == ()
    assert _old_contender(path)


def test_old_holder_returns_held_without_mutation_or_retained_authority(tmp_path):
    path = tmp_path / "server.pid"
    path.write_bytes(b"9876")
    inode = path.stat().st_ino
    owner = _old_open_and_lock(path)
    try:
        result = acquire_state_authority(path, pid=1234)
        assert result.status is AuthorityStatus.HELD
        assert result.operation is AuthorityOperation.FLOCK
        assert result.handle is None
        assert result.path == path
        assert result.prior_pid_text == "9876"
        assert path.read_bytes() == b"9876"
        assert path.stat().st_ino == inode
    finally:
        owner.close()

    assert _old_contender(path)


def test_open_failure_is_handleless_and_bounded(tmp_path):
    path = tmp_path / "missing" / "server.pid"
    result = acquire_state_authority(path)

    assert result.status is AuthorityStatus.FAILED
    assert result.operation is AuthorityOperation.OPEN
    assert result.handle is None
    assert result.path == path
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.operation is AuthorityOperation.OPEN
    assert diagnostic.errno == errno.ENOENT
    assert len(diagnostic.exception_type) <= 128
    assert len(diagnostic.message) <= 512


@pytest.mark.parametrize("pid", [0, -1, True, "123"])
def test_invalid_pid_fails_before_open(tmp_path, monkeypatch, pid):
    path = tmp_path / "server.pid"
    opens = []
    monkeypatch.setattr(
        "hopper.state_authority.os.open", lambda *args, **kwargs: opens.append((args, kwargs))
    )

    result = acquire_state_authority(path, pid=pid)

    assert result.status is AuthorityStatus.FAILED
    assert result.operation is AuthorityOperation.PREPARE_PID
    assert result.handle is None
    assert [item.operation for item in result.diagnostics] == [AuthorityOperation.PREPARE_PID]
    assert opens == []


def test_nonheld_flock_failure_closes_descriptor_and_preserves_inode(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    path.write_bytes(b"sentinel")
    inode = path.stat().st_ino
    real_close = os.close
    closed: list[int] = []

    def fail_flock(fd, flags):
        del fd, flags
        raise OSError(errno.EBADF, "bad flock")

    def observed_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr("hopper.state_authority.fcntl.flock", fail_flock)
    monkeypatch.setattr("hopper.state_authority.os.close", observed_close)
    result = acquire_state_authority(path)

    assert result.status is AuthorityStatus.FAILED
    assert result.operation is AuthorityOperation.FLOCK
    assert result.handle is None
    assert [item.operation for item in result.diagnostics] == [AuthorityOperation.FLOCK]
    assert result.diagnostics[0].errno == errno.EBADF
    assert len(closed) == 1
    assert path.read_bytes() == b"sentinel"
    assert path.stat().st_ino == inode


def test_held_cleanup_close_diagnostic_cannot_grant_authority(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    path.write_text("42")
    owner = _old_open_and_lock(path)
    real_close = os.close

    def close_then_fail(fd):
        real_close(fd)
        raise OSError(errno.EIO, "close after delegate")

    monkeypatch.setattr("hopper.state_authority.os.close", close_then_fail)
    try:
        result = acquire_state_authority(path)
    finally:
        owner.close()

    assert result.status is AuthorityStatus.HELD
    assert result.handle is None
    assert [item.operation for item in result.diagnostics] == [AuthorityOperation.CLEANUP_CLOSE]
    assert _old_contender(path)


@pytest.mark.parametrize(
    ("coordinate", "expected_operation", "expected_bytes"),
    [
        ("read", AuthorityOperation.READ_PID, b"77"),
        ("seek", AuthorityOperation.SEEK, b"prior"),
        ("truncate", AuthorityOperation.TRUNCATE, b"prior"),
        ("write_zero", AuthorityOperation.WRITE_PID, b""),
        ("write_fault", AuthorityOperation.WRITE_PID, b"7"),
    ],
)
def test_postflock_metadata_fault_retains_authority(
    tmp_path, monkeypatch, coordinate, expected_operation, expected_bytes
):
    path = tmp_path / "server.pid"
    path.write_bytes(b"prior")
    real_pread = os.pread
    real_lseek = os.lseek
    real_ftruncate = os.ftruncate
    real_write = os.write
    writes = 0

    if coordinate == "read":
        monkeypatch.setattr(
            "hopper.state_authority.os.pread",
            lambda fd, size, offset: (_ for _ in ()).throw(OSError(errno.EIO, "read fault")),
        )
    elif coordinate == "seek":
        monkeypatch.setattr(
            "hopper.state_authority.os.lseek",
            lambda fd, offset, whence: (_ for _ in ()).throw(OSError(errno.EIO, "seek fault")),
        )
    elif coordinate == "truncate":
        monkeypatch.setattr(
            "hopper.state_authority.os.ftruncate",
            lambda fd, size: (_ for _ in ()).throw(OSError(errno.EIO, "truncate fault")),
        )
    elif coordinate == "write_zero":
        monkeypatch.setattr("hopper.state_authority.os.write", lambda fd, data: 0)
    else:

        def short_then_fault(fd, data):
            nonlocal writes
            writes += 1
            if writes == 1:
                return real_write(fd, data[:1])
            raise OSError(errno.ENOSPC, "write fault")

        monkeypatch.setattr("hopper.state_authority.os.write", short_then_fault)

    result = acquire_state_authority(path, pid=77)
    handle = _assert_acquired(result)
    assert [item.operation for item in result.diagnostics] == [expected_operation]
    assert path.read_bytes() == expected_bytes
    assert not _old_contender(path)

    with monkeypatch.context() as restore:
        restore.setattr("hopper.state_authority.os.pread", real_pread)
        restore.setattr("hopper.state_authority.os.lseek", real_lseek)
        restore.setattr("hopper.state_authority.os.ftruncate", real_ftruncate)
        restore.setattr("hopper.state_authority.os.write", real_write)
        assert handle.release().status is ReleaseStatus.RELEASED
    assert _old_contender(path)


def test_pid_write_loops_over_real_positive_counts(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    real_write = os.write
    offered: list[bytes] = []

    def short_write(fd, data):
        offered.append(bytes(data))
        amount = 2 if len(data) > 2 else len(data)
        return real_write(fd, data[:amount])

    monkeypatch.setattr("hopper.state_authority.os.write", short_write)
    result = acquire_state_authority(path, pid=1234567)
    handle = _assert_acquired(result)

    assert result.diagnostics == ()
    assert path.read_bytes() == b"1234567"
    assert offered == [b"1234567", b"34567", b"567", b"7"]
    handle.release()


@pytest.mark.parametrize("size", [255, 256, 257])
def test_prior_pid_read_is_bounded_at_descriptor(tmp_path, monkeypatch, size):
    path = tmp_path / "server.pid"
    path.write_bytes(b"x" * size)
    owner = _old_open_and_lock(path)
    real_pread = os.pread
    calls: list[tuple[int, int]] = []

    def bounded_pread(fd, maximum, offset):
        calls.append((maximum, offset))
        return real_pread(fd, maximum, offset)

    monkeypatch.setattr("hopper.state_authority.os.pread", bounded_pread)
    try:
        result = acquire_state_authority(path)
    finally:
        owner.close()

    assert result.status is AuthorityStatus.HELD
    assert calls == [(256, 0)]
    assert len(result.prior_pid_text) == min(size, 256)


@pytest.mark.parametrize(
    ("raw", "expected_length"),
    [
        ((b"\n" * 255) + b"x", 511),
        (b"\n" * 256, 512),
        (b"\xff" * 256, 512),
    ],
)
def test_prior_pid_text_is_control_safe_and_capped_after_escaping(tmp_path, raw, expected_length):
    path = tmp_path / "server.pid"
    path.write_bytes(raw)
    owner = _old_open_and_lock(path)
    try:
        result = acquire_state_authority(path)
    finally:
        owner.close()

    assert len(result.prior_pid_text) == expected_length
    assert all(ord(character) >= 32 for character in result.prior_pid_text)


@pytest.mark.parametrize(
    "raw",
    [b"", b"not-a-pid", b"-7", b"9" * 400, b"\x00\n\r", b"\xff\xfe"],
)
def test_untrusted_prior_pid_text_never_changes_held_authority(tmp_path, raw):
    path = tmp_path / "server.pid"
    path.write_bytes(raw)
    owner = _old_open_and_lock(path)
    try:
        result = acquire_state_authority(path)
    finally:
        owner.close()

    assert result.status is AuthorityStatus.HELD
    assert result.operation is AuthorityOperation.FLOCK
    assert result.handle is None


def test_unreadable_prior_pid_diagnostic_keeps_held_result(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    path.write_bytes(b"42")
    owner = _old_open_and_lock(path)
    monkeypatch.setattr(
        "hopper.state_authority.os.pread",
        lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "unreadable")),
    )
    try:
        result = acquire_state_authority(path)
    finally:
        owner.close()

    assert result.status is AuthorityStatus.HELD
    assert result.handle is None
    assert result.prior_pid_text == ""
    assert [item.operation for item in result.diagnostics] == [AuthorityOperation.READ_PID]


@pytest.mark.parametrize("unlock_mode", ["success", "fail_before", "fail_after"])
@pytest.mark.parametrize("close_mode", ["success", "fail_before", "fail_after"])
def test_release_truth_matrix_uses_real_kernel_state(
    tmp_path, monkeypatch, unlock_mode, close_mode
):
    path = tmp_path / "server.pid"
    captured_fds: list[int] = []
    real_open = os.open
    real_close = os.close
    real_flock = fcntl.flock

    def observed_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        captured_fds.append(fd)
        return fd

    monkeypatch.setattr("hopper.state_authority.os.open", observed_open)
    result = acquire_state_authority(path, pid=11)
    handle = _assert_acquired(result)
    target_fd = captured_fds[0]

    def injected_flock(fd, flags):
        assert fd == target_fd
        assert flags == fcntl.LOCK_UN
        if unlock_mode == "fail_before":
            raise OSError(errno.EIO, "unlock before")
        real_flock(fd, flags)
        if unlock_mode == "fail_after":
            raise OSError(errno.EIO, "unlock after")

    def injected_close(fd):
        assert fd == target_fd
        if close_mode == "fail_before":
            raise OSError(errno.EIO, "close before")
        real_close(fd)
        if close_mode == "fail_after":
            raise OSError(errno.EIO, "close after")

    monkeypatch.setattr("hopper.state_authority.fcntl.flock", injected_flock)
    monkeypatch.setattr("hopper.state_authority.os.close", injected_close)
    terminal = handle.release()

    expected_released = unlock_mode == "success" or close_mode == "success"
    assert terminal.status is (
        ReleaseStatus.RELEASED if expected_released else ReleaseStatus.RELEASE_UNKNOWN
    )
    expected_operations = []
    if unlock_mode != "success":
        expected_operations.append(AuthorityOperation.UNLOCK)
    if close_mode != "success":
        expected_operations.append(AuthorityOperation.CLOSE)
    assert [item.operation for item in terminal.diagnostics] == expected_operations

    actual_free = unlock_mode != "fail_before" or close_mode != "fail_before"
    contender = real_open(path, os.O_RDWR)
    try:
        if actual_free:
            real_flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            with pytest.raises(BlockingIOError):
                real_flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        real_close(contender)
        if close_mode == "fail_before":
            real_close(target_fd)


@pytest.mark.parametrize("coordinate", ["read", "seek", "truncate", "write"])
def test_every_metadata_fault_has_conservative_unknown_release(tmp_path, monkeypatch, coordinate):
    path = tmp_path / "server.pid"
    path.write_bytes(b"prior")
    if coordinate == "read":
        monkeypatch.setattr(
            "hopper.state_authority.os.pread",
            lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "read fault")),
        )
    elif coordinate == "seek":
        monkeypatch.setattr(
            "hopper.state_authority.os.lseek",
            lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "seek fault")),
        )
    elif coordinate == "truncate":
        monkeypatch.setattr(
            "hopper.state_authority.os.ftruncate",
            lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "truncate fault")),
        )
    else:
        monkeypatch.setattr("hopper.state_authority.os.write", lambda *args: 0)

    result = acquire_state_authority(path, pid=77)
    handle = _assert_acquired(result)
    target_fd = handle._fd
    real_close = os.close
    real_flock = fcntl.flock

    monkeypatch.setattr(
        "hopper.state_authority.fcntl.flock",
        lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "unlock before")),
    )
    monkeypatch.setattr(
        "hopper.state_authority.os.close",
        lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "close before")),
    )
    terminal = handle.release()

    assert terminal.status is ReleaseStatus.RELEASE_UNKNOWN
    contender = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            real_flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        real_close(contender)
        real_close(target_fd)


@pytest.mark.parametrize("coordinate", ["read", "seek", "truncate", "write"])
def test_every_metadata_fault_is_content_safe_after_unlock(tmp_path, monkeypatch, coordinate):
    path = tmp_path / "server.pid"
    path.write_bytes(b"prior")
    real_ftruncate = os.ftruncate
    real_write = os.write
    real_close = os.close
    real_flock = fcntl.flock
    if coordinate == "read":
        monkeypatch.setattr(
            "hopper.state_authority.os.pread",
            lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "read fault")),
        )
    elif coordinate == "seek":
        monkeypatch.setattr(
            "hopper.state_authority.os.lseek",
            lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "seek fault")),
        )
    elif coordinate == "truncate":
        monkeypatch.setattr(
            "hopper.state_authority.os.ftruncate",
            lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "truncate fault")),
        )
    else:
        monkeypatch.setattr("hopper.state_authority.os.write", lambda *args: 0)

    result = acquire_state_authority(path, pid=77)
    handle = _assert_acquired(result)
    target_fd = handle._fd
    close_entered = threading.Event()
    permit_close = threading.Event()

    def paused_close(fd):
        assert fd == target_fd
        close_entered.set()
        assert permit_close.wait(5)
        real_close(fd)

    monkeypatch.setattr("hopper.state_authority.os.close", paused_close)
    releases = []
    thread = threading.Thread(target=lambda: releases.append(handle.release()))
    thread.start()
    assert close_entered.wait(5)

    successor_fd = os.open(path, os.O_RDWR)
    real_flock(successor_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    real_ftruncate(successor_fd, 0)
    real_write(successor_fd, b"successor sentinel")
    permit_close.set()
    thread.join(5)

    assert not thread.is_alive()
    assert releases[0].status is ReleaseStatus.RELEASED
    assert path.read_bytes() == b"successor sentinel"
    real_flock(successor_fd, fcntl.LOCK_UN)
    real_close(successor_fd)


def test_unlock_to_close_boundary_cannot_overwrite_successor(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    result = acquire_state_authority(path, pid=1)
    handle = _assert_acquired(result)
    target_fd = handle._fd
    real_close = os.close
    close_entered = threading.Event()
    permit_close = threading.Event()

    def paused_close(fd):
        assert fd == target_fd
        close_entered.set()
        assert permit_close.wait(5)
        real_close(fd)

    monkeypatch.setattr("hopper.state_authority.os.close", paused_close)
    releases = []
    capability = [handle]
    thread = threading.Thread(target=lambda: releases.append(capability[0].release()))
    thread.start()
    assert close_entered.wait(5)

    successor_fd = os.open(path, os.O_RDWR)
    fcntl.flock(successor_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(successor_fd, 0)
    os.write(successor_fd, b"successor sentinel")
    permit_close.set()
    thread.join(5)
    assert not thread.is_alive()
    capability.clear()
    result = None
    handle = None
    thread = None
    gc.collect()
    assert path.read_bytes() == b"successor sentinel"
    fcntl.flock(successor_fd, fcntl.LOCK_UN)
    real_close(successor_fd)
    assert releases[0].status is ReleaseStatus.RELEASED


def test_release_unknown_survives_handle_destruction_and_gc(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    result = acquire_state_authority(path, pid=1)
    handle = _assert_acquired(result)
    target_fd = handle._fd
    real_close = os.close
    real_flock = fcntl.flock

    def fail_unlock(fd, flags):
        assert fd == target_fd
        assert flags == fcntl.LOCK_UN
        raise OSError(errno.EIO, "unlock before")

    def fail_close(fd):
        assert fd == target_fd
        raise OSError(errno.EIO, "close before")

    monkeypatch.setattr("hopper.state_authority.fcntl.flock", fail_unlock)
    monkeypatch.setattr("hopper.state_authority.os.close", fail_close)
    terminal = handle.release()
    assert terminal.status is ReleaseStatus.RELEASE_UNKNOWN
    del result, handle
    gc.collect()

    contender = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            real_flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        real_close(contender)
        real_close(target_fd)


def test_concurrent_release_is_exactly_once_and_cannot_touch_reused_fd(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    decoy_path = tmp_path / "decoy"
    decoy_path.write_bytes(b"decoy sentinel")
    result = acquire_state_authority(path, pid=1)
    handle = _assert_acquired(result)
    target_fd = handle._fd
    real_close = os.close
    real_flock = fcntl.flock
    unlock_entered = threading.Event()
    permit_unlock = threading.Event()
    calls: list[tuple[str, int]] = []
    decoy_fds: list[int] = []

    def paused_unlock(fd, flags):
        calls.append(("unlock", fd))
        unlock_entered.set()
        assert permit_unlock.wait(5)
        real_flock(fd, flags)

    def close_and_reuse(fd):
        calls.append(("close", fd))
        real_close(fd)
        decoy_fd = os.open(decoy_path, os.O_RDWR)
        assert decoy_fd == target_fd
        decoy_fds.append(decoy_fd)

    monkeypatch.setattr("hopper.state_authority.fcntl.flock", paused_unlock)
    monkeypatch.setattr("hopper.state_authority.os.close", close_and_reuse)
    results = []
    first = threading.Thread(target=lambda: results.append(handle.release()))
    second = threading.Thread(target=lambda: results.append(handle.release()))
    first.start()
    assert unlock_entered.wait(5)
    second.start()
    permit_unlock.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2
    assert results[0] is results[1]
    assert calls == [("unlock", target_fd), ("close", target_fd)]
    assert handle.release() is results[0]
    assert calls == [("unlock", target_fd), ("close", target_fd)]
    assert decoy_path.read_bytes() == b"decoy sentinel"
    real_close(decoy_fds[0])


def test_diagnostics_are_snapshots_control_safe_and_errno_preserving(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    source = OSError(errno.ENOSPC, ("bad\n" * 300))

    def fail_open(*args, **kwargs):
        del args, kwargs
        raise source

    monkeypatch.setattr("hopper.state_authority.os.open", fail_open)
    result = acquire_state_authority(path)
    diagnostic = result.diagnostics[0]
    before = diagnostic
    source.args = ("changed",)

    assert diagnostic == before
    assert diagnostic.errno == errno.ENOSPC
    assert "\n" not in diagnostic.message
    assert r"\n" in diagnostic.message
    assert len(diagnostic.exception_type) <= 128
    assert len(diagnostic.message) == 512


def test_diagnostic_normalization_survives_hostile_exception_properties(tmp_path, monkeypatch):
    class HostileError(Exception):
        @property
        def errno(self):
            raise RuntimeError("errno property escaped")

        def __str__(self):
            raise RuntimeError("string conversion escaped")

    def fail_open(*args, **kwargs):
        del args, kwargs
        raise HostileError()

    monkeypatch.setattr("hopper.state_authority.os.open", fail_open)
    result = acquire_state_authority(tmp_path / "server.pid")

    assert result.status is AuthorityStatus.FAILED
    assert result.diagnostics[0].message == "<unprintable>"
    assert result.diagnostics[0].errno == 0


def test_acquired_capability_cannot_be_copied_or_serialized(tmp_path):
    result = acquire_state_authority(tmp_path / "server.pid")
    handle = _assert_acquired(result)
    try:
        with pytest.raises(TypeError, match="cannot be copied"):
            copy.copy(handle)
        with pytest.raises(TypeError, match="cannot be copied"):
            copy.deepcopy(handle)
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(handle)
        assert repr(handle) == "<StateAuthorityHandle>"
    finally:
        handle.release()


def test_release_result_is_reused_for_later_calls(tmp_path):
    result = acquire_state_authority(tmp_path / "server.pid")
    handle = _assert_acquired(result)
    first = handle.release()
    assert handle.release() is first


def test_new_holder_excludes_frozen_old_style_contender(tmp_path):
    path = tmp_path / "server.pid"
    result = acquire_state_authority(path)
    handle = _assert_acquired(result)
    assert not _old_contender(path)
    assert handle.release().status is ReleaseStatus.RELEASED
    assert _old_contender(path)


def test_production_graph_does_not_import_inert_primitive():
    package = Path(__file__).parents[1] / "hopper"
    roots = sorted(package.glob("*.py"))
    assert len(roots) >= 25
    offenders = []
    for path in roots:
        if path.name == "state_authority.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "hopper.state_authority":
                offenders.append(path.name)
            if isinstance(node, ast.Import) and any(
                alias.name == "hopper.state_authority" for alias in node.names
            ):
                offenders.append(path.name)
    assert offenders == []

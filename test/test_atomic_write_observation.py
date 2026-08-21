# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Focused contracts for atomic JSONL write observation and compatibility."""

import builtins
import copy
import errno
import json
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hopper import lodes

OLD_BYTES = b'{"id": "old"}\n'
FOREIGN_BYTES = b"foreign writer temp\n"


def _target_path(candidate) -> Path | None:
    """Resolve a path-like spy argument, or None when it cannot be a target."""
    if isinstance(candidate, (str, os.PathLike)):
        return Path(candidate)
    return None


class DelegatingStream:
    """Proxy one real stream while allowing exact stream-coordinate faults."""

    def __init__(self, stream, harness):
        self._stream = stream
        self._harness = harness

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._harness.stream_exit_count += 1
        self._harness.events.append(("stream_close", exc))
        result = self._stream.__exit__(exc_type, exc, traceback)
        error = self._harness.faults.get("close_temp")
        if error is not None:
            raise error
        return result

    def write(self, data):
        self._harness.events.append(("stream_write", data))
        error = self._harness.faults.get("write_record")
        if error is not None:
            raise error
        return self._stream.write(data)

    def flush(self):
        self._harness.events.append(("stream_flush", None))
        error = self._harness.faults.get("flush_temp")
        if error is not None:
            raise error
        return self._stream.flush()

    def fileno(self):
        self._harness.events.append(("stream_fileno", None))
        return self._stream.fileno()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class AtomicWriteHarness:
    """Narrow real-I/O fault injection for one observed JSONL write."""

    def __init__(self, path: Path, faults: dict[str, Exception], *, parent_absent: bool):
        self.path = path
        self.faults = faults
        self.parent_absent = parent_absent
        self.tmp_path = path.with_name(f"{path.name}.writer.tmp")
        self.foreign_path = path.with_name(f"{path.name}.foreign.tmp")
        self.events: list[tuple] = []
        self.directory_fd: int | None = None
        self.directory_open_paths: list[Path] = []
        self.directory_fsync_fds: list[int] = []
        self.directory_close_fds: list[int] = []
        self.directory_real_close_count = 0
        self.destination_at_parent_open: list[bytes] = []
        self.unlink_calls: list[Path] = []
        self.temp_bytes_before_cleanup: list[bytes | None] = []
        self.stream_exit_count = 0
        self.real_streams = []
        self._stack = ExitStack()
        self._real_open = builtins.open
        self._real_mkdir = Path.mkdir
        self._real_unlink = Path.unlink
        self._real_replace = os.replace
        self._real_os_open = os.open
        self._real_fsync = os.fsync
        self._real_close = os.close

    def _ensure_foreign_temp(self) -> None:
        if not self.foreign_path.exists():
            self.foreign_path.write_bytes(FOREIGN_BYTES)

    def _mkdir(self, candidate: Path, *args, **kwargs):
        result = self._real_mkdir(candidate, *args, **kwargs)
        if candidate == self.path.parent:
            self._ensure_foreign_temp()
            error = self.faults.get("mkdir_parent")
            if error is not None:
                raise error
        return result

    def _open(self, file, *args, **kwargs):
        if _target_path(file) != self.tmp_path:
            return self._real_open(file, *args, **kwargs)
        error = self.faults.get("open_temp")
        if error is not None:
            raise error
        stream = self._real_open(file, *args, **kwargs)
        self.real_streams.append(stream)
        return DelegatingStream(stream, self)

    def _replace(self, source, target):
        source_path = _target_path(source)
        target_path = _target_path(target)
        if source_path != self.tmp_path or target_path != self.path:
            return self._real_replace(source, target)
        self.events.append(("replace", source_path, target_path))
        error = self.faults.get("replace_destination")
        if error is not None:
            raise error
        return self._real_replace(source, target)

    def _os_open(self, path, flags, *args, **kwargs):
        path_object = _target_path(path)
        if path_object != self.path.parent:
            return self._real_os_open(path, flags, *args, **kwargs)
        self.destination_at_parent_open.append(self.path.read_bytes())
        error = self.faults.get("open_destination_parent")
        if error is not None:
            raise error
        fd = self._real_os_open(path, flags, *args, **kwargs)
        self.directory_fd = fd
        self.directory_open_paths.append(path_object)
        self.events.append(("directory_open", fd))
        return fd

    def _fsync(self, fd):
        if self.directory_fd is not None and fd == self.directory_fd:
            self.directory_fsync_fds.append(fd)
            self.events.append(("directory_fsync", fd))
            error = self.faults.get("fsync_destination_parent")
            if error is not None:
                raise error
        elif self.directory_fd is None:
            self.events.append(("stream_fsync", fd))
            error = self.faults.get("fsync_temp")
            if error is not None:
                raise error
        return self._real_fsync(fd)

    def _close(self, fd):
        if self.directory_fd is not None and fd == self.directory_fd:
            self.directory_close_fds.append(fd)
            self.events.append(("directory_close", fd))
            error = self.faults.get("close_destination_parent")
            result = self._real_close(fd)
            self.directory_real_close_count += 1
            if error is not None:
                raise error
            return result
        return self._real_close(fd)

    def _unlink(self, candidate: Path, *args, **kwargs):
        if candidate == self.tmp_path:
            self.unlink_calls.append(candidate)
            self.temp_bytes_before_cleanup.append(
                candidate.read_bytes() if candidate.exists() else None
            )
            error = self.faults.get("cleanup_temp")
            if error is not None:
                raise error
        return self._real_unlink(candidate, *args, **kwargs)

    def __enter__(self):
        if self.parent_absent:
            assert not self.path.parent.exists()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(OLD_BYTES)
            self._ensure_foreign_temp()

        harness = self

        def mkdir(candidate, *args, **kwargs):
            return harness._mkdir(candidate, *args, **kwargs)

        def unlink(candidate, *args, **kwargs):
            return harness._unlink(candidate, *args, **kwargs)

        self._stack.enter_context(
            patch("hopper.lodes.uuid.uuid4", return_value=SimpleNamespace(hex="writer"))
        )
        self._stack.enter_context(patch("builtins.open", self._open))
        self._stack.enter_context(patch.object(Path, "mkdir", mkdir))
        self._stack.enter_context(patch.object(Path, "unlink", unlink))
        self._stack.enter_context(patch("hopper.lodes.os.replace", self._replace))
        self._stack.enter_context(patch("hopper.lodes.os.open", self._os_open))
        self._stack.enter_context(patch("hopper.lodes.os.fsync", self._fsync))
        self._stack.enter_context(patch("hopper.lodes.os.close", self._close))
        return self

    def __exit__(self, *args):
        self._stack.close()

    def assert_foreign_temp_unchanged(self) -> None:
        assert self.foreign_path.read_bytes() == FOREIGN_BYTES


def _path(temp_config: Path, existing: bool) -> Path:
    return (
        temp_config / "existing" / "active.jsonl"
        if existing
        else temp_config / "absent" / "active.jsonl"
    )


def _items(operation: str) -> list[dict]:
    if operation == "serialize_record":
        return [{"id": "first"}, {"bad": object()}]
    return [{"id": "payload"}]


def _assert_fault(fault, operation: str, error: Exception) -> None:
    assert fault.operation == operation
    assert fault.error is error
    assert type(fault.error) is type(error)
    assert fault.error.args == error.args
    assert str(fault.error) == str(error)
    assert fault.errno == getattr(error, "errno", None)


def _assert_fault_vector(observation, expected: list[tuple[str, Exception]]) -> None:
    assert [fault.operation for fault in observation.errors] == [
        operation for operation, _ in expected
    ]
    for fault, (operation, error) in zip(observation.errors, expected, strict=True):
        _assert_fault(fault, operation, error)


def _assert_destination(path: Path, existing: bool, publication, candidate: bytes) -> None:
    if publication is lodes._AtomicJsonlPublication.NOT_PUBLISHED:
        if existing:
            assert path.read_bytes() == OLD_BYTES
        else:
            assert not path.exists()
    else:
        assert path.read_bytes() == candidate


SINGLE_FAULTS = (
    ("mkdir_parent", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ("open_temp", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ("serialize_record", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ("write_record", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ("flush_temp", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ("fsync_temp", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ("close_temp", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ("replace_destination", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ("open_destination_parent", lodes._AtomicJsonlPublication.PUBLISHED_DURABILITY_UNKNOWN),
    ("fsync_destination_parent", lodes._AtomicJsonlPublication.PUBLISHED_DURABILITY_UNKNOWN),
    ("close_destination_parent", lodes._AtomicJsonlPublication.COMMITTED),
)


def test_success_observes_real_publication_chain_and_mid_call_destination(temp_config):
    path = _path(temp_config, existing=False)
    items = [{"name": "café", "b": 1, "a": 2}, {"z": 0}]
    candidate = b'{"name": "caf\\u00e9", "b": 1, "a": 2}\n{"z": 0}\n'

    with AtomicWriteHarness(path, {}, parent_absent=True) as harness:
        observation = lodes._observe_jsonl_atomic_write(path, items)

    assert observation.publication is lodes._AtomicJsonlPublication.COMMITTED
    assert observation.errors == ()
    assert observation.compatibility_exception is None
    assert path.read_bytes() == candidate
    assert harness.destination_at_parent_open == [candidate]
    assert harness.directory_open_paths == [path.parent]
    assert harness.directory_fsync_fds == [harness.directory_fd]
    assert harness.directory_close_fds == [harness.directory_fd]
    assert harness.directory_real_close_count == 1
    assert [event[0] for event in harness.events] == [
        "stream_write",
        "stream_write",
        "stream_flush",
        "stream_fileno",
        "stream_fsync",
        "stream_close",
        "replace",
        "directory_open",
        "directory_fsync",
        "directory_close",
    ]
    assert harness.unlink_calls == []
    assert harness.tmp_path.exists() is False
    assert all(stream.closed for stream in harness.real_streams)
    harness.assert_foreign_temp_unchanged()


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize(("operation", "publication"), SINGLE_FAULTS)
def test_observation_category_matrix_and_single_cleanup(
    temp_config, existing, operation, publication
):
    path = _path(temp_config, existing)
    items = _items(operation)
    expected_error = None if operation == "serialize_record" else OSError(f"{operation} fault")
    faults = {} if expected_error is None else {operation: expected_error}
    candidate = json.dumps(items[0]).encode() + b"\n"

    with AtomicWriteHarness(path, faults, parent_absent=not existing) as harness:
        observation = lodes._observe_jsonl_atomic_write(path, items)

    assert observation.publication is publication
    assert observation.compatibility_exception is observation.errors[0].error
    assert observation.compatibility_exception is not None
    if expected_error is None:
        assert observation.errors[0].operation == "serialize_record"
        assert type(observation.errors[0].error) is TypeError
        assert observation.errors[0].errno is None
    else:
        _assert_fault(observation.errors[0], operation, expected_error)
    _assert_destination(path, existing, publication, candidate)
    assert harness.unlink_calls == [harness.tmp_path]
    assert harness.tmp_path.exists() is False
    harness.assert_foreign_temp_unchanged()


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize(
    ("primary", "publication"),
    [
        ("mkdir_parent", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
        ("open_temp", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
        ("serialize_record", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
        ("write_record", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
        ("flush_temp", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
        ("fsync_temp", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
        ("close_temp", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
        ("replace_destination", lodes._AtomicJsonlPublication.NOT_PUBLISHED),
    ],
)
def test_prepublication_fault_then_cleanup_preserves_compatibility(
    temp_config, existing, primary, publication
):
    path = _path(temp_config, existing)
    primary_error = None if primary == "serialize_record" else OSError(f"{primary} fault")
    cleanup_error = OSError("cleanup fault")
    faults = {"cleanup_temp": cleanup_error}
    if primary_error is not None:
        faults[primary] = primary_error
    items = _items(primary)

    with AtomicWriteHarness(path, faults, parent_absent=not existing) as harness:
        observation = lodes._observe_jsonl_atomic_write(path, items)

    observed_primary = observation.errors[0].error
    if primary_error is not None:
        assert observed_primary is primary_error
    else:
        assert type(observed_primary) is TypeError
    _assert_fault_vector(
        observation,
        [(primary, observed_primary), ("cleanup_temp", cleanup_error)],
    )
    assert observation.publication is publication
    assert observation.compatibility_exception is observed_primary
    assert observation.compatibility_exception.__context__ is None
    assert observation.compatibility_exception.__cause__ is None
    assert observation.compatibility_exception.__suppress_context__ is False
    _assert_destination(path, existing, publication, b'{"id": "payload"}\n')
    assert harness.unlink_calls == [harness.tmp_path]
    assert harness.tmp_path.exists() is (primary not in {"mkdir_parent", "open_temp"})
    harness.assert_foreign_temp_unchanged()


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize(
    ("faults", "labels", "publication", "compatibility_key", "context_key"),
    [
        (
            ("fsync_temp", "close_temp", "cleanup_temp"),
            ("fsync_temp", "close_temp", "cleanup_temp"),
            lodes._AtomicJsonlPublication.NOT_PUBLISHED,
            "close_temp",
            "fsync_temp",
        ),
        (
            ("fsync_destination_parent", "close_destination_parent", "cleanup_temp"),
            ("fsync_destination_parent", "close_destination_parent", "cleanup_temp"),
            lodes._AtomicJsonlPublication.PUBLISHED_DURABILITY_UNKNOWN,
            "close_destination_parent",
            "fsync_destination_parent",
        ),
        (
            ("close_destination_parent", "cleanup_temp"),
            ("close_destination_parent", "cleanup_temp"),
            lodes._AtomicJsonlPublication.COMMITTED,
            "close_destination_parent",
            None,
        ),
    ],
)
def test_observation_combined_fault_order_and_compatibility(
    temp_config, existing, faults, labels, publication, compatibility_key, context_key
):
    path = _path(temp_config, existing)
    errors = {operation: OSError(f"{operation} fault") for operation in faults}

    with AtomicWriteHarness(path, errors, parent_absent=not existing) as harness:
        observation = lodes._observe_jsonl_atomic_write(path, [{"id": "payload"}])

    _assert_fault_vector(observation, [(operation, errors[operation]) for operation in labels])
    compatibility = errors[compatibility_key]
    assert observation.compatibility_exception is compatibility
    assert compatibility.__context__ is (errors[context_key] if context_key else None)
    assert compatibility.__cause__ is None
    assert compatibility.__suppress_context__ is False
    assert observation.publication is publication
    _assert_destination(path, existing, publication, b'{"id": "payload"}\n')
    assert harness.unlink_calls == [harness.tmp_path]
    if "close_destination_parent" in labels:
        assert harness.directory_real_close_count == 1
    assert harness.tmp_path.exists() is (labels[0] == "fsync_temp")
    harness.assert_foreign_temp_unchanged()


@pytest.mark.parametrize("existing", [True, False])
def test_second_record_type_error_closes_real_stream_before_cleanup(temp_config, existing):
    path = _path(temp_config, existing)
    first = {"id": "first"}
    close_error = OSError("close fault")
    cleanup_error = OSError("cleanup fault")
    first_line = json.dumps(first).encode() + b"\n"

    with AtomicWriteHarness(
        path,
        {"close_temp": close_error, "cleanup_temp": cleanup_error},
        parent_absent=not existing,
    ) as harness:
        observation = lodes._observe_jsonl_atomic_write(path, [first, {"bad": object()}])

    type_error = observation.errors[0].error
    assert type(type_error) is TypeError
    _assert_fault_vector(
        observation,
        [
            ("serialize_record", type_error),
            ("close_temp", close_error),
            ("cleanup_temp", cleanup_error),
        ],
    )
    assert observation.publication is lodes._AtomicJsonlPublication.NOT_PUBLISHED
    assert observation.compatibility_exception is close_error
    assert close_error.__context__ is type_error
    assert close_error.__cause__ is None
    assert close_error.__suppress_context__ is False
    assert isinstance(observation.compatibility_exception, OSError)
    assert harness.events[0] == ("stream_write", first_line.decode())
    assert harness.stream_exit_count == 1
    assert all(stream.closed for stream in harness.real_streams)
    assert harness.temp_bytes_before_cleanup == [first_line]
    assert harness.tmp_path.exists()
    _assert_destination(path, existing, observation.publication, first_line)
    harness.assert_foreign_temp_unchanged()


@pytest.mark.parametrize("existing", [True, False])
def test_second_record_type_error_cleanup_removes_real_first_line_temp(temp_config, existing):
    path = _path(temp_config, existing)
    first = {"id": "first"}
    first_line = json.dumps(first).encode() + b"\n"

    with AtomicWriteHarness(path, {}, parent_absent=not existing) as harness:
        observation = lodes._observe_jsonl_atomic_write(path, [first, {"bad": object()}])

    assert observation.errors[0].operation == "serialize_record"
    assert type(observation.compatibility_exception) is TypeError
    assert harness.stream_exit_count == 1
    assert all(stream.closed for stream in harness.real_streams)
    assert harness.temp_bytes_before_cleanup == [first_line]
    assert harness.tmp_path.exists() is False
    _assert_destination(path, existing, observation.publication, first_line)
    harness.assert_foreign_temp_unchanged()


def test_byte_witnesses_include_empty_default_json_and_trailing_newlines(temp_config):
    empty = temp_config / "empty.jsonl"
    populated = temp_config / "populated.jsonl"

    assert lodes._write_jsonl_atomic(empty, []) is None
    assert empty.read_bytes() == b""
    items = [{"name": "café", "b": 1, "a": 2}, {"z": 0}]
    assert lodes._write_jsonl_atomic(populated, items) is None
    assert populated.read_bytes() == b'{"name": "caf\\u00e9", "b": 1, "a": 2}\n{"z": 0}\n'


@pytest.mark.parametrize("mode", ["clean", "prepublication", "postpublication"])
def test_observation_never_mutates_caller_items(temp_config, mode):
    path = temp_config / f"{mode}.jsonl"
    items = [{"id": "payload", "nested": {"letters": ["a", "b"]}}]
    before = copy.deepcopy(items)
    list_id = id(items)
    record_ids = [id(record) for record in items]
    if mode == "clean":
        faults = {}
    elif mode == "prepublication":
        faults = {
            "replace_destination": OSError("replace fault"),
            "cleanup_temp": OSError("cleanup fault"),
        }
    else:
        faults = {"open_destination_parent": OSError("directory open fault")}

    with AtomicWriteHarness(path, faults, parent_absent=False):
        lodes._observe_jsonl_atomic_write(path, items)

    assert id(items) == list_id
    assert [id(record) for record in items] == record_ids
    assert items == before


@pytest.mark.parametrize(
    ("saver", "filename"),
    [(lodes.save_lodes, "active.jsonl"), (lodes.save_archived_lodes, "archived.jsonl")],
)
def test_write_jsonl_atomic_and_public_savers_return_none(temp_config, saver, filename):
    direct = temp_config / "direct.jsonl"
    assert lodes._write_jsonl_atomic(direct, [{"id": "direct"}]) is None
    assert saver([{"id": "saved"}]) is None
    assert (temp_config / filename).read_bytes() == b'{"id": "saved"}\n'


@pytest.mark.parametrize(
    ("saver", "filename"),
    [(lodes.save_lodes, "active.jsonl"), (lodes.save_archived_lodes, "archived.jsonl")],
)
@pytest.mark.parametrize("operation", [operation for operation, _ in SINGLE_FAULTS])
def test_public_savers_reraise_exact_single_coordinate_exception(
    temp_config, saver, filename, operation
):
    path = temp_config / filename
    expected_error = None if operation == "serialize_record" else OSError(f"{operation} fault")
    faults = {} if expected_error is None else {operation: expected_error}

    with AtomicWriteHarness(path, faults, parent_absent=False):
        with pytest.raises(Exception) as exc_info:
            saver(_items(operation))

    if expected_error is None:
        assert type(exc_info.value) is TypeError
    else:
        assert exc_info.value is expected_error
        assert isinstance(exc_info.value, OSError)


@pytest.mark.parametrize(
    ("saver", "filename"),
    [(lodes.save_lodes, "active.jsonl"), (lodes.save_archived_lodes, "archived.jsonl")],
)
@pytest.mark.parametrize(
    ("case", "fault_names"),
    [
        ("file_fsync", ("fsync_temp", "close_temp", "cleanup_temp")),
        ("second_record", ("close_temp", "cleanup_temp")),
        (
            "directory_fsync",
            ("fsync_destination_parent", "close_destination_parent", "cleanup_temp"),
        ),
        ("directory_close", ("close_destination_parent", "cleanup_temp")),
    ],
)
def test_public_savers_preserve_combined_outward_close_exception(
    temp_config, saver, filename, case, fault_names
):
    path = temp_config / filename
    errors = {name: OSError(f"{name} fault") for name in fault_names}
    close_name = (
        "close_temp" if case in {"file_fsync", "second_record"} else "close_destination_parent"
    )
    close_error = errors[close_name]
    context_error = None
    if case == "file_fsync":
        context_error = errors["fsync_temp"]
        items = [{"id": "payload"}]
    elif case == "second_record":
        items = [{"id": "first"}, {"bad": object()}]
    elif case == "directory_fsync":
        context_error = errors["fsync_destination_parent"]
        items = [{"id": "payload"}]
    else:
        items = [{"id": "payload"}]

    with AtomicWriteHarness(path, errors, parent_absent=False) as harness:
        with pytest.raises(OSError) as exc_info:
            saver(items)

    assert exc_info.value is close_error
    if case == "second_record":
        assert type(close_error.__context__) is TypeError
    else:
        assert close_error.__context__ is context_error
    assert close_error.__cause__ is None
    assert close_error.__suppress_context__ is False
    assert harness.unlink_calls == [harness.tmp_path]
    harness.assert_foreign_temp_unchanged()


@pytest.mark.parametrize(
    ("saver", "filename"),
    [(lodes.save_lodes, "active.jsonl"), (lodes.save_archived_lodes, "archived.jsonl")],
)
def test_public_saver_preserves_writer_chain_inside_active_caller_exception(
    temp_config, saver, filename
):
    path = temp_config / filename
    close_error = OSError(errno.EIO, "close fault")
    cleanup_error = OSError(errno.ENOSPC, "cleanup fault")
    caller_error = ValueError("active caller fault")

    with AtomicWriteHarness(
        path,
        {"close_temp": close_error, "cleanup_temp": cleanup_error},
        parent_absent=False,
    ):
        with pytest.raises(OSError) as exc_info:
            try:
                raise caller_error
            except ValueError:
                saver([{"id": "first"}, {"bad": object()}])

    type_error = close_error.__context__
    assert exc_info.value is close_error
    assert type(type_error) is TypeError
    assert type_error.__context__ is caller_error
    assert close_error.__cause__ is None
    assert close_error.__suppress_context__ is False


def test_observation_retains_non_null_errno_negative_oracle(temp_config):
    path = temp_config / "active.jsonl"
    sync_error = OSError(errno.ENOSPC, "temp sync fault")

    with AtomicWriteHarness(
        path,
        {"fsync_temp": sync_error},
        parent_absent=False,
    ):
        observation = lodes._observe_jsonl_atomic_write(path, [{"id": "payload"}])

    _assert_fault(observation.errors[0], "fsync_temp", sync_error)
    assert observation.errors[0].errno == errno.ENOSPC

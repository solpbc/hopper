# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for ownership capture and deterministic containment."""

import errno
import os
import select
import signal
import subprocess
from unittest.mock import MagicMock

import pytest

from hopper import oom, teardown, tmux


def _linux_stat(pid=100, ppid=10, pgid=100, starttime=12345, command="worker (inner)"):
    fields = ["S", str(ppid), str(pgid), *("0" for _ in range(16)), str(starttime)]
    return f"{pid} ({command}) {' '.join(fields)}\n"


def _linux_process(pid: int, ppid: int = 1, pgid: int | None = None, start=1) -> dict:
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pid if pgid is None else pgid,
        "birth": {
            "kind": "linux-proc-starttime",
            "boot_id": "boot-one",
            "value": str(start),
        },
    }


def _ps_process(pid: int, ppid: int = 1, pgid: int | None = None, start="Mon Aug 10 22:29:26 2026"):
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pid if pgid is None else pgid,
        "birth": {"kind": "ps-lstart", "boot_id": None, "value": start},
    }


def _containment_record(mode: str, *, state="not_started") -> dict:
    return {
        "ownership": {"proof_mode": mode},
        "containment": {
            "state": state,
            "started_monotonic_ns": None,
            "deadline_monotonic_ns": None,
            "poll_interval_ms": 50,
            "last_cgroup_observation": None,
            "last_supervisor_observation": None,
            "last_owned_process_count": None,
            "result": None,
            "proof_label": None,
            "last_error": None,
        },
    }


def _fake_clock(start=1_000_000_000):
    clock = {"now": start, "polls": []}

    def now_ns():
        return clock["now"]

    def poll(seconds):
        clock["polls"].append(seconds)
        clock["now"] += int(seconds * 1_000_000_000)

    return clock, now_ns, poll


def _pidfd_interface(*, open_call=None, signal_call=None, source="test"):
    return {
        "source": source,
        "open": open_call or (lambda _pid, _flags: 17),
        "send_signal": signal_call or (lambda _fd, _sig, _info, _flags: None),
    }


def test_pidfd_resolution_prefers_complete_stdlib_interface(monkeypatch):
    calls = []

    def stdlib_open(pid, flags):
        calls.append(("open", pid, flags))
        return 23

    def stdlib_signal(fd, sig, info, flags):
        calls.append(("signal", fd, sig, info, flags))

    monkeypatch.setattr(teardown.os, "pidfd_open", stdlib_open, raising=False)
    monkeypatch.setattr(teardown.signal, "pidfd_send_signal", stdlib_signal, raising=False)
    monkeypatch.setattr(
        teardown.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: pytest.fail("libc must not load when stdlib is complete"),
    )

    interface = teardown.resolve_pidfd_interface()
    assert interface is not None
    assert interface["source"] == "stdlib"
    assert interface["open"](101, 0) == 23
    interface["send_signal"](23, signal.SIGKILL, None, 0)
    assert calls == [("open", 101, 0), ("signal", 23, signal.SIGKILL, None, 0)]


def test_pidfd_resolution_uses_typed_errno_aware_libc_adapter(monkeypatch):
    libc = MagicMock()
    libc.pidfd_open = MagicMock(return_value=29)
    libc.pidfd_send_signal = MagicMock(return_value=0)
    loads = []
    monkeypatch.delattr(teardown.os, "pidfd_open", raising=False)
    monkeypatch.delattr(teardown.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        teardown.ctypes,
        "CDLL",
        lambda name, *, use_errno: loads.append((name, use_errno)) or libc,
    )

    interface = teardown.resolve_pidfd_interface()
    assert interface is not None
    assert interface["source"] == "libc"
    assert loads == [(None, True)]
    assert libc.pidfd_open.argtypes == [teardown.ctypes.c_int, teardown.ctypes.c_uint]
    assert libc.pidfd_open.restype is teardown.ctypes.c_int
    assert libc.pidfd_send_signal.argtypes == [
        teardown.ctypes.c_int,
        teardown.ctypes.c_int,
        teardown.ctypes.c_void_p,
        teardown.ctypes.c_uint,
    ]
    assert libc.pidfd_send_signal.restype is teardown.ctypes.c_int
    assert interface["open"](101, 0) == 29
    interface["send_signal"](29, signal.SIGKILL, None, 0)
    libc.pidfd_open.assert_called_once_with(101, 0)
    libc.pidfd_send_signal.assert_called_once_with(29, signal.SIGKILL, None, 0)

    libc.pidfd_open.return_value = -1
    monkeypatch.setattr(teardown.ctypes, "get_errno", lambda: errno.ESRCH)
    with pytest.raises(ProcessLookupError) as raised:
        interface["open"](999999, 0)
    assert raised.value.errno == errno.ESRCH


def test_pidfd_resolution_fails_closed_when_stdlib_and_libc_are_unavailable(monkeypatch):
    monkeypatch.delattr(teardown.os, "pidfd_open", raising=False)
    monkeypatch.delattr(teardown.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(teardown.ctypes, "CDLL", MagicMock(side_effect=OSError("no libc")))

    assert teardown.resolve_pidfd_interface() is None


def test_parse_linux_process_stat_uses_field_22_and_survives_parentheses():
    assert teardown.parse_linux_process_stat(_linux_stat(), "boot-one") == _linux_process(
        100, 10, 100, 12345
    )


@pytest.mark.parametrize("text", ["", "100 (x) S 1", "bad (x) " + " ".join(["0"] * 20)])
def test_parse_linux_process_stat_rejects_ambiguous_rows(text):
    with pytest.raises(ValueError):
        teardown.parse_linux_process_stat(text, "boot-one")


def test_parse_ps_process_row_keeps_lstart_opaque():
    row = " 3898898 3399955 3898898  Mon Aug 10 22:29:26 2026\n"
    assert teardown.parse_ps_process_row(row) == _ps_process(3898898, 3399955, 3898898)


def test_non_linux_host_boot_identity_uses_opaque_pid_one_start():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="1 0 1 Mon Aug 10 22:29:26 2026\n",
            stderr="",
        )

    assert teardown.read_host_boot_identity(platform="darwin", run=run) == (
        "ps-pid1:Mon Aug 10 22:29:26 2026"
    )
    assert calls[0][0] == ["ps", "-o", "pid=,ppid=,pgid=,lstart=", "-p", "1"]


def test_non_linux_host_boot_identity_fails_closed_on_ambiguous_ps():
    result = subprocess.CompletedProcess(["ps"], 0, stdout="", stderr="")
    assert teardown.read_host_boot_identity(platform="darwin", run=lambda *_a, **_k: result) is None


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "10 1 Mon Aug 10 22:29:26 2026\n",
        "10 1 10 Mon Aug 10 22:29:26 2026\n11 1 11 Tue Aug 11 22:29:26 2026\n",
    ],
)
def test_parse_ps_process_row_rejects_missing_or_ambiguous_output(stdout):
    with pytest.raises(ValueError):
        teardown.parse_ps_process_row(stdout)


def test_non_linux_identity_uses_mockable_ps(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="3898898 3399955 3898898 Mon Aug 10 22:29:26 2026\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    observed = teardown.read_process_identity(3898898, platform="darwin")

    assert observed == {
        "state": "alive",
        "identity": _ps_process(3898898, 3399955, 3898898),
        "error": None,
    }
    assert calls == [
        (
            ["ps", "-o", "pid=,ppid=,pgid=,lstart=", "-p", "3898898"],
            {"capture_output": True, "text": True, "timeout": 1.0},
        )
    ]


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, stdout="", stderr="not found"),
        subprocess.CompletedProcess([], 0, stdout="garbled", stderr=""),
    ],
)
def test_non_linux_ps_failure_is_unknown_never_gone(monkeypatch, result):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    assert teardown.read_process_identity(99, platform="darwin")["state"] == "cannot-tell"


def test_read_linux_process_identity_distinguishes_absence_and_parse_failure(monkeypatch):
    def missing(_path):
        raise FileNotFoundError

    monkeypatch.setattr(oom, "_read_text", missing)
    assert teardown.read_linux_process_identity(10, boot_id="boot-one")["state"] == "gone"

    monkeypatch.setattr(oom, "_read_text", lambda _path: "malformed")
    assert teardown.read_linux_process_identity(10, boot_id="boot-one")["state"] == "cannot-tell"


def test_capture_descendants_is_transitive_and_excludes_seed_roots():
    roots = [_linux_process(10), _linux_process(11, 10)]
    table = [
        *roots,
        _linux_process(12, 11),
        _linux_process(13, 12),
        _linux_process(20, 1),
    ]
    assert [item["pid"] for item in teardown.capture_descendants(roots, table)] == [12, 13]


def test_capture_scope_cgroup_uses_control_group_and_records_inode(tmp_path, monkeypatch):
    cgroup = tmp_path / "user.slice/example.scope"
    cgroup.mkdir(parents=True)
    monkeypatch.setattr(
        oom,
        "read_scope_control_group",
        lambda *_args: {
            "state": "present",
            "load_state": "loaded",
            "control_group": "/user.slice/example.scope",
            "error": None,
        },
    )

    captured = teardown.capture_scope_cgroup(
        "/bin/systemctl",
        "example.scope",
        cgroup_root=tmp_path,
        boot_id="boot-one",
    )

    assert captured["state"] == "captured"
    assert captured["unit"]["control_group"] == "/user.slice/example.scope"
    assert captured["cgroup"] == {
        "relative_path": "/user.slice/example.scope",
        "absolute_path": str(cgroup),
        "identity": {"st_dev": cgroup.stat().st_dev, "st_ino": cgroup.stat().st_ino},
        "boot_id": "boot-one",
    }


def test_capture_scope_cgroup_rejects_root_or_unknown_control_group(tmp_path, monkeypatch):
    monkeypatch.setattr(
        oom,
        "read_scope_control_group",
        lambda *_args: {
            "state": "present",
            "load_state": "loaded",
            "control_group": "/",
            "error": None,
        },
    )
    assert (
        teardown.capture_scope_cgroup(
            "systemctl", "bad.scope", cgroup_root=tmp_path, boot_id="boot-one"
        )["state"]
        == "cannot-tell"
    )


def test_capture_ownership_collects_all_linux_facts_before_close(monkeypatch):
    pane_root = _linux_process(100, 1, 100, 1)
    supervisor = _linux_process(101, 100, 100, 2)
    worker = _linux_process(102, 101, 102, 3)
    child = _linux_process(103, 102, 102, 4)
    identities = {item["pid"]: item for item in (pane_root, supervisor, worker, child)}
    monkeypatch.setattr(
        tmux,
        "pane_identity",
        lambda _pane: {"pane_id": "%1", "window_id": "@1", "pane_pid": 100},
    )
    monkeypatch.setattr(teardown, "read_boot_id", lambda **_kwargs: "boot-one")
    monkeypatch.setattr(
        teardown,
        "read_process_identity",
        lambda pid, **_kwargs: {"state": "alive", "identity": identities[pid], "error": None},
    )
    monkeypatch.setattr(
        teardown,
        "read_process_table",
        lambda **_kwargs: {
            "state": "complete",
            "identities": list(identities.values()),
            "error": None,
        },
    )
    monkeypatch.setattr(
        teardown,
        "capture_scope_cgroup",
        lambda *_args, **_kwargs: {
            "state": "captured",
            "unit": {
                "name": "hopper.scope",
                "load_state": "loaded",
                "control_group": "/user.slice/hopper.scope",
            },
            "cgroup": {
                "relative_path": "/user.slice/hopper.scope",
                "absolute_path": "/sys/fs/cgroup/user.slice/hopper.scope",
                "identity": {"st_dev": 1, "st_ino": 2},
                "boot_id": "boot-one",
            },
            "error": None,
        },
    )

    captured = teardown.capture_ownership(
        pane_id="%1",
        supervisor_pid=101,
        worker_pid=102,
        process_group=100,
        unit_name="hopper.scope",
        systemctl="systemctl",
        platform="linux",
    )

    assert captured["state"] == "captured"
    ownership = captured["ownership"]
    assert ownership["proof_mode"] == "linux-strict"
    assert ownership["pane"] == {
        "pane_id": "%1",
        "window_id": "@1",
        "root_process": pane_root,
    }
    assert ownership["supervisor"] == supervisor
    assert ownership["worker"] == worker
    assert ownership["process_group"] == 100
    assert ownership["descendants"] == [child]
    assert ownership["unit"]["name"] == "hopper.scope"
    assert ownership["cgroup"]["identity"] == {"st_dev": 1, "st_ino": 2}


def test_capture_ownership_blocks_before_close_on_supervisor_pgid_mismatch(monkeypatch):
    monkeypatch.setattr(
        tmux,
        "pane_identity",
        lambda _pane: {"pane_id": "%1", "window_id": "@1", "pane_pid": 100},
    )
    monkeypatch.setattr(teardown, "read_boot_id", lambda **_kwargs: "boot-one")
    identities = {
        100: _linux_process(100, 1, 100),
        101: _linux_process(101, 100, 999),
        102: _linux_process(102, 101, 102),
    }
    monkeypatch.setattr(
        teardown,
        "read_process_identity",
        lambda pid, **_kwargs: {"state": "alive", "identity": identities[pid], "error": None},
    )

    captured = teardown.capture_ownership(
        pane_id="%1",
        supervisor_pid=101,
        worker_pid=102,
        process_group=100,
        platform="linux",
    )
    assert captured == {
        "state": "cannot-tell",
        "ownership": None,
        "error": "supervisor PGID mismatch",
    }


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ("populated 0\nfrozen 0\n", "empty"),
        ("populated 1\nfrozen 0\n", "populated"),
        ("frozen 0\n", "cannot-tell"),
        ("populated 0\npopulated 1\n", "cannot-tell"),
        ("populated 2\n", "cannot-tell"),
    ],
)
def test_observe_cgroup_uses_recursive_populated_fact(tmp_path, monkeypatch, events, expected):
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    identity = {"st_dev": cgroup.stat().st_dev, "st_ino": cgroup.stat().st_ino}
    record = {
        "relative_path": "/user.slice/test.scope",
        "absolute_path": str(cgroup),
        "identity": identity,
        "boot_id": "boot-one",
    }
    monkeypatch.setattr(
        oom, "_read_text", lambda path: events if path.name == "cgroup.events" else ""
    )

    assert (
        teardown.observe_cgroup(
            record,
            {"state": "present", "control_group": "/user.slice/test.scope"},
            boot_id="boot-one",
        )
        == expected
    )


def test_cgroup_absence_requires_explicit_unit_absence(tmp_path):
    record = {
        "relative_path": "/user.slice/gone.scope",
        "absolute_path": str(tmp_path / "gone.scope"),
        "identity": {"st_dev": 1, "st_ino": 2},
        "boot_id": "old-boot",
    }
    assert teardown.observe_cgroup(record, {"state": "absent"}) == "empty"
    assert teardown.observe_cgroup(record, {"state": "present"}) == "cannot-tell"
    assert teardown.observe_cgroup(record, {"state": "cannot-tell"}) == "cannot-tell"


def test_cgroup_identity_mismatch_is_unknown(tmp_path):
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    record = {
        "relative_path": "/user.slice/test.scope",
        "absolute_path": str(cgroup),
        "identity": {"st_dev": cgroup.stat().st_dev, "st_ino": cgroup.stat().st_ino + 1},
        "boot_id": "boot-one",
    }
    assert (
        teardown.observe_cgroup(
            record,
            {"state": "present", "control_group": "/user.slice/test.scope"},
            boot_id="boot-one",
        )
        == "cannot-tell"
    )


def test_cgroup_unit_path_change_is_unknown_before_read(tmp_path, monkeypatch):
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    record = {
        "relative_path": "/user.slice/original.scope",
        "absolute_path": str(cgroup),
        "identity": {"st_dev": cgroup.stat().st_dev, "st_ino": cgroup.stat().st_ino},
        "boot_id": "boot-one",
    }
    read = MagicMock(side_effect=AssertionError("mismatched unit must block before cgroup read"))
    monkeypatch.setattr(oom, "_read_text", read)

    assert (
        teardown.observe_cgroup(
            record,
            {"state": "present", "control_group": "/user.slice/reused.scope"},
            boot_id="boot-one",
        )
        == "cannot-tell"
    )
    read.assert_not_called()


def test_kill_cgroup_writes_only_through_verified_directory(tmp_path, monkeypatch):
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    record = {
        "absolute_path": str(cgroup),
        "identity": {"st_dev": cgroup.stat().st_dev, "st_ino": cgroup.stat().st_ino},
        "boot_id": "boot-one",
    }
    writes = []
    monkeypatch.setattr(oom, "_write_text", lambda path, value: writes.append((path.name, value)))

    assert teardown.kill_cgroup(record, boot_id="boot-one") is True
    assert writes == [("cgroup.kill", "1")]

    record["identity"]["st_ino"] += 1
    assert teardown.kill_cgroup(record, boot_id="boot-one") is False
    assert writes == [("cgroup.kill", "1")]


def test_pidfd_is_opened_before_birth_verification(monkeypatch):
    read_fd, write_fd = os.pipe()
    events = []

    def fake_open(pid, flags):
        events.append(("open", pid, flags))
        return read_fd

    def fake_read(path):
        events.append(("read", path.name))
        if path.name == "boot_id":
            return "boot-one\n"
        return _linux_stat(pid=100, ppid=1, pgid=100, starttime=9)

    monkeypatch.setattr(oom, "_read_text", fake_read)
    supervisor = _linux_process(100, 1, 100, 9)
    observed = teardown.reopen_supervisor_pidfd(
        supervisor, pidfd_interface=_pidfd_interface(open_call=fake_open)
    )
    try:
        assert observed == {"state": "alive", "fd": read_fd, "error": None}
        assert events == [("open", 100, 0), ("read", "boot_id"), ("read", "stat")]
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_pidfd_birth_mismatch_is_gone_and_never_a_signal_target(monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        oom,
        "_read_text",
        lambda path: (
            "boot-one\n"
            if path.name == "boot_id"
            else _linux_stat(pid=100, ppid=1, pgid=100, starttime=10)
        ),
    )
    observed = teardown.reopen_supervisor_pidfd(
        _linux_process(100, 1, 100, 9),
        pidfd_interface=_pidfd_interface(open_call=lambda _pid, _flags: read_fd),
    )
    try:
        assert observed == {"state": "gone", "fd": None, "error": None}
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


def test_observe_pidfd_uses_poll_and_signal_zero(monkeypatch):
    registrations = []
    signals = []

    class FakePoll:
        def register(self, fd, flags):
            registrations.append((fd, flags))

        def poll(self, timeout):
            assert timeout == 0
            return []

    monkeypatch.setattr(teardown.select, "poll", FakePoll)
    interface = _pidfd_interface(
        signal_call=lambda fd, sig, info, flags: signals.append((fd, sig, info, flags))
    )

    assert teardown.observe_pidfd(17, pidfd_interface=interface) == "alive"
    assert registrations == [(17, select.POLLIN | select.POLLHUP | select.POLLERR)]
    assert signals == [(17, 0, None, 0)]


def test_observe_pidfd_invalid_descriptor_is_unknown(monkeypatch):
    class FakePoll:
        def register(self, _fd, _flags):
            return None

        def poll(self, _timeout):
            return [(17, select.POLLNVAL)]

    monkeypatch.setattr(teardown.select, "poll", FakePoll)
    interface = _pidfd_interface(
        signal_call=lambda *_args: pytest.fail("invalid pidfd must not be probed or signaled")
    )
    assert teardown.observe_pidfd(17, pidfd_interface=interface) == "cannot-tell"


@pytest.mark.parametrize(
    ("error", "expected"),
    [(None, True), (ProcessLookupError(), True), (PermissionError(), False)],
)
def test_kill_supervisor_pidfd_never_uses_a_numeric_pid(monkeypatch, error, expected):
    calls = []

    def fake_signal(fd, sig, info, flags):
        calls.append((fd, sig, info, flags))
        if error is not None:
            raise error

    interface = _pidfd_interface(signal_call=fake_signal)
    assert teardown.kill_supervisor_pidfd(17, pidfd_interface=interface) is expected
    assert calls == [(17, signal.SIGKILL, None, 0)]


def test_close_owned_pane_checks_every_identity_before_exact_kill():
    root = _linux_process(100, 1, 100, 9)
    ownership = {
        "platform": "linux",
        "pane": {"pane_id": "%1", "window_id": "@1", "root_process": root},
    }
    calls = []
    result = teardown.close_owned_pane(
        ownership,
        pane_reader=lambda _pane: {"pane_id": "%1", "window_id": "@1", "pane_pid": 100},
        pane_probe=lambda _pane: tmux.Liveness.GONE,
        process_reader=lambda _pid, **_kwargs: {
            "state": "alive",
            "identity": root,
            "error": None,
        },
        kill=lambda pane: calls.append(pane) or True,
    )

    assert result == {"state": "gone", "error": None}
    assert calls == ["%1"]


def test_close_owned_pane_mismatch_never_kills():
    ownership = {
        "platform": "linux",
        "pane": {
            "pane_id": "%1",
            "window_id": "@1",
            "root_process": _linux_process(100),
        },
    }
    calls = []
    result = teardown.close_owned_pane(
        ownership,
        pane_reader=lambda _pane: {"pane_id": "%1", "window_id": "@2", "pane_pid": 100},
        kill=lambda pane: calls.append(pane) or True,
    )
    assert result["state"] == "cannot-tell"
    assert calls == []


@pytest.mark.parametrize(
    ("pane_state", "process_state", "current", "expected"),
    [
        (tmux.Liveness.GONE, "gone", None, "gone"),
        (tmux.Liveness.GONE, "alive", _linux_process(100), "alive"),
        (tmux.Liveness.ALIVE, "gone", None, "alive"),
        (tmux.Liveness.UNKNOWN, "gone", None, "cannot-tell"),
        (tmux.Liveness.GONE, "cannot-tell", None, "cannot-tell"),
    ],
)
def test_pane_root_absence_requires_both_proofs(pane_state, process_state, current, expected):
    ownership = {
        "platform": "linux",
        "pane": {
            "pane_id": "%1",
            "window_id": "@1",
            "root_process": _linux_process(100),
        },
    }
    assert (
        teardown.observe_pane_root_absence(
            ownership,
            pane_probe=lambda _pane: pane_state,
            process_reader=lambda _pid, **_kwargs: {
                "state": process_state,
                "identity": current,
                "error": None,
            },
        )
        == expected
    )


def test_bounded_observation_retains_reparented_process_and_adds_child():
    owned = [_ps_process(10, 1, 10), _ps_process(11, 10, 10)]
    table = {
        "state": "complete",
        "error": None,
        "identities": [
            _ps_process(11, 1, 99),
            _ps_process(12, 11, 99),
            _ps_process(10, 1, 10, start="Tue Aug 11 22:29:26 2026"),
        ],
    }
    observed = teardown.observe_bounded_processes(owned, platform="darwin", process_table=table)
    assert observed["state"] == "populated"
    assert [item["pid"] for item in observed["identities"]] == [11, 12]


def test_bounded_observation_never_trusts_unavailable_birth():
    identity = _ps_process(10)
    identity["birth"] = {"kind": "unavailable", "boot_id": None, "value": None}
    observed = teardown.observe_bounded_processes(
        [identity],
        platform="other",
        process_table={"state": "complete", "identities": [], "error": None},
    )
    assert observed["state"] == "cannot-tell"


def test_strict_containment_proves_clean_exit_without_kill():
    _clock, now_ns, poll = _fake_clock()
    kills = []
    result = teardown.observe_containment(
        _containment_record("linux-strict"),
        {
            "observe_cgroup": lambda: "empty",
            "observe_supervisor": lambda: "gone",
            "observe_pane_root": lambda: "gone",
            "kill_cgroup": lambda: kills.append("cgroup") or True,
            "kill_supervisor": lambda: kills.append("supervisor") or True,
        },
        now_ns=now_ns,
        poll=poll,
    )
    assert result["state"] == "proven"
    assert result["result"] == "linux-strict-empty"
    assert kills == []


def test_containment_deadline_is_materialized_before_worker_polling():
    _clock, now_ns, _poll = _fake_clock(start=5_000_000_000)
    record = _containment_record("linux-strict", state="pane_close_pending")

    started = teardown.start_containment(record, now_ns=now_ns)

    assert started["state"] == "grace"
    assert started["started_monotonic_ns"] == 5_000_000_000
    assert started["deadline_monotonic_ns"] == 35_000_000_000
    assert record["containment"]["deadline_monotonic_ns"] is None


def test_strict_containment_returns_kill_intent_then_verifies_without_real_wait():
    clock, now_ns, poll = _fake_clock()
    observations = {"cgroup": "populated", "supervisor": "alive"}
    record = _containment_record("linux-strict")

    pending_kill = teardown.observe_containment(
        record,
        {
            "observe_cgroup": lambda: observations["cgroup"],
            "observe_supervisor": lambda: observations["supervisor"],
            "observe_pane_root": lambda: "gone",
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert pending_kill["state"] == "kill_pending"
    assert clock["now"] == 30_950_000_000
    record["containment"] = pending_kill
    kills = []

    def kill_cgroup():
        kills.append("cgroup")
        observations["cgroup"] = "empty"
        return True

    def kill_supervisor():
        kills.append("supervisor")
        observations["supervisor"] = "gone"
        return True

    proven = teardown.observe_containment(
        record,
        {
            "observe_cgroup": lambda: observations["cgroup"],
            "observe_supervisor": lambda: observations["supervisor"],
            "observe_pane_root": lambda: "gone",
            "kill_cgroup": kill_cgroup,
            "kill_supervisor": kill_supervisor,
        },
        now_ns=now_ns,
        poll=poll,
    )
    assert proven["state"] == "proven"
    assert proven["result"] == "linux-strict-killed-empty"
    assert kills == ["cgroup", "supervisor"]


def test_strict_ambiguity_blocks_without_killing():
    _clock, now_ns, poll = _fake_clock()
    kills = []
    result = teardown.observe_containment(
        _containment_record("linux-strict"),
        {
            "observe_cgroup": lambda: "cannot-tell",
            "observe_supervisor": lambda: "alive",
            "observe_pane_root": lambda: "gone",
            "kill_cgroup": lambda: kills.append("cgroup") or True,
            "kill_supervisor": lambda: kills.append("supervisor") or True,
        },
        now_ns=now_ns,
        poll=poll,
    )
    assert result["state"] == "blocked"
    assert kills == []


def test_darwin_bounded_empty_proves_with_degraded_label():
    _clock, now_ns, poll = _fake_clock()
    result = teardown.observe_containment(
        _containment_record("darwin-bounded"),
        {
            "observe_bounded": lambda: {"state": "empty", "count": 0},
            "observe_pane": lambda: "gone",
        },
        now_ns=now_ns,
        poll=poll,
    )
    assert result["state"] == "proven"
    assert result["result"] == "darwin-bounded-empty"
    assert "unproven" in result["proof_label"]


def test_degraded_survivor_blocks_at_deadline_without_signal_or_real_wait():
    clock, now_ns, poll = _fake_clock()
    result = teardown.observe_containment(
        _containment_record("other-bounded-no-birth"),
        {
            "observe_bounded": lambda: {"state": "populated", "count": 1},
            "observe_pane": lambda: "gone",
        },
        now_ns=now_ns,
        poll=poll,
    )
    assert result["state"] == "blocked"
    assert result["last_owned_process_count"] == 1
    assert clock["now"] == 31_000_000_000
    assert len(clock["polls"]) == 600

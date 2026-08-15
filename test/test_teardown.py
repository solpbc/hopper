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
        "action_type": "completion",
        "boot_id": "boot-one",
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


def _expired_grace(mode: str, *, action_type: str = "completion", start=1_000_000_000) -> dict:
    """A record whose grace budget has already run out.

    Escalation is reached the way production reaches it — the waiting budget
    expired — rather than by a per-action-type bypass. Grace is not a sleep: it
    polls until the cgroup empties, so a test that wants escalation must expire
    the budget, not opt out of it.
    """
    record = _containment_record(mode, state="grace")
    record["action_type"] = action_type
    record["containment"]["started_monotonic_ns"] = start - 1
    record["containment"]["deadline_monotonic_ns"] = start - 1
    return record


def _observe(record: dict, handles: dict, *, now_ns, poll, host_boot_identity="boot-one"):
    return teardown.observe_containment(
        record,
        handles,
        host_boot_identity=host_boot_identity,
        now_ns=now_ns,
        poll=poll,
    )


def _force(state: str, error: str | None = None) -> dict:
    return {"state": state, "error": error}


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


def test_parse_linux_process_stat_accepts_observed_kernel_thread_pgid_zero():
    assert teardown.parse_linux_process_stat(
        _linux_stat(pid=2, ppid=0, pgid=0, command="kthreadd"), "boot-one"
    ) == _linux_process(2, 0, 0, 12345)


def test_linux_process_table_accepts_kernel_thread_pgid_zero(tmp_path):
    (tmp_path / "2").mkdir()
    (tmp_path / "2" / "stat").write_text(_linux_stat(pid=2, ppid=0, pgid=0, command="kthreadd"))
    (tmp_path / "101").mkdir()
    (tmp_path / "101" / "stat").write_text(
        _linux_stat(pid=101, ppid=1, pgid=101, command="hop process")
    )

    observed = teardown.read_process_table(platform="linux", proc_root=tmp_path, boot_id="boot-one")

    assert observed == {
        "state": "complete",
        "identities": [
            _linux_process(2, 0, 0, 12345),
            _linux_process(101, 1, 101, 12345),
        ],
        "error": None,
    }


def test_linux_process_table_zero_rows_is_unknown(tmp_path):
    assert teardown.read_process_table(
        platform="linux", proc_root=tmp_path, boot_id="boot-one"
    ) == {
        "state": "unknown",
        "identities": [],
        "error": "process table returned no rows",
    }


def test_linux_process_table_failed_read_is_unknown(tmp_path):
    observed = teardown.read_process_table(
        platform="linux", proc_root=tmp_path / "missing", boot_id="boot-one"
    )

    assert observed["state"] == "unknown"
    assert observed["identities"] == []
    assert observed["error"]


def test_linux_process_table_retains_parsed_rows_when_one_row_is_ambiguous(tmp_path):
    (tmp_path / "10").mkdir()
    (tmp_path / "10" / "stat").write_text(_linux_stat(pid=10, ppid=1, pgid=10))
    (tmp_path / "11").mkdir()
    (tmp_path / "11" / "stat").write_text("malformed")

    observed = teardown.read_process_table(platform="linux", proc_root=tmp_path, boot_id="boot-one")

    assert observed["state"] == "partial"
    assert observed["identities"] == [_linux_process(10, 1, 10, 12345)]
    assert observed["error"] == "PID 11: malformed /proc stat row"


def test_linux_process_table_with_only_a_rejected_row_is_partial(tmp_path):
    (tmp_path / "11").mkdir()
    (tmp_path / "11" / "stat").write_text("malformed")

    observed = teardown.read_process_table(platform="linux", proc_root=tmp_path, boot_id="boot-one")

    assert observed["state"] == "partial"
    assert observed["identities"] == []


@pytest.mark.parametrize(
    ("stdout", "state", "pids"),
    [
        ("", "unknown", []),
        (
            "10 1 10 Mon Aug 10 22:29:26 2026\nmalformed\n",
            "partial",
            [10],
        ),
        ("malformed\n", "partial", []),
    ],
)
def test_ps_process_table_distinguishes_unknown_and_partial(stdout, state, pids):
    completed = subprocess.CompletedProcess(["ps"], 0, stdout=stdout, stderr="")

    observed = teardown.read_process_table(
        platform="darwin", run=lambda *_args, **_kwargs: completed
    )

    assert observed["state"] == state
    assert [identity["pid"] for identity in observed["identities"]] == pids
    assert observed["error"] is not None


def test_ps_process_table_failed_read_is_unknown():
    completed = subprocess.CompletedProcess(["ps"], 1, stdout="", stderr="ps failed")

    observed = teardown.read_process_table(
        platform="darwin", run=lambda *_args, **_kwargs: completed
    )

    assert observed == {"state": "unknown", "identities": [], "error": "ps failed"}


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


@pytest.mark.parametrize(
    ("membership", "control_group", "observed_group"),
    [
        (
            "0::/user.slice/hopper.scope\n",
            "/user.slice/hopper.scope",
            "/user.slice/hopper.scope",
        ),
        (
            "7:memory:/legacy/memory\n3:cpu,cpuacct:/legacy/cpu\n0::/user.slice/hopper.scope\n",
            "/user.slice/hopper.scope",
            "/user.slice/hopper.scope",
        ),
        (
            "0::/user.slice/odd:name.scope\n",
            "/user.slice/odd:name.scope",
            "/user.slice/odd:name.scope",
        ),
        (
            "0::/user.slice/hopper.scope/\n",
            "/user.slice/hopper.scope",
            "/user.slice/hopper.scope",
        ),
    ],
)
def test_capture_worker_cgroup_membership_proves_exact_membership(
    tmp_path, monkeypatch, membership, control_group, observed_group
):
    worker = _linux_process(102, 101, 102, 3)

    def read(path):
        if path == tmp_path / "102/cgroup":
            return membership
        assert path == tmp_path / "102/stat"
        return _linux_stat(pid=102, ppid=101, pgid=102, starttime=3)

    monkeypatch.setattr(oom, "_read_text", read)

    assert teardown.capture_worker_cgroup_membership(worker, control_group, proc_root=tmp_path) == {
        "state": "proven",
        "control_group": observed_group,
        "error": None,
    }


def test_capture_worker_cgroup_membership_refuses_blank_file(tmp_path, monkeypatch):
    monkeypatch.setattr(oom, "_read_text", lambda _path: "\n \n\t\n")

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed == {
        "state": "cannot-tell",
        "control_group": None,
        "error": "worker has no cgroup-v2 membership",
    }


def test_capture_worker_cgroup_membership_refuses_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(oom, "_read_text", MagicMock(side_effect=FileNotFoundError))

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed == {
        "state": "cannot-tell",
        "control_group": None,
        "error": "worker cgroup membership file is missing",
    }


def test_capture_worker_cgroup_membership_refuses_process_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(oom, "_read_text", MagicMock(side_effect=ProcessLookupError))

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed == {
        "state": "cannot-tell",
        "control_group": None,
        "error": "worker exited before cgroup membership could be read",
    }


def test_capture_worker_cgroup_membership_refuses_unreadable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(oom, "_read_text", MagicMock(side_effect=PermissionError("denied")))

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed == {
        "state": "cannot-tell",
        "control_group": None,
        "error": "worker cgroup membership is unreadable: denied",
    }


def test_capture_worker_cgroup_membership_refuses_malformed_line(tmp_path, monkeypatch):
    monkeypatch.setattr(oom, "_read_text", lambda _path: "malformed\n")

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed["state"] == "cannot-tell"
    assert observed["error"] == "worker cgroup membership is malformed"


def test_capture_worker_cgroup_membership_refuses_missing_v2_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(oom, "_read_text", lambda _path: "7:memory:/legacy\n")

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed["state"] == "cannot-tell"
    assert observed["error"] == "worker has no cgroup-v2 membership"


def test_capture_worker_cgroup_membership_refuses_ambiguous_v2_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(
        oom,
        "_read_text",
        lambda _path: "0::/user.slice/hopper.scope\n0::/user.slice/hopper.scope\n",
    )

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed["state"] == "cannot-tell"
    assert observed["error"] == "worker has ambiguous cgroup-v2 membership"


@pytest.mark.parametrize("path", ["", "relative.scope", "/", "/user.slice/../other.scope"])
def test_capture_worker_cgroup_membership_refuses_invalid_v2_paths(tmp_path, monkeypatch, path):
    monkeypatch.setattr(oom, "_read_text", lambda _path: f"0::{path}\n")

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed["state"] == "cannot-tell"
    assert observed["error"] == f"worker cgroup-v2 membership path is invalid: {path!r}"


@pytest.mark.parametrize(
    "path",
    [
        "/user.slice/hopper.scope-extra",
        "/user.slice/hopper.scope/inner",
        "/user.slice/sibling.scope",
    ],
)
def test_capture_worker_cgroup_membership_refuses_non_exact_paths(tmp_path, monkeypatch, path):
    monkeypatch.setattr(oom, "_read_text", lambda _path: f"0::{path}\n")

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed == {
        "state": "cannot-tell",
        "control_group": path,
        "error": (
            f"worker cgroup membership {path} does not match "
            "unit control group /user.slice/hopper.scope"
        ),
    }


def test_capture_worker_cgroup_membership_requires_linux_birth(tmp_path, monkeypatch):
    worker = _linux_process(102)
    worker["birth"] = {"kind": "ps-lstart", "boot_id": None, "value": "opaque"}
    read = MagicMock(side_effect=AssertionError("procfs must not be read"))
    monkeypatch.setattr(oom, "_read_text", read)

    observed = teardown.capture_worker_cgroup_membership(
        worker, "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed["state"] == "cannot-tell"
    assert observed["error"] == "worker does not have a Linux proc-stat birth identity"
    read.assert_not_called()


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (FileNotFoundError(), "gone"),
        (PermissionError("denied"), "denied"),
    ],
)
def test_capture_worker_cgroup_membership_refuses_unavailable_birth_recheck(
    tmp_path, monkeypatch, error, detail
):
    def read(path):
        if path.name == "cgroup":
            return "0::/user.slice/hopper.scope\n"
        raise error

    monkeypatch.setattr(oom, "_read_text", read)

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed["state"] == "cannot-tell"
    assert observed["control_group"] == "/user.slice/hopper.scope"
    assert observed["error"].endswith(detail)


def test_capture_worker_cgroup_membership_refuses_birth_change(tmp_path, monkeypatch):
    def read(path):
        if path.name == "cgroup":
            return "0::/user.slice/hopper.scope\n"
        return _linux_stat(pid=102, ppid=1, pgid=102, starttime=99)

    monkeypatch.setattr(oom, "_read_text", read)

    observed = teardown.capture_worker_cgroup_membership(
        _linux_process(102, start=3), "/user.slice/hopper.scope", proc_root=tmp_path
    )

    assert observed == {
        "state": "cannot-tell",
        "control_group": "/user.slice/hopper.scope",
        "error": "worker identity changed while membership was observed",
    }


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


def test_kill_cgroup_reports_structured_outcomes(tmp_path, monkeypatch):
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    record = {
        "absolute_path": str(cgroup),
        "identity": {"st_dev": cgroup.stat().st_dev, "st_ino": cgroup.stat().st_ino},
        "boot_id": "boot-one",
    }
    writes = []
    monkeypatch.setattr(oom, "_write_text", lambda path, value: writes.append((path.name, value)))

    assert teardown.kill_cgroup(record, boot_id="boot-one") == _force("signalled")
    assert writes == [("cgroup.kill", "1")]

    record["identity"]["st_ino"] += 1
    assert teardown.kill_cgroup(record, boot_id="boot-one") == _force(
        "unaddressable", "cgroup identity mismatch"
    )
    assert writes == [("cgroup.kill", "1")]

    record["absolute_path"] = str(tmp_path / "gone")
    assert teardown.kill_cgroup(record, boot_id="boot-one") == _force("already-gone")


def test_kill_cgroup_classifies_a_completed_write_as_signalled(tmp_path, monkeypatch):
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    record = {
        "absolute_path": str(cgroup),
        "identity": {"st_dev": cgroup.stat().st_dev, "st_ino": cgroup.stat().st_ino},
        "boot_id": "boot-one",
    }
    writes = []
    monkeypatch.setattr(oom, "_write_text", lambda path, value: writes.append((path.name, value)))
    monkeypatch.setattr(teardown, "_cgroup_identity_matches", lambda *_args: False)

    assert teardown.kill_cgroup(record, boot_id="boot-one") == _force("signalled")
    assert writes == [("cgroup.kill", "1")]


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ENODEV])
def test_retained_cgroup_removed_read_with_matching_identity_proves_removal(
    tmp_path, monkeypatch, error_number
):
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    fd = os.open(cgroup, os.O_RDONLY | os.O_DIRECTORY)
    record = {
        "identity": {"st_dev": cgroup.stat().st_dev, "st_ino": cgroup.stat().st_ino},
        "boot_id": "boot-one",
    }

    def removed(_path):
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(oom, "_read_text", removed)
    try:
        assert teardown.observe_retained_cgroup(fd, record, boot_id="boot-one") == "empty"
    finally:
        os.close(fd)


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
    observed = teardown.reopen_process_pidfd(
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
    observed = teardown.reopen_process_pidfd(
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
    [
        (None, _force("signalled")),
        (ProcessLookupError(), _force("already-gone")),
        (PermissionError("denied"), _force("unaddressable", "denied")),
    ],
)
def test_signal_process_pidfd_never_uses_a_numeric_pid(monkeypatch, error, expected):
    calls = []

    def fake_signal(fd, sig, info, flags):
        calls.append((fd, sig, info, flags))
        if error is not None:
            raise error

    monkeypatch.setattr(
        oom,
        "_read_text",
        lambda path: (
            "boot-one\n"
            if path.name == "boot_id"
            else _linux_stat(pid=100, ppid=1, pgid=100, starttime=9)
        ),
    )
    interface = _pidfd_interface(signal_call=fake_signal)
    assert (
        teardown.signal_process_pidfd(17, _linux_process(100, 1, 100, 9), pidfd_interface=interface)
        == expected
    )
    assert calls == [(17, signal.SIGKILL, None, 0)]


def test_pid_reuse_before_signal_leaves_replacement_alive(monkeypatch):
    signals = []
    monkeypatch.setattr(
        oom,
        "_read_text",
        lambda path: (
            "boot-one\n"
            if path.name == "boot_id"
            else _linux_stat(pid=100, ppid=1, pgid=100, starttime=10)
        ),
    )

    outcome = teardown.signal_process_pidfd(
        17,
        _linux_process(100, 1, 100, 9),
        pidfd_interface=_pidfd_interface(signal_call=lambda *args: signals.append(args)),
    )

    assert outcome == _force("already-gone")
    assert signals == []


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
    assert observed["resolution"] == "complete"
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


def test_partial_linux_table_resolves_every_owned_identity_and_keeps_discovering():
    owned = [_linux_process(10), _linux_process(11, 10)]
    table = {
        "state": "partial",
        "identities": [_linux_process(10), _linux_process(12, 10)],
        "error": "PID 11: unreadable",
    }
    reads = []

    observed = teardown.observe_bounded_processes(
        owned,
        platform="linux",
        process_table=table,
        process_reader=lambda pid, **_kwargs: (
            reads.append(pid) or {"state": "gone", "identity": None, "error": None}
        ),
    )

    assert observed["state"] == "populated"
    assert observed["resolution"] == "partial"
    assert [identity["pid"] for identity in observed["identities"]] == [10, 12]
    assert reads == [11]


def test_partial_ps_missing_identity_stays_unknown_but_birth_change_releases_it():
    owned = [_ps_process(10)]
    table = {"state": "partial", "identities": [], "error": "malformed row"}

    unknown = teardown.observe_bounded_processes(
        owned,
        platform="darwin",
        process_table=table,
        process_reader=lambda _pid, **_kwargs: {
            "state": "cannot-tell",
            "identity": None,
            "error": "ps exited 1",
        },
    )
    reused = teardown.observe_bounded_processes(
        owned,
        platform="darwin",
        process_table=table,
        process_reader=lambda _pid, **_kwargs: {
            "state": "alive",
            "identity": _ps_process(10, start="Tue Aug 11 22:29:26 2026"),
            "error": None,
        },
    )

    assert unknown["state"] == "cannot-tell"
    assert unknown["identities"] == owned
    assert reused["state"] == "empty"
    assert reused["identities"] == []


def test_unknown_table_retains_owned_identity_across_step_boundary():
    owned = [_ps_process(10)]

    observed = teardown.observe_bounded_processes(
        owned,
        platform="darwin",
        process_table={"state": "unknown", "identities": [], "error": "ps timed out"},
    )

    assert observed["state"] == "cannot-tell"
    assert observed["identities"] == owned


def test_strict_containment_proves_all_three_surfaces_without_signalling():
    _clock, now_ns, poll = _fake_clock()
    signals = []

    result = _observe(
        _containment_record("linux-strict"),
        {
            "observe_cgroup": lambda: "empty",
            "observe_supervisor": lambda: "gone",
            "observe_pane_root": lambda: "gone",
            "kill_cgroup": lambda: signals.append("cgroup") or _force("signalled"),
            "kill_supervisor": lambda: signals.append("supervisor") or _force("signalled"),
            "kill_pane_root": lambda: signals.append("pane") or _force("signalled"),
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "proven"
    assert result["result"] == "linux-strict-empty"
    assert signals == []


@pytest.mark.parametrize(
    ("cgroup", "supervisor", "pane_root"),
    [
        ("populated", "gone", "gone"),
        ("empty", "alive", "gone"),
        ("empty", "gone", "alive"),
    ],
)
def test_each_strict_surface_must_be_absent_before_proof(cgroup, supervisor, pane_root):
    clock, now_ns, poll = _fake_clock()
    record = _expired_grace("linux-strict")

    result = _observe(
        record,
        {
            "observe_cgroup": lambda: cgroup,
            "observe_supervisor": lambda: supervisor,
            "observe_pane_root": lambda: pane_root,
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "kill_pending"
    assert result["result"] is None
    assert clock["polls"] == []


def test_containment_waiting_budget_is_materialized_before_worker_polling():
    _clock, now_ns, _poll = _fake_clock(start=5_000_000_000)
    record = _containment_record("linux-strict", state="pane_close_pending")

    started = teardown.start_containment(record, now_ns=now_ns)

    assert started["state"] == "grace"
    assert started["started_monotonic_ns"] == 5_000_000_000
    assert started["deadline_monotonic_ns"] == 35_000_000_000
    assert record["containment"]["deadline_monotonic_ns"] is None


def test_kill_action_escalates_despite_ambiguity_and_arms_verification_budget():
    clock, now_ns, poll = _fake_clock()
    record = _expired_grace("linux-strict", action_type="kill")

    result = _observe(
        record,
        {
            "observe_cgroup": lambda: "cannot-tell",
            "observe_supervisor": lambda: "cannot-tell",
            "observe_pane_root": lambda: "cannot-tell",
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "kill_pending"
    assert result["started_monotonic_ns"] == clock["now"]
    assert result["deadline_monotonic_ns"] - result["started_monotonic_ns"] == int(
        teardown.CONTAINMENT_TIMEOUT_SEC * 1_000_000_000
    )
    assert clock["polls"] == []


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.75, 0.99])
def test_verification_budget_is_independent_fraction_with_zero_cost_observations(fraction):
    clock, now_ns, poll = _fake_clock()
    record = _containment_record("linux-strict")
    waiting = {
        "observe_cgroup": lambda: "populated",
        "observe_supervisor": lambda: "alive",
        "observe_pane_root": lambda: "gone",
    }

    pending = _observe(record, waiting, now_ns=now_ns, poll=poll)

    verification_start = pending["started_monotonic_ns"]
    assert pending["state"] == "kill_pending"
    assert pending["deadline_monotonic_ns"] == verification_start + 30_000_000_000
    record["containment"] = pending
    signals = []
    prove_at = verification_start + int(fraction * 30_000_000_000)

    def surface(present, absent):
        return lambda: absent if signals and clock["now"] >= prove_at else present

    proven = _observe(
        record,
        {
            "observe_cgroup": surface("populated", "empty"),
            "observe_supervisor": surface("alive", "gone"),
            "observe_pane_root": lambda: "gone",
            "kill_cgroup": lambda: signals.append("cgroup") or _force("signalled"),
            "kill_supervisor": lambda: signals.append("supervisor") or _force("signalled"),
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert proven["state"] == "proven"
    assert proven["result"] == "linux-strict-killed-empty"
    assert signals == ["cgroup", "supervisor"]
    assert clock["now"] - verification_start >= int(fraction * 30_000_000_000)


def test_force_observes_before_signalling_and_already_gone_is_not_killed():
    clock, now_ns, poll = _fake_clock()
    record = _containment_record("linux-strict", state="kill_pending")
    record["containment"] = teardown.arm_phase(record["containment"], "kill_pending", now_ns=now_ns)
    events = []
    cgroup = {"state": "populated"}

    def observe_cgroup():
        events.append("observe-cgroup")
        return cgroup["state"]

    def kill_cgroup():
        events.append("kill-cgroup")
        cgroup["state"] = "empty"
        return _force("already-gone")

    result = _observe(
        record,
        {
            "observe_cgroup": observe_cgroup,
            "observe_supervisor": lambda: events.append("observe-supervisor") or "gone",
            "observe_pane_root": lambda: events.append("observe-pane") or "gone",
            "kill_cgroup": kill_cgroup,
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert events[:4] == ["observe-cgroup", "observe-supervisor", "observe-pane", "kill-cgroup"]
    assert result["state"] == "proven"
    assert result["result"] == "linux-strict-empty"


def test_unaddressable_force_target_does_not_prevent_independent_signals():
    _clock, now_ns, poll = _fake_clock()
    record = _containment_record("linux-strict", state="kill_pending")
    record["containment"] = teardown.arm_phase(record["containment"], "kill_pending", now_ns=now_ns)
    alive = {"value": True}
    calls = []

    result = _observe(
        record,
        {
            "observe_cgroup": lambda: "empty" if not alive["value"] else "populated",
            "observe_supervisor": lambda: "gone" if not alive["value"] else "alive",
            "observe_pane_root": lambda: "gone",
            "kill_cgroup": lambda: (
                calls.append("cgroup") or _force("unaddressable", "identity changed")
            ),
            "kill_supervisor": lambda: (
                calls.append("supervisor") or alive.update(value=False) or _force("signalled")
            ),
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert calls == ["cgroup", "supervisor"]
    assert result["state"] == "proven"
    assert result["result"] == "linux-strict-killed-empty"


def test_permanent_ambiguity_escalates_then_blocks_at_force_budget_expiry():
    clock, now_ns, poll = _fake_clock()
    record = _containment_record("linux-strict")
    ambiguous = {
        "observe_cgroup": lambda: "cannot-tell",
        "observe_supervisor": lambda: "cannot-tell",
        "observe_pane_root": lambda: "cannot-tell",
    }

    pending = _observe(record, ambiguous, now_ns=now_ns, poll=poll)
    assert pending["state"] == "kill_pending"
    assert pending["last_error"] is None

    record["containment"] = pending
    blocked = _observe(record, ambiguous, now_ns=now_ns, poll=poll)

    assert blocked["state"] == "kill_pending"
    assert "ambiguous" in blocked["last_error"]
    assert blocked["result"] is None


def test_transient_waiting_ambiguity_resolves_without_force():
    clock, now_ns, poll = _fake_clock()
    calls = {"count": 0}

    def cgroup():
        calls["count"] += 1
        return "cannot-tell" if calls["count"] < 3 else "empty"

    result = _observe(
        _containment_record("linux-strict"),
        {
            "observe_cgroup": cgroup,
            "observe_supervisor": lambda: "gone",
            "observe_pane_root": lambda: "gone",
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "proven"
    assert result["result"] == "linux-strict-empty"
    assert calls["count"] == 3
    assert len(clock["polls"]) == 2


def test_programming_error_is_named_and_not_retried():
    _clock, now_ns, poll = _fake_clock()
    calls = []

    def broken():
        calls.append("cgroup")
        raise TypeError("bad observer wiring")

    result = _observe(
        _containment_record("linux-strict"),
        {
            "observe_cgroup": broken,
            "observe_supervisor": lambda: "gone",
            "observe_pane_root": lambda: "gone",
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "grace"
    assert result["last_error"] == ("observer observe_cgroup raised TypeError: bad observer wiring")
    assert calls == ["cgroup"]


def test_force_programming_error_is_distinct_and_other_target_is_addressed():
    _clock, now_ns, poll = _fake_clock()
    record = _containment_record("linux-strict", state="kill_pending")
    record["containment"] = teardown.arm_phase(record["containment"], "kill_pending", now_ns=now_ns)
    calls = []

    def broken():
        calls.append("cgroup")
        raise RuntimeError("write contract broken")

    result = _observe(
        record,
        {
            "observe_cgroup": lambda: "populated",
            "observe_supervisor": lambda: "alive",
            "observe_pane_root": lambda: "gone",
            "kill_cgroup": broken,
            "kill_supervisor": lambda: calls.append("supervisor") or _force("signalled"),
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert calls == ["cgroup", "supervisor"]
    assert result["state"] == "verify_after_kill"
    assert result["last_error"] == (
        "force action kill_cgroup raised RuntimeError: write contract broken"
    )


def test_boot_mismatch_rearms_without_comparing_stale_monotonic_values():
    clock, now_ns, poll = _fake_clock(start=7_000_000_000)
    record = _containment_record("linux-strict", state="grace")
    record["containment"]["started_monotonic_ns"] = 999_000_000_000
    record["containment"]["deadline_monotonic_ns"] = 1

    result = _observe(
        record,
        {
            "observe_cgroup": lambda: "empty",
            "observe_supervisor": lambda: "gone",
            "observe_pane_root": lambda: "gone",
        },
        host_boot_identity="boot-two",
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "proven"
    assert result["started_monotonic_ns"] == 7_000_000_000
    assert result["deadline_monotonic_ns"] == 37_000_000_000
    assert clock["polls"] == []


def test_same_boot_restart_preserves_remaining_waiting_budget():
    clock, now_ns, poll = _fake_clock(start=20_000_000_000)
    record = _containment_record("linux-strict", state="grace")
    record["containment"]["started_monotonic_ns"] = 1_000_000_000
    record["containment"]["deadline_monotonic_ns"] = 31_000_000_000

    result = _observe(
        record,
        {
            "observe_cgroup": lambda: "populated",
            "observe_supervisor": lambda: "gone",
            "observe_pane_root": lambda: "gone",
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "kill_pending"
    assert sum(clock["polls"]) == pytest.approx(11.0)
    assert result["started_monotonic_ns"] == 31_000_000_000


@pytest.mark.parametrize(
    ("mode", "cursor"),
    [("linux-strict", "kill_pending"), ("darwin-bounded", "grace")],
)
def test_legacy_blocked_state_normalizes_with_fresh_budget(mode, cursor):
    _clock, now_ns, _poll = _fake_clock(start=9_000_000_000)
    record = _containment_record(mode, state="blocked")
    record["containment"]["started_monotonic_ns"] = 1
    record["containment"]["deadline_monotonic_ns"] = 2
    record["containment"]["last_error"] = "legacy block"

    normalized = teardown.normalize_legacy_blocked_containment(record, now_ns=now_ns)

    assert normalized["state"] == cursor
    assert normalized["started_monotonic_ns"] == 9_000_000_000
    assert normalized["deadline_monotonic_ns"] == 39_000_000_000
    assert normalized["last_error"] is None


def test_darwin_bounded_ambiguity_retries_then_proves():
    clock, now_ns, poll = _fake_clock()
    calls = {"count": 0}

    def bounded():
        calls["count"] += 1
        if calls["count"] < 3:
            return {"state": "cannot-tell", "count": None}
        return {"state": "empty", "count": 0}

    result = _observe(
        _containment_record("darwin-bounded"),
        {"observe_bounded": bounded, "observe_pane": lambda: "gone"},
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "proven"
    assert result["result"] == "darwin-bounded-empty"
    assert "unproven" in result["proof_label"]
    assert len(clock["polls"]) == 2


def test_degraded_permanent_ambiguity_blocks_with_cursor_and_original_budget():
    clock, now_ns, poll = _fake_clock()

    result = _observe(
        _containment_record("other-bounded-no-birth"),
        {
            "observe_bounded": lambda: {"state": "cannot-tell", "count": None},
            "observe_pane": lambda: "gone",
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "grace"
    assert result["last_error"] == "bounded containment remained ambiguous until budget expiry"
    assert result["started_monotonic_ns"] == 1_000_000_000
    assert result["deadline_monotonic_ns"] == 31_000_000_000
    assert sum(clock["polls"]) == pytest.approx(30.0)


@pytest.mark.parametrize("latency_ms", range(0, 2001, 80))
def test_charged_observation_and_force_costs_still_receive_a_full_force_budget(latency_ms):
    clock, now_ns, poll = _fake_clock()
    charge_ns = latency_ms * 1_000_000
    record = _containment_record("linux-strict")

    def charged(value):
        def call():
            clock["now"] += charge_ns
            return value

        return call

    pending = _observe(
        record,
        {
            "observe_cgroup": charged("populated"),
            "observe_supervisor": charged("alive"),
            "observe_pane_root": charged("alive"),
        },
        now_ns=now_ns,
        poll=poll,
    )
    assert pending["state"] == "kill_pending"

    record["containment"] = pending
    force_entry = clock["now"]
    prove_at = force_entry + 15_000_000_000
    signals = []

    def charged_surface(present, absent):
        def observe():
            clock["now"] += charge_ns
            return absent if signals and clock["now"] >= prove_at else present

        return observe

    def charged_force(name):
        def force():
            clock["now"] += charge_ns
            signals.append(name)
            return _force("signalled")

        return force

    result = _observe(
        record,
        {
            "observe_cgroup": charged_surface("populated", "empty"),
            "observe_supervisor": charged_surface("alive", "gone"),
            "observe_pane_root": charged_surface("alive", "gone"),
            "kill_cgroup": charged_force("cgroup"),
            "kill_supervisor": charged_force("supervisor"),
            "kill_pane_root": charged_force("pane-root"),
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert signals == ["cgroup", "supervisor", "pane-root"]
    assert result["state"] == "proven"
    assert result["result"] == "linux-strict-killed-empty"
    assert result["deadline_monotonic_ns"] == pending["deadline_monotonic_ns"]


def test_explicit_kill_honours_grace_and_proves_without_forcing():
    """A kill waits for the closed PTY to drain instead of going straight to SIGKILL.

    The pane is already closed by the time containment starts, so a runner that
    exits cleanly empties the cgroup during grace. Escalating immediately would
    SIGKILL a process that was about to release everything it held.
    """
    clock, now_ns, poll = _fake_clock()
    record = _containment_record("linux-strict", state="pane_close_pending")
    record["action_type"] = "kill"
    record["containment"] = teardown.start_containment(record, now_ns=now_ns)
    assert record["containment"]["state"] == "grace"

    observations = ["populated", "populated", "empty"]

    result = _observe(
        record,
        {
            "observe_cgroup": lambda: observations.pop(0),
            "observe_supervisor": lambda: "gone",
            "observe_pane_root": lambda: "gone",
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "proven"
    # proven by draining, not by killing — the killed-empty label is the other path
    assert result["result"] == "linux-strict-empty"
    assert clock["polls"], "a kill must actually wait during grace"


def test_explicit_kill_still_escalates_when_the_runner_will_not_exit():
    """Grace is a ceiling, not a promise: a wedged runner is still force-killed."""
    clock, now_ns, poll = _fake_clock()
    record = _expired_grace("linux-strict", action_type="kill")

    result = _observe(
        record,
        {
            "observe_cgroup": lambda: "populated",
            "observe_supervisor": lambda: "alive",
            "observe_pane_root": lambda: "alive",
        },
        now_ns=now_ns,
        poll=poll,
    )

    assert result["state"] == "kill_pending"

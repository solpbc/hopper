# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure ownership and bounded-containment primitives for completion teardown."""

import ctypes
import errno
import os
import re
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from hopper import oom, tmux

CONTAINMENT_TIMEOUT_SEC = 30.0
CONTAINMENT_POLL_SEC = 0.05
PROCESS_QUERY_TIMEOUT_SEC = 1.0

_PS_ROW = re.compile(
    r"^\s*(?P<pid>[0-9]+)\s+(?P<ppid>[0-9]+)\s+(?P<pgid>[0-9]+)\s+(?P<start>.+?)\s*$"
)


def _libc_pidfd_interface() -> dict | None:
    """Resolve the two scalar pidfd calls from the process's libc namespace."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        pidfd_open = libc.pidfd_open
        pidfd_send_signal = libc.pidfd_send_signal
    except (AttributeError, OSError):
        return None

    pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    pidfd_open.restype = ctypes.c_int
    pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    pidfd_send_signal.restype = ctypes.c_int

    def checked_pidfd_open(pid: int, flags: int) -> int:
        result = pidfd_open(pid, flags)
        if result < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        return result

    def checked_pidfd_send_signal(fd: int, sig: int, info: None, flags: int) -> None:
        result = pidfd_send_signal(fd, sig, info, flags)
        if result < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))

    return {
        "source": "libc",
        "open": checked_pidfd_open,
        "send_signal": checked_pidfd_send_signal,
    }


def resolve_pidfd_interface() -> dict | None:
    """Resolve one complete pidfd interface, preferring Python's stdlib bindings."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if callable(pidfd_open) and callable(pidfd_send_signal):
        return {
            "source": "stdlib",
            "open": pidfd_open,
            "send_signal": pidfd_send_signal,
        }
    return _libc_pidfd_interface()


def platform_name(value: str | None = None) -> str:
    """Map the supported platform branches to their persisted names."""
    value = sys.platform if value is None else value
    if value.startswith("linux"):
        return "linux"
    if value == "darwin":
        return "darwin"
    return "other"


def read_boot_id(*, proc_root: Path = Path("/proc")) -> str | None:
    """Read the Linux boot identity through the existing mockable text seam."""
    try:
        value = oom._read_text(proc_root / "sys/kernel/random/boot_id").strip()
    except (OSError, ValueError):
        return None
    return value or None


def read_host_boot_identity(
    *, platform: str | None = None, run: Callable | None = None
) -> str | None:
    """Read a same-boot token without parsing platform timestamps."""
    if platform_name(platform) == "linux":
        return read_boot_id()
    observed = read_ps_process_identity(1, run=run)
    if observed["state"] != "alive":
        return None
    return f"ps-pid1:{observed['identity']['birth']['value']}"


def parse_linux_process_stat(text: str, boot_id: str) -> dict:
    """Parse the identity-bearing fields from one Linux /proc PID stat row."""
    opening = text.find("(")
    closing = text.rfind(")")
    if opening < 1 or closing <= opening:
        raise ValueError("malformed /proc stat row")
    try:
        pid = int(text[:opening].strip())
    except ValueError as error:
        raise ValueError("malformed /proc stat PID") from error
    fields = text[closing + 1 :].split()
    if len(fields) < 20:
        raise ValueError("incomplete /proc stat row")
    try:
        ppid = int(fields[1])
        pgid = int(fields[2])
        starttime = int(fields[19])
    except ValueError as error:
        raise ValueError("malformed /proc stat identity") from error
    if pid < 1 or ppid < 0 or pgid < 1 or starttime < 0 or not boot_id:
        raise ValueError("invalid /proc stat identity")
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid,
        "birth": {
            "kind": "linux-proc-starttime",
            "boot_id": boot_id,
            "value": str(starttime),
        },
    }


def parse_ps_process_row(text: str) -> dict:
    """Parse one ps row while retaining lstart as an opaque comparison token."""
    rows = [line for line in text.splitlines() if line.strip()]
    if len(rows) != 1:
        raise ValueError("ps did not return exactly one process row")
    match = _PS_ROW.fullmatch(rows[0])
    if match is None:
        raise ValueError("malformed ps process row")
    start = match.group("start")
    pid = int(match.group("pid"))
    ppid = int(match.group("ppid"))
    pgid = int(match.group("pgid"))
    if not start or pid < 1 or ppid < 0 or pgid < 1:
        raise ValueError("invalid ps process identity")
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid,
        "birth": {"kind": "ps-lstart", "boot_id": None, "value": start},
    }


def _process_result(state: str, identity: dict | None = None, error: str | None = None) -> dict:
    return {"state": state, "identity": identity, "error": error}


def read_linux_process_identity(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
    boot_id: str | None = None,
) -> dict:
    """Read a Linux PID identity, distinguishing absence from ambiguity."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return _process_result("cannot-tell", error="invalid PID")
    boot_id = boot_id or read_boot_id(proc_root=proc_root)
    if boot_id is None:
        return _process_result("cannot-tell", error="Linux boot identity unavailable")
    try:
        text = oom._read_text(proc_root / str(pid) / "stat")
    except (FileNotFoundError, ProcessLookupError):
        return _process_result("gone")
    except OSError as error:
        return _process_result("cannot-tell", error=str(error))
    try:
        identity = parse_linux_process_stat(text, boot_id)
    except ValueError as error:
        return _process_result("cannot-tell", error=str(error))
    if identity["pid"] != pid:
        return _process_result("cannot-tell", error="proc stat PID mismatch")
    return _process_result("alive", identity=identity)


def read_ps_process_identity(
    pid: int,
    *,
    run: Callable | None = None,
    timeout: float = PROCESS_QUERY_TIMEOUT_SEC,
) -> dict:
    """Read a Darwin/degraded identity; every ps failure remains unknown."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return _process_result("cannot-tell", error="invalid PID")
    run = subprocess.run if run is None else run
    try:
        result = run(
            ["ps", "-o", "pid=,ppid=,pgid=,lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _process_result("cannot-tell", error=str(error))
    if result.returncode != 0:
        return _process_result(
            "cannot-tell", error=result.stderr.strip() or f"ps exited {result.returncode}"
        )
    try:
        identity = parse_ps_process_row(result.stdout)
    except ValueError as error:
        return _process_result("cannot-tell", error=str(error))
    if identity["pid"] != pid:
        return _process_result("cannot-tell", error="ps PID mismatch")
    return _process_result("alive", identity=identity)


def read_process_identity(pid: int, *, platform: str | None = None, **kwargs) -> dict:
    """Read one platform process identity through the supported source."""
    if platform_name(platform) == "linux":
        return read_linux_process_identity(pid, **kwargs)
    return read_ps_process_identity(pid, **kwargs)


def _read_linux_process_table(*, proc_root: Path, boot_id: str) -> dict:
    identities = []
    try:
        entries = list(proc_root.iterdir())
    except OSError as error:
        return {"state": "cannot-tell", "identities": [], "error": str(error)}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        observed = read_linux_process_identity(
            int(entry.name), proc_root=proc_root, boot_id=boot_id
        )
        if observed["state"] == "gone":
            continue
        if observed["state"] != "alive":
            return {"state": "cannot-tell", "identities": [], "error": observed["error"]}
        identities.append(observed["identity"])
    return {
        "state": "complete",
        "identities": sorted(identities, key=lambda item: item["pid"]),
        "error": None,
    }


def _read_ps_process_table(*, run: Callable, timeout: float) -> dict:
    try:
        result = run(
            ["ps", "-axo", "pid=,ppid=,pgid=,lstart="],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"state": "cannot-tell", "identities": [], "error": str(error)}
    if result.returncode != 0:
        return {
            "state": "cannot-tell",
            "identities": [],
            "error": result.stderr.strip() or f"ps exited {result.returncode}",
        }
    identities = []
    for row in result.stdout.splitlines():
        if not row.strip():
            continue
        try:
            identities.append(parse_ps_process_row(row))
        except ValueError as error:
            return {"state": "cannot-tell", "identities": [], "error": str(error)}
    pids = [identity["pid"] for identity in identities]
    if len(pids) != len(set(pids)):
        return {"state": "cannot-tell", "identities": [], "error": "duplicate ps PID"}
    return {
        "state": "complete",
        "identities": sorted(identities, key=lambda item: item["pid"]),
        "error": None,
    }


def read_process_table(
    *,
    platform: str | None = None,
    proc_root: Path = Path("/proc"),
    boot_id: str | None = None,
    run: Callable | None = None,
    timeout: float = PROCESS_QUERY_TIMEOUT_SEC,
) -> dict:
    """Read one complete process table or return a fail-closed observation."""
    if platform_name(platform) == "linux":
        boot_id = boot_id or read_boot_id(proc_root=proc_root)
        if boot_id is None:
            return {"state": "cannot-tell", "identities": [], "error": "boot ID unavailable"}
        return _read_linux_process_table(proc_root=proc_root, boot_id=boot_id)
    return _read_ps_process_table(run=subprocess.run if run is None else run, timeout=timeout)


def same_birth(left: dict, right: dict) -> bool:
    """Compare the complete immutable birth tuple for two process facts."""
    return left.get("pid") == right.get("pid") and left.get("birth") == right.get("birth")


def capture_descendants(root_identities: list[dict], process_table: list[dict]) -> list[dict]:
    """Return the transitive descendants of the supplied roots, sorted by PID."""
    root_pids = {identity["pid"] for identity in root_identities}
    owned_pids = set(root_pids)
    changed = True
    while changed:
        changed = False
        for identity in process_table:
            if identity["pid"] not in owned_pids and identity["ppid"] in owned_pids:
                owned_pids.add(identity["pid"])
                changed = True
    return sorted(
        [identity for identity in process_table if identity["pid"] in owned_pids - root_pids],
        key=lambda item: item["pid"],
    )


def capture_scope_cgroup(
    systemctl: str,
    unit_name: str,
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    boot_id: str,
) -> dict:
    """Capture a loaded unit and exact server-visible cgroup directory identity."""
    observed = oom.read_scope_control_group(systemctl, unit_name)
    if observed["state"] != "present":
        return {"state": "cannot-tell", "unit": None, "cgroup": None, "error": observed["error"]}
    relative = PurePosixPath(observed["control_group"])
    if not relative.is_absolute() or relative == PurePosixPath("/") or ".." in relative.parts:
        return {
            "state": "cannot-tell",
            "unit": None,
            "cgroup": None,
            "error": "invalid ControlGroup path",
        }
    absolute = cgroup_root.joinpath(*relative.parts[1:])
    try:
        fd = os.open(absolute, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            stat = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as error:
        return {"state": "cannot-tell", "unit": None, "cgroup": None, "error": str(error)}
    return {
        "state": "captured",
        "unit": {
            "name": unit_name,
            "load_state": observed["load_state"],
            "control_group": observed["control_group"],
        },
        "cgroup": {
            "relative_path": observed["control_group"],
            "absolute_path": str(absolute),
            "identity": {"st_dev": stat.st_dev, "st_ino": stat.st_ino},
            "boot_id": boot_id,
        },
        "error": None,
    }


def capture_ownership(
    *,
    pane_id: str,
    supervisor_pid: int,
    worker_pid: int,
    process_group: int,
    unit_name: str | None = None,
    systemctl: str | None = None,
    platform: str | None = None,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    run: Callable | None = None,
) -> dict:
    """Capture all ownership facts required before exact pane closure."""
    platform = platform_name(platform)
    pane = tmux.pane_identity(pane_id)
    if pane is None:
        return {"state": "cannot-tell", "ownership": None, "error": "pane identity unavailable"}
    boot_id = read_boot_id(proc_root=proc_root) if platform == "linux" else None
    if platform == "linux" and boot_id is None:
        return {"state": "cannot-tell", "ownership": None, "error": "boot ID unavailable"}

    run = subprocess.run if run is None else run
    identity_kwargs = (
        {"proc_root": proc_root, "boot_id": boot_id} if platform == "linux" else {"run": run}
    )
    roots = []
    for label, pid in (
        ("pane root", pane["pane_pid"]),
        ("supervisor", supervisor_pid),
        ("worker", worker_pid),
    ):
        observed = read_process_identity(pid, platform=platform, **identity_kwargs)
        if observed["state"] != "alive":
            return {
                "state": "cannot-tell",
                "ownership": None,
                "error": f"{label} identity unavailable: {observed['error'] or observed['state']}",
            }
        roots.append(observed["identity"])
    if roots[1]["pgid"] != process_group:
        return {"state": "cannot-tell", "ownership": None, "error": "supervisor PGID mismatch"}

    table = read_process_table(
        platform=platform,
        proc_root=proc_root,
        boot_id=boot_id,
        run=run,
    )
    if table["state"] != "complete":
        return {"state": "cannot-tell", "ownership": None, "error": table["error"]}
    by_pid = {identity["pid"]: identity for identity in table["identities"]}
    if any(
        root["pid"] not in by_pid or not same_birth(root, by_pid[root["pid"]]) for root in roots
    ):
        return {
            "state": "cannot-tell",
            "ownership": None,
            "error": "root identity changed during capture",
        }
    # The bounded platforms cannot rely on a cgroup after pane closure.  Seed
    # their retained set with every member of the supervisor's original
    # process group as well as transitive descendants.  Retention is by birth
    # identity from this point onward; the numeric PGID is never a kill target.
    descendants_by_pid = {
        identity["pid"]: identity
        for identity in table["identities"]
        if identity["pgid"] == process_group
        and identity["pid"] not in {root["pid"] for root in roots}
    }
    descendants_by_pid.update(
        {identity["pid"]: identity for identity in capture_descendants(roots, table["identities"])}
    )
    descendants = sorted(descendants_by_pid.values(), key=lambda identity: identity["pid"])
    verified_descendants = []
    for descendant in descendants:
        observed = read_process_identity(descendant["pid"], platform=platform, **identity_kwargs)
        if observed["state"] == "gone":
            continue
        if observed["state"] != "alive":
            return {
                "state": "cannot-tell",
                "ownership": None,
                "error": "descendant identity became ambiguous during capture",
            }
        if same_birth(descendant, observed["identity"]):
            verified_descendants.append(observed["identity"])
    descendants = verified_descendants

    unit = None
    cgroup = None
    if platform == "linux" and unit_name is not None:
        if systemctl is None:
            return {"state": "cannot-tell", "ownership": None, "error": "systemctl unavailable"}
        scope = capture_scope_cgroup(
            systemctl,
            unit_name,
            cgroup_root=cgroup_root,
            boot_id=boot_id,
        )
        if scope["state"] != "captured":
            return {"state": "cannot-tell", "ownership": None, "error": scope["error"]}
        unit = scope["unit"]
        cgroup = scope["cgroup"]

    proof_mode = {
        "linux": "linux-strict" if cgroup is not None else "linux-degraded",
        "darwin": "darwin-bounded",
        "other": "other-bounded-no-birth",
    }[platform]
    return {
        "state": "captured",
        "ownership": {
            "platform": platform,
            "proof_mode": proof_mode,
            "pane": {
                "pane_id": pane["pane_id"],
                "window_id": pane["window_id"],
                "root_process": roots[0],
            },
            "supervisor": roots[1],
            "worker": roots[2],
            "process_group": process_group,
            "descendants": descendants,
            "unit": unit,
            "cgroup": cgroup,
        },
        "error": None,
    }


def _opened_cgroup(cgroup: dict) -> tuple[int | None, str | None]:
    try:
        fd = os.open(
            cgroup["absolute_path"],
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None, "absent"
    except OSError as error:
        return None, str(error)
    try:
        stat = os.fstat(fd)
    except OSError as error:
        os.close(fd)
        return None, str(error)
    expected = cgroup["identity"]
    if (stat.st_dev, stat.st_ino) != (expected["st_dev"], expected["st_ino"]):
        os.close(fd)
        return None, "cgroup identity mismatch"
    return fd, None


def _parse_cgroup_events(text: str) -> str:
    values = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "populated":
            values.append(fields[1])
    if values == ["0"]:
        return "empty"
    if values == ["1"]:
        return "populated"
    return "cannot-tell"


def observe_cgroup(cgroup: dict, unit_observation: dict, *, boot_id: str | None = None) -> str:
    """Observe recursive cgroup population with authoritative absence handling."""
    unit_state = unit_observation.get("state")
    if unit_state not in {"present", "absent"}:
        return "cannot-tell"
    if unit_state == "present" and unit_observation.get("control_group") != cgroup.get(
        "relative_path"
    ):
        return "cannot-tell"
    fd, error = _opened_cgroup(cgroup)
    if fd is None:
        if error == "absent" and unit_state == "absent":
            return "empty"
        return "cannot-tell"
    try:
        current_boot = boot_id or read_boot_id()
        if current_boot is None or current_boot != cgroup["boot_id"]:
            return "cannot-tell"
        path = Path(f"/proc/self/fd/{fd}") / "cgroup.events"
        try:
            result = _parse_cgroup_events(oom._read_text(path))
            after = os.fstat(fd)
        except (OSError, ValueError):
            return "cannot-tell"
        expected = cgroup["identity"]
        if (after.st_dev, after.st_ino) != (expected["st_dev"], expected["st_ino"]):
            return "cannot-tell"
        return result
    finally:
        os.close(fd)


def kill_cgroup(cgroup: dict, *, boot_id: str | None = None) -> bool:
    """Write cgroup.kill only through a verified exact cgroup directory."""
    fd, _error = _opened_cgroup(cgroup)
    if fd is None:
        return False
    try:
        current_boot = boot_id or read_boot_id()
        if current_boot is None or current_boot != cgroup["boot_id"]:
            return False
        try:
            oom._write_text(Path(f"/proc/self/fd/{fd}") / "cgroup.kill", "1")
            after = os.fstat(fd)
        except (OSError, ValueError):
            return False
        expected = cgroup["identity"]
        return (after.st_dev, after.st_ino) == (expected["st_dev"], expected["st_ino"])
    finally:
        os.close(fd)


def reopen_supervisor_pidfd(
    supervisor: dict,
    *,
    proc_root: Path = Path("/proc"),
    pidfd_interface: dict | None = None,
) -> dict:
    """Open the recorded PID first, then verify birth without risking PID reuse."""
    interface = resolve_pidfd_interface() if pidfd_interface is None else pidfd_interface
    if interface is None:
        return {
            "state": "cannot-tell",
            "fd": None,
            "error": "pidfd_open and pidfd_send_signal are unavailable",
        }
    try:
        fd = interface["open"](supervisor["pid"], 0)
    except (ProcessLookupError, FileNotFoundError):
        return {"state": "gone", "fd": None, "error": None}
    except OSError as error:
        if error.errno == errno.ESRCH:
            return {"state": "gone", "fd": None, "error": None}
        return {"state": "cannot-tell", "fd": None, "error": str(error)}
    current_boot = read_boot_id(proc_root=proc_root)
    if current_boot is None:
        os.close(fd)
        return {"state": "cannot-tell", "fd": None, "error": "Linux boot identity unavailable"}
    if current_boot != supervisor["birth"]["boot_id"]:
        os.close(fd)
        return {"state": "gone", "fd": None, "error": None}
    observed = read_linux_process_identity(
        supervisor["pid"],
        proc_root=proc_root,
        boot_id=current_boot,
    )
    if observed["state"] == "gone":
        os.close(fd)
        return {"state": "gone", "fd": None, "error": None}
    if observed["state"] != "alive":
        os.close(fd)
        return {"state": "cannot-tell", "fd": None, "error": observed["error"]}
    if not same_birth(supervisor, observed["identity"]):
        os.close(fd)
        return {"state": "gone", "fd": None, "error": None}
    return {"state": "alive", "fd": fd, "error": None}


def observe_pidfd(fd: int, *, pidfd_interface: dict | None = None) -> str:
    """Return whether a verified pidfd target is alive, gone, or ambiguous."""
    interface = resolve_pidfd_interface() if pidfd_interface is None else pidfd_interface
    if interface is None:
        return "cannot-tell"
    try:
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        events = poller.poll(0)
        if any(event & select.POLLNVAL for _descriptor, event in events):
            return "cannot-tell"
        if events:
            return "gone"
        interface["send_signal"](fd, 0, None, 0)
    except ProcessLookupError:
        return "gone"
    except (AttributeError, OSError):
        return "cannot-tell"
    return "alive"


def kill_supervisor_pidfd(fd: int, *, pidfd_interface: dict | None = None) -> bool:
    """SIGKILL only the stable process referenced by a verified pidfd."""
    interface = resolve_pidfd_interface() if pidfd_interface is None else pidfd_interface
    if interface is None:
        return False
    try:
        interface["send_signal"](fd, signal.SIGKILL, None, 0)
    except ProcessLookupError:
        return True
    except (AttributeError, OSError):
        return False
    return True


def close_owned_pane(
    ownership: dict,
    *,
    pane_reader: Callable[[str], dict | None] | None = None,
    pane_probe: Callable[[str], tmux.Liveness] | None = None,
    process_reader: Callable[..., dict] | None = None,
    kill: Callable[[str], bool] | None = None,
) -> dict:
    """Close only the pane whose window, PID, and birth identity still match."""
    pane_reader = tmux.pane_identity if pane_reader is None else pane_reader
    pane_probe = tmux.pane_liveness if pane_probe is None else pane_probe
    process_reader = read_process_identity if process_reader is None else process_reader
    kill = tmux.kill_pane if kill is None else kill
    recorded = ownership["pane"]
    pane_id = recorded["pane_id"]
    current = pane_reader(pane_id)
    if current is None:
        if pane_probe(pane_id) is tmux.Liveness.GONE:
            return {"state": "gone", "error": None}
        return {"state": "cannot-tell", "error": "pane identity unavailable"}
    if current["pane_id"] != pane_id or current["window_id"] != recorded["window_id"]:
        return {"state": "cannot-tell", "error": "pane or window identity mismatch"}
    if current["pane_pid"] != recorded["root_process"]["pid"]:
        return {"state": "cannot-tell", "error": "pane root PID mismatch"}
    observed = process_reader(current["pane_pid"], platform=ownership["platform"])
    if observed["state"] != "alive" or not same_birth(
        recorded["root_process"], observed["identity"]
    ):
        return {"state": "cannot-tell", "error": "pane root birth identity mismatch"}
    if not kill(pane_id) and pane_probe(pane_id) is not tmux.Liveness.GONE:
        return {"state": "cannot-tell", "error": "tmux pane close failed"}
    if pane_probe(pane_id) is not tmux.Liveness.GONE:
        return {"state": "cannot-tell", "error": "tmux pane closure is unverified"}
    return {"state": "gone", "error": None}


def observe_pane_root_absence(
    ownership: dict,
    *,
    pane_probe: Callable[[str], tmux.Liveness] | None = None,
    process_reader: Callable[..., dict] | None = None,
) -> str:
    """Require both the exact pane and its recorded root birth to be absent."""
    pane_probe = tmux.pane_liveness if pane_probe is None else pane_probe
    process_reader = read_process_identity if process_reader is None else process_reader
    recorded = ownership["pane"]
    pane_state = pane_probe(recorded["pane_id"])
    observed = process_reader(recorded["root_process"]["pid"], platform=ownership["platform"])
    if pane_state is tmux.Liveness.UNKNOWN or observed["state"] == "cannot-tell":
        return "cannot-tell"
    root_alive = observed["state"] == "alive" and same_birth(
        recorded["root_process"], observed["identity"]
    )
    if pane_state is tmux.Liveness.GONE and not root_alive:
        return "gone"
    return "alive"


def observe_bounded_processes(
    owned: list[dict],
    *,
    platform: str,
    process_table: dict,
) -> dict:
    """Observe a bounded identity set and add children of still-owned processes."""
    if process_table["state"] != "complete":
        return {
            "state": "cannot-tell",
            "count": None,
            "identities": list(owned),
            "error": process_table.get("error"),
        }
    current = {identity["pid"]: identity for identity in process_table["identities"]}
    retained = []
    for identity in owned:
        if identity["birth"]["kind"] == "unavailable":
            return {
                "state": "cannot-tell",
                "count": None,
                "identities": list(owned),
                "error": "owned process birth identity is unavailable",
            }
        candidate = current.get(identity["pid"])
        if candidate is not None and same_birth(identity, candidate):
            retained.append(candidate)
    retained_by_pid = {identity["pid"]: identity for identity in retained}
    changed = True
    while changed:
        changed = False
        for identity in process_table["identities"]:
            if identity["pid"] not in retained_by_pid and identity["ppid"] in retained_by_pid:
                retained_by_pid[identity["pid"]] = identity
                changed = True
    identities = sorted(retained_by_pid.values(), key=lambda item: item["pid"])
    return {
        "state": "empty" if not identities else "populated",
        "count": len(identities),
        "identities": identities,
        "error": None,
        "platform": platform_name(platform),
    }


def _containment_copy(record: dict, now_ns: Callable[[], int]) -> dict:
    containment = dict(record["containment"])
    if containment["started_monotonic_ns"] is None:
        containment["started_monotonic_ns"] = now_ns()
    if containment["deadline_monotonic_ns"] is None:
        containment["deadline_monotonic_ns"] = containment["started_monotonic_ns"] + int(
            CONTAINMENT_TIMEOUT_SEC * 1_000_000_000
        )
    containment["poll_interval_ms"] = int(CONTAINMENT_POLL_SEC * 1000)
    if containment["state"] in {"not_started", "pane_close_pending"}:
        containment["state"] = "grace"
    return containment


def start_containment(
    record: dict,
    *,
    now_ns: Callable[[], int] = time.monotonic_ns,
) -> dict:
    """Return the deadline-bearing grace state to persist before polling."""
    return _containment_copy(record, now_ns)


def _blocked(containment: dict, error: str) -> dict:
    containment["state"] = "blocked"
    containment["last_error"] = error
    containment["result"] = None
    containment["proof_label"] = None
    return containment


def _proven(containment: dict, result: str, label: str) -> dict:
    containment["state"] = "proven"
    containment["result"] = result
    containment["proof_label"] = label
    containment["last_error"] = None
    return containment


def _call_observer(handles: dict, name: str):
    try:
        return handles[name]()
    except Exception:
        return "cannot-tell"


def _call_action(handles: dict, name: str) -> bool:
    try:
        return bool(handles[name]())
    except Exception:
        return False


def _poll(now_ns: Callable[[], int], poll: Callable[[float], None], deadline: int) -> None:
    remaining = max(0.0, (deadline - now_ns()) / 1_000_000_000)
    poll(min(CONTAINMENT_POLL_SEC, remaining))


def _observe_strict(
    record: dict,
    containment: dict,
    handles: dict,
    *,
    now_ns: Callable[[], int],
    poll: Callable[[float], None],
) -> dict:
    deadline = containment["deadline_monotonic_ns"]
    kill_at = deadline - int(CONTAINMENT_POLL_SEC * 1_000_000_000)
    killed = containment["state"] in {"kill_pending", "verify_after_kill"}
    if containment["state"] == "kill_pending":
        if containment["last_cgroup_observation"] == "populated" and not _call_action(
            handles, "kill_cgroup"
        ):
            return _blocked(containment, "exact cgroup kill failed")
        if containment["last_supervisor_observation"] == "alive" and not _call_action(
            handles, "kill_supervisor"
        ):
            return _blocked(containment, "verified supervisor kill failed")
        containment["state"] = "verify_after_kill"

    while True:
        cgroup = _call_observer(handles, "observe_cgroup")
        supervisor = _call_observer(handles, "observe_supervisor")
        pane_root = _call_observer(handles, "observe_pane_root")
        containment["last_cgroup_observation"] = cgroup
        containment["last_supervisor_observation"] = supervisor
        if "cannot-tell" in {cgroup, supervisor, pane_root}:
            return _blocked(containment, "strict Linux containment is ambiguous")
        if cgroup == "empty" and supervisor == "gone" and pane_root == "gone":
            result = "linux-strict-killed-empty" if killed else "linux-strict-empty"
            return _proven(containment, result, "strict Linux containment proven")
        if record.get("action_type") == "kill" and containment["state"] == "grace":
            containment["state"] = "kill_pending"
            return containment
        current = now_ns()
        if current >= deadline:
            return _blocked(containment, "strict Linux containment deadline expired")
        if containment["state"] == "grace" and current >= kill_at:
            containment["state"] = "kill_pending"
            return containment
        _poll(now_ns, poll, deadline)


def _observe_degraded(
    mode: str,
    containment: dict,
    handles: dict,
    *,
    now_ns: Callable[[], int],
    poll: Callable[[float], None],
) -> dict:
    deadline = containment["deadline_monotonic_ns"]
    result_and_label = {
        "linux-degraded": (
            "linux-degraded-bounded-empty",
            "bounded Linux containment observed; leak-free cleanup unproven",
        ),
        "darwin-bounded": (
            "darwin-bounded-empty",
            "bounded Darwin teardown observed; leak-free cleanup unproven",
        ),
        "other-bounded-no-birth": (
            "other-bounded-empty-no-birth",
            "degraded teardown observed; birth identity and leak-free cleanup unproven",
        ),
    }[mode]
    while True:
        bounded = _call_observer(handles, "observe_bounded")
        pane = _call_observer(handles, "observe_pane")
        if not isinstance(bounded, dict):
            return _blocked(containment, "bounded process observation is ambiguous")
        containment["last_owned_process_count"] = bounded.get("count")
        if bounded.get("state") == "cannot-tell" or pane == "cannot-tell":
            return _blocked(containment, "bounded process observation is ambiguous")
        if bounded.get("state") == "empty" and pane == "gone":
            return _proven(containment, *result_and_label)
        if now_ns() >= deadline:
            return _blocked(containment, "bounded containment deadline expired")
        _poll(now_ns, poll, deadline)


def observe_containment(
    record: dict,
    handles: dict,
    *,
    now_ns: Callable[[], int] = time.monotonic_ns,
    poll: Callable[[float], None] = time.sleep,
) -> dict:
    """Run one persisted containment state to its next durable transition."""
    containment = start_containment(record, now_ns=now_ns)
    if containment["state"] in {"proven", "blocked"}:
        return containment
    mode = record["ownership"]["proof_mode"]
    if mode == "linux-strict":
        return _observe_strict(
            record,
            containment,
            handles,
            now_ns=now_ns,
            poll=poll,
        )
    return _observe_degraded(mode, containment, handles, now_ns=now_ns, poll=poll)

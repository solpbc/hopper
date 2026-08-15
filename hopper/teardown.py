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

from hopper import actions, oom, tmux

CONTAINMENT_TIMEOUT_SEC = 30.0
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
    # Linux exposes kernel threads with a zero process-group ID in /proc.  They
    # are valid observed table rows even though Hopper-owned launch identities
    # must still report a positive process group at their validation boundary.
    if pid < 1 or ppid < 0 or pgid < 0 or starttime < 0 or not boot_id:
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
    errors = []
    try:
        entries = list(proc_root.iterdir())
    except OSError as error:
        return {"state": "unknown", "identities": [], "error": str(error)}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        observed = read_linux_process_identity(
            int(entry.name), proc_root=proc_root, boot_id=boot_id
        )
        if observed["state"] == "gone":
            continue
        if observed["state"] != "alive":
            errors.append(f"PID {entry.name}: {observed['error'] or 'cannot tell'}")
            continue
        identities.append(observed["identity"])
    if not identities:
        return {
            "state": "partial" if errors else "unknown",
            "identities": [],
            "error": "; ".join(errors) or "process table returned no rows",
        }
    return {
        "state": "partial" if errors else "complete",
        "identities": sorted(identities, key=lambda item: item["pid"]),
        "error": "; ".join(errors) or None,
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
        return {"state": "unknown", "identities": [], "error": str(error)}
    if result.returncode != 0:
        return {
            "state": "unknown",
            "identities": [],
            "error": result.stderr.strip() or f"ps exited {result.returncode}",
        }
    identities = []
    errors = []
    for row in result.stdout.splitlines():
        if not row.strip():
            continue
        try:
            identities.append(parse_ps_process_row(row))
        except ValueError as error:
            errors.append(str(error))
    unique = {}
    for identity in identities:
        pid = identity["pid"]
        if pid in unique:
            errors.append(f"duplicate ps PID {pid}")
            continue
        unique[pid] = identity
    identities = list(unique.values())
    if not identities:
        return {
            "state": "partial" if errors else "unknown",
            "identities": [],
            "error": "; ".join(errors) or "ps returned no rows",
        }
    return {
        "state": "partial" if errors else "complete",
        "identities": sorted(identities, key=lambda item: item["pid"]),
        "error": "; ".join(errors) or None,
    }


def read_process_table(
    *,
    platform: str | None = None,
    proc_root: Path = Path("/proc"),
    boot_id: str | None = None,
    run: Callable | None = None,
    timeout: float = PROCESS_QUERY_TIMEOUT_SEC,
) -> dict:
    """Read a complete, partial, or unknown process-table observation."""
    if platform_name(platform) == "linux":
        boot_id = boot_id or read_boot_id(proc_root=proc_root)
        if boot_id is None:
            return {"state": "unknown", "identities": [], "error": "boot ID unavailable"}
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


def capture_worker_cgroup_membership(
    worker: dict,
    control_group: str,
    *,
    proc_root: Path = Path("/proc"),
) -> dict:
    """Prove that one birth-identified Linux worker occupies a control group."""
    pid = worker.get("pid") if isinstance(worker, dict) else None
    birth = worker.get("birth") if isinstance(worker, dict) else None
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid < 1
        or not isinstance(birth, dict)
        or birth.get("kind") != "linux-proc-starttime"
        or not isinstance(birth.get("boot_id"), str)
        or not birth["boot_id"]
        or not isinstance(birth.get("value"), str)
        or not birth["value"]
    ):
        return {
            "state": "cannot-tell",
            "control_group": None,
            "error": "worker does not have a Linux proc-stat birth identity",
        }

    try:
        text = oom._read_text(proc_root / str(pid) / "cgroup")
    except FileNotFoundError:
        return {
            "state": "cannot-tell",
            "control_group": None,
            "error": "worker cgroup membership file is missing",
        }
    except ProcessLookupError:
        return {
            "state": "cannot-tell",
            "control_group": None,
            "error": "worker exited before cgroup membership could be read",
        }
    except OSError as error:
        return {
            "state": "cannot-tell",
            "control_group": None,
            "error": f"worker cgroup membership is unreadable: {error}",
        }

    memberships = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(":", 2)
        if len(fields) != 3:
            return {
                "state": "cannot-tell",
                "control_group": None,
                "error": "worker cgroup membership is malformed",
            }
        if fields[0] == "0" and fields[1] == "":
            memberships.append(fields[2])

    if not memberships:
        return {
            "state": "cannot-tell",
            "control_group": None,
            "error": "worker has no cgroup-v2 membership",
        }
    if len(memberships) != 1:
        return {
            "state": "cannot-tell",
            "control_group": None,
            "error": "worker has ambiguous cgroup-v2 membership",
        }

    observed_path = PurePosixPath(memberships[0])
    if (
        not memberships[0]
        or not observed_path.is_absolute()
        or observed_path == PurePosixPath("/")
        or ".." in observed_path.parts
    ):
        return {
            "state": "cannot-tell",
            "control_group": None,
            "error": f"worker cgroup-v2 membership path is invalid: {memberships[0]!r}",
        }
    observed_group = str(observed_path)
    expected_path = PurePosixPath(control_group)
    if observed_path.parts != expected_path.parts:
        return {
            "state": "cannot-tell",
            "control_group": observed_group,
            "error": (
                f"worker cgroup membership {observed_group} does not match "
                f"unit control group {expected_path}"
            ),
        }

    current = read_linux_process_identity(
        pid,
        proc_root=proc_root,
        boot_id=birth["boot_id"],
    )
    if current["state"] != "alive":
        return {
            "state": "cannot-tell",
            "control_group": observed_group,
            "error": (
                "worker identity became unavailable while membership was observed: "
                f"{current['error'] or current['state']}"
            ),
        }
    if not same_birth(worker, current["identity"]):
        return {
            "state": "cannot-tell",
            "control_group": observed_group,
            "error": "worker identity changed while membership was observed",
        }
    return {"state": "proven", "control_group": observed_group, "error": None}


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

    discovered = _discover_owned_descendants(
        roots,
        process_group,
        table["identities"],
        process_reader=read_process_identity,
        identity_kwargs={"platform": platform, **identity_kwargs},
    )
    if discovered["state"] == "ambiguous":
        return {
            "state": "cannot-tell",
            "ownership": None,
            "error": "descendant identity became ambiguous during capture",
        }
    descendants = discovered["descendants"]

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


def _discover_owned_descendants(
    roots: list[dict],
    process_group: int,
    process_table: list[dict],
    *,
    process_reader: Callable[..., dict],
    identity_kwargs: dict,
) -> dict:
    """Discover and birth-verify the owned descendants of recorded roots."""
    # The bounded platforms cannot rely on a cgroup after pane closure.  Seed
    # their retained set with every member of the supervisor's original
    # process group as well as transitive descendants.  Retention is by birth
    # identity from this point onward; the numeric PGID is never a kill target.
    descendants_by_pid = {
        identity["pid"]: identity
        for identity in process_table
        if identity["pgid"] == process_group
        and identity["pid"] not in {root["pid"] for root in roots}
    }
    descendants_by_pid.update(
        {identity["pid"]: identity for identity in capture_descendants(roots, process_table)}
    )
    descendants = sorted(descendants_by_pid.values(), key=lambda identity: identity["pid"])
    verified_descendants = []
    for descendant in descendants:
        observed = process_reader(descendant["pid"], **identity_kwargs)
        if observed["state"] == "gone":
            continue
        if observed["state"] != "alive":
            return {"state": "ambiguous", "descendants": None}
        if same_birth(descendant, observed["identity"]):
            verified_descendants.append(observed["identity"])
    return {"state": "discovered", "descendants": verified_descendants}


def _merge_discovered_descendants(ownership: dict, discovered: list[dict]) -> list[dict]:
    """Union discovery into recorded descendants without accepting PID reuse."""
    root_pids = {
        ownership["pane"]["root_process"]["pid"],
        ownership["supervisor"]["pid"],
        ownership["worker"]["pid"],
    }
    merged = {
        identity["pid"]: identity
        for identity in ownership["descendants"]
        if identity["pid"] not in root_pids
    }
    for identity in discovered:
        pid = identity["pid"]
        if pid in root_pids:
            continue
        recorded = merged.get(pid)
        if recorded is None or same_birth(recorded, identity):
            merged[pid] = identity
    return [merged[pid] for pid in sorted(merged)]


def discover_owned_set(
    ownership: dict,
    *,
    boot_id_reader: Callable | None = None,
    process_table_reader: Callable | None = None,
    process_reader: Callable | None = None,
    proc_root: Path = Path("/proc"),
    run: Callable | None = None,
) -> dict:
    """Discover descendants from recorded ownership immediately before pane close."""
    platform = platform_name(ownership["platform"])
    boot_id_reader = read_boot_id if boot_id_reader is None else boot_id_reader
    process_table_reader = (
        read_process_table if process_table_reader is None else process_table_reader
    )
    process_reader = read_process_identity if process_reader is None else process_reader
    run = subprocess.run if run is None else run
    boot_id = boot_id_reader(proc_root=proc_root) if platform == "linux" else None
    if platform == "linux" and boot_id is None:
        return {"state": "cannot-tell", "descendants": None, "error": "boot ID unavailable"}
    if platform == "linux":
        recorded_boot_id = ownership["pane"]["root_process"]["birth"]["boot_id"]
        # This is a host-level boot consistency check, not the deleted root-liveness gate;
        # it does not probe whether any recorded root is alive.
        if boot_id != recorded_boot_id:
            return {
                "state": "cannot-tell",
                "descendants": None,
                "error": (
                    f"Linux boot identity mismatch: recorded {recorded_boot_id}, current {boot_id}"
                ),
            }
    identity_kwargs = (
        {"platform": platform, "proc_root": proc_root, "boot_id": boot_id}
        if platform == "linux"
        else {"platform": platform, "run": run}
    )
    table = process_table_reader(
        platform=platform,
        proc_root=proc_root,
        boot_id=boot_id,
        run=run,
    )
    if table["state"] != "complete":
        # This lode moves an existing capture-phase wedge to pane close rather than creating one;
        # cto-70 carries the fix for the underlying persistently rejecting process table.
        return {
            "state": "cannot-tell",
            "descendants": None,
            "error": table.get("error") or "process table is incomplete",
        }
    roots = [
        ownership["pane"]["root_process"],
        ownership["supervisor"],
        ownership["worker"],
    ]
    discovered = _discover_owned_descendants(
        roots,
        ownership["process_group"],
        table["identities"],
        process_reader=process_reader,
        identity_kwargs=identity_kwargs,
    )
    if discovered["state"] == "ambiguous":
        return {
            "state": "cannot-tell",
            "descendants": None,
            "error": "descendant identity became ambiguous during owned-set discovery",
        }
    return {
        "state": "discovered",
        "descendants": _merge_discovered_descendants(ownership, discovered["descendants"]),
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


def _cgroup_identity_matches(fd: int, cgroup: dict) -> bool:
    stat = os.fstat(fd)
    expected = cgroup["identity"]
    return (stat.st_dev, stat.st_ino) == (expected["st_dev"], expected["st_ino"])


def observe_retained_cgroup(
    fd: int,
    cgroup: dict,
    *,
    boot_id: str,
) -> str:
    """Read recursive population through an identity-verified retained directory."""
    if boot_id != cgroup["boot_id"]:
        return "cannot-tell"
    try:
        if not _cgroup_identity_matches(fd, cgroup):
            return "cannot-tell"
        path = Path(f"/proc/self/fd/{fd}") / "cgroup.events"
        try:
            text = oom._read_text(path)
        except OSError as error:
            if error.errno not in {errno.ENOENT, errno.ENODEV} or not _cgroup_identity_matches(
                fd, cgroup
            ):
                return "cannot-tell"
            return "empty"
        if not _cgroup_identity_matches(fd, cgroup):
            return "cannot-tell"
    except OSError:
        return "cannot-tell"
    return _parse_cgroup_events(text)


def _force_result(state: str, error: str | None = None) -> dict:
    return {"state": state, "error": error}


def kill_cgroup(cgroup: dict, *, boot_id: str | None = None) -> dict:
    """Write cgroup.kill only through a verified exact cgroup directory."""
    fd, open_error = _opened_cgroup(cgroup)
    if fd is None:
        if open_error == "absent":
            return _force_result("already-gone")
        return _force_result("unaddressable", open_error)
    try:
        current_boot = boot_id or read_boot_id()
        if current_boot is None or current_boot != cgroup["boot_id"]:
            return _force_result("unaddressable", "cgroup boot identity mismatch")
        try:
            oom._write_text(Path(f"/proc/self/fd/{fd}") / "cgroup.kill", "1")
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ENODEV}:
                try:
                    if _cgroup_identity_matches(fd, cgroup):
                        return _force_result("already-gone")
                except OSError:
                    pass
            return _force_result("unaddressable", str(error))
        except ValueError as error:
            return _force_result("unaddressable", str(error))
        return _force_result("signalled")
    finally:
        os.close(fd)


def reopen_process_pidfd(
    identity: dict,
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
        fd = interface["open"](identity["pid"], 0)
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
    if current_boot != identity["birth"]["boot_id"]:
        os.close(fd)
        return {"state": "gone", "fd": None, "error": None}
    observed = read_linux_process_identity(
        identity["pid"],
        proc_root=proc_root,
        boot_id=current_boot,
    )
    if observed["state"] == "gone":
        os.close(fd)
        return {"state": "gone", "fd": None, "error": None}
    if observed["state"] != "alive":
        os.close(fd)
        return {"state": "cannot-tell", "fd": None, "error": observed["error"]}
    if not same_birth(identity, observed["identity"]):
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


def signal_process_pidfd(
    fd: int,
    identity: dict,
    *,
    proc_root: Path = Path("/proc"),
    pidfd_interface: dict | None = None,
) -> dict:
    """SIGKILL an identity-verified process descriptor, never a numeric PID."""
    interface = resolve_pidfd_interface() if pidfd_interface is None else pidfd_interface
    if interface is None:
        return _force_result("unaddressable", "pidfd_send_signal is unavailable")
    current_boot = read_boot_id(proc_root=proc_root)
    if current_boot is None:
        return _force_result("unaddressable", "Linux boot identity unavailable")
    if current_boot != identity["birth"]["boot_id"]:
        return _force_result("already-gone")
    observed = read_linux_process_identity(
        identity["pid"], proc_root=proc_root, boot_id=current_boot
    )
    if observed["state"] == "gone":
        return _force_result("already-gone")
    if observed["state"] != "alive":
        return _force_result("unaddressable", observed["error"])
    if not same_birth(identity, observed["identity"]):
        return _force_result("already-gone")
    try:
        interface["send_signal"](fd, signal.SIGKILL, None, 0)
    except ProcessLookupError:
        return _force_result("already-gone")
    except (AttributeError, OSError) as error:
        return _force_result("unaddressable", str(error))
    return _force_result("signalled")


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


def observe_bounded_processes(
    owned: list[dict],
    *,
    platform: str,
    process_table: dict,
    process_reader: Callable[..., dict] | None = None,
) -> dict:
    """Resolve owned identities against a complete, partial, or unknown table."""
    resolution = process_table["state"]
    if resolution not in {"complete", "partial", "unknown"}:
        raise ValueError(f"invalid process table resolution: {resolution}")
    process_reader = read_process_identity if process_reader is None else process_reader
    if any(identity["birth"]["kind"] == "unavailable" for identity in owned):
        return {
            "state": "cannot-tell",
            "count": None,
            "identities": list(owned),
            "error": "owned process birth identity is unavailable",
            "resolution": resolution,
            "platform": platform_name(platform),
            "replaced_pids": [],
        }

    current = {identity["pid"]: identity for identity in process_table["identities"]}
    retained_by_pid = {}
    replaced_pids = set()
    present_pids = set()
    unresolved_pids = set()
    for identity in owned:
        candidate = current.get(identity["pid"])
        if candidate is not None and same_birth(identity, candidate):
            retained_by_pid[identity["pid"]] = candidate
            present_pids.add(identity["pid"])
            continue
        if candidate is not None:
            replaced_pids.add(identity["pid"])
            continue
        if resolution == "complete":
            continue
        if resolution == "unknown":
            retained_by_pid[identity["pid"]] = identity
            unresolved_pids.add(identity["pid"])
            continue
        observed = process_reader(identity["pid"], platform=platform)
        if observed["state"] == "alive":
            if same_birth(identity, observed["identity"]):
                retained_by_pid[identity["pid"]] = observed["identity"]
                present_pids.add(identity["pid"])
            else:
                replaced_pids.add(identity["pid"])
            continue
        if observed["state"] == "gone" and platform_name(platform) == "linux":
            continue
        retained_by_pid[identity["pid"]] = identity
        unresolved_pids.add(identity["pid"])

    changed = True
    while changed:
        changed = False
        for identity in process_table["identities"]:
            if identity["pid"] in retained_by_pid or identity["pid"] in replaced_pids:
                continue
            if identity["ppid"] in present_pids:
                retained_by_pid[identity["pid"]] = identity
                present_pids.add(identity["pid"])
                changed = True
    identities = sorted(retained_by_pid.values(), key=lambda item: item["pid"])
    if present_pids:
        state = "populated"
        count = len(identities)
    elif unresolved_pids or resolution == "unknown":
        state = "cannot-tell"
        count = None
    else:
        state = "empty"
        count = 0
    return {
        "state": state,
        "count": count,
        "identities": identities,
        "error": process_table.get("error") if state == "cannot-tell" else None,
        "resolution": resolution,
        "platform": platform_name(platform),
        "replaced_pids": sorted(replaced_pids),
    }


def arm_phase(containment: dict, state: str, *, now_ns: Callable[[], int]) -> dict:
    """Arm a fresh budget for exactly one containment phase."""
    armed = dict(containment)
    started = now_ns()
    armed["state"] = state
    armed["started_monotonic_ns"] = started
    armed["deadline_monotonic_ns"] = started + int(CONTAINMENT_TIMEOUT_SEC * 1_000_000_000)
    armed["poll_interval_ms"] = actions.POLL_INTERVAL_MS
    armed["result"] = None
    armed["proof_label"] = None
    armed["last_error"] = None
    return armed


def continue_phase(containment: dict) -> dict:
    """Continue an already armed phase without extending its budget."""
    continued = dict(containment)
    if continued["started_monotonic_ns"] is None or continued["deadline_monotonic_ns"] is None:
        raise ValueError(f"containment phase {continued['state']} has no armed budget")
    continued["poll_interval_ms"] = actions.POLL_INTERVAL_MS
    return continued


def normalize_legacy_blocked_containment(
    record: dict,
    *,
    now_ns: Callable[[], int],
) -> dict:
    """Migrate the sole legacy blocked-state encoding to a phase cursor."""
    containment = dict(record["containment"])
    if containment["state"] != "blocked":
        return containment
    state = "kill_pending" if record["ownership"]["proof_mode"] == "linux-strict" else "grace"
    return arm_phase(containment, state, now_ns=now_ns)


def start_containment(
    record: dict,
    *,
    now_ns: Callable[[], int] = time.monotonic_ns,
) -> dict:
    """Return the phase state that must be persisted before polling."""
    containment = normalize_legacy_blocked_containment(record, now_ns=now_ns)
    if containment["state"] in {"not_started", "pane_close_pending"}:
        return arm_phase(containment, "grace", now_ns=now_ns)
    if containment["state"] == "proven":
        return containment
    return continue_phase(containment)


def _blocked(containment: dict, error: str) -> dict:
    containment["last_error"] = error
    return containment


def _proven(containment: dict, result: str, label: str) -> dict:
    containment["state"] = "proven"
    containment["result"] = result
    containment["proof_label"] = label
    containment["last_error"] = None
    return containment


def _call_observer(handles: dict, name: str) -> dict:
    try:
        return {"kind": "value", "value": handles[name]()}
    except Exception as error:
        return {
            "kind": "programming-error",
            "error": _programming_error("observer", name, error),
        }


def _call_action(handles: dict, name: str) -> dict:
    try:
        outcome = handles[name]()
        if not isinstance(outcome, dict) or outcome.get("state") not in {
            "signalled",
            "already-gone",
            "unaddressable",
        }:
            raise ValueError(f"invalid force outcome: {outcome!r}")
        return {"kind": "value", "value": outcome}
    except Exception as error:
        return {
            "kind": "programming-error",
            "error": _programming_error("force action", name, error),
        }


def _programming_error(role: str, name: str, error: Exception) -> str:
    detail = str(error)
    suffix = f": {detail}" if detail else ""
    return f"{role} {name} raised {type(error).__name__}{suffix}"


def _scalar_observations(handles: dict, names: tuple[str, ...]) -> tuple[dict, list[str]]:
    values = {}
    errors = []
    for name in names:
        observed = _call_observer(handles, name)
        if observed["kind"] == "programming-error":
            errors.append(observed["error"])
        else:
            values[name] = observed["value"]
    return values, errors


def _poll(now_ns: Callable[[], int], poll: Callable[[float], None], deadline: int) -> None:
    remaining = max(0.0, (deadline - now_ns()) / 1_000_000_000)
    poll(min(actions.POLL_INTERVAL_MS / 1000, remaining))


def _observe_strict(
    record: dict,
    containment: dict,
    handles: dict,
    *,
    now_ns: Callable[[], int],
    poll: Callable[[float], None],
) -> dict:
    deadline = containment["deadline_monotonic_ns"]
    force_attempted = containment["state"] == "verify_after_kill"
    force_errors = []
    while True:
        observations, programming_errors = _scalar_observations(
            handles,
            ("observe_cgroup", "observe_supervisor", "observe_pane_root"),
        )
        expected = {
            "observe_cgroup": {"empty", "populated", "cannot-tell"},
            "observe_supervisor": {"gone", "alive", "cannot-tell"},
            "observe_pane_root": {"gone", "alive", "cannot-tell"},
        }
        for name, value in observations.items():
            if value not in expected[name]:
                programming_errors.append(
                    _programming_error(
                        "observer", name, ValueError(f"invalid observation: {value!r}")
                    )
                )
        cgroup = observations.get("observe_cgroup")
        supervisor = observations.get("observe_supervisor")
        pane_root = observations.get("observe_pane_root")
        if cgroup in expected["observe_cgroup"]:
            containment["last_cgroup_observation"] = cgroup
        if supervisor in expected["observe_supervisor"]:
            containment["last_supervisor_observation"] = supervisor
        if cgroup == "empty" and supervisor == "gone" and pane_root == "gone":
            killed = containment["state"] == "verify_after_kill"
            result = "linux-strict-killed-empty" if killed else "linux-strict-empty"
            return _proven(containment, result, "strict Linux containment proven")

        current = now_ns()

        if containment["state"] == "grace":
            # Every teardown gets the grace it was armed with, including an explicit
            # kill. The pane's PTY is already closed by this point, so grace is not a
            # sleep — it polls until the cgroup empties and escalates the moment it
            # does not. Honouring it costs nothing when the runner exits cleanly, and
            # skipping it means SIGKILL reaches a process that never got to release
            # anything it held.
            if current >= deadline:
                return arm_phase(containment, "kill_pending", now_ns=now_ns)
            if programming_errors:
                return _blocked(containment, programming_errors[0])
            _poll(now_ns, poll, deadline)
            continue

        if programming_errors:
            return _blocked(containment, programming_errors[0])

        ambiguous = "cannot-tell" in {cgroup, supervisor, pane_root}
        if containment["state"] == "kill_pending" and not force_attempted:
            force_attempted = True
            signalled = False
            action_errors = []
            targets = []
            if cgroup == "populated":
                targets.append("kill_cgroup")
            if supervisor == "alive":
                targets.append("kill_supervisor")
            if pane_root == "alive":
                targets.append("kill_pane_root")
            for name in targets:
                action = _call_action(handles, name)
                if action["kind"] == "programming-error":
                    action_errors.append(action["error"])
                    continue
                outcome = action["value"]
                if outcome["state"] == "signalled":
                    signalled = True
                elif outcome["state"] == "unaddressable":
                    force_errors.append(f"{name}: {outcome.get('error') or 'unaddressable'}")
            if signalled:
                containment["state"] = "verify_after_kill"
            if action_errors:
                return _blocked(containment, action_errors[0])
            continue

        if current >= deadline:
            if ambiguous:
                return _blocked(
                    containment,
                    "strict Linux force verification remained ambiguous until budget expiry",
                )
            detail = f" after {'; '.join(force_errors)}" if force_errors else ""
            return _blocked(
                containment,
                f"strict Linux force verification budget expired{detail}",
            )
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
        observed, programming_errors = _scalar_observations(
            handles, ("observe_bounded", "observe_pane")
        )
        bounded = observed.get("observe_bounded")
        pane = observed.get("observe_pane")
        if bounded is not None and (
            not isinstance(bounded, dict)
            or bounded.get("state") not in {"empty", "populated", "cannot-tell"}
        ):
            programming_errors.append(
                _programming_error(
                    "observer",
                    "observe_bounded",
                    ValueError(f"invalid observation: {bounded!r}"),
                )
            )
            bounded = None
        if pane is not None and pane not in {"gone", "alive", "cannot-tell"}:
            programming_errors.append(
                _programming_error(
                    "observer", "observe_pane", ValueError(f"invalid observation: {pane!r}")
                )
            )
            pane = None
        if isinstance(bounded, dict):
            containment["last_owned_process_count"] = bounded.get("count")
        if isinstance(bounded, dict) and bounded.get("state") == "empty" and pane == "gone":
            return _proven(containment, *result_and_label)
        if programming_errors:
            return _blocked(containment, programming_errors[0])
        ambiguous = (
            not isinstance(bounded, dict)
            or bounded.get("state") == "cannot-tell"
            or pane == "cannot-tell"
        )
        if now_ns() >= deadline:
            error = (
                "bounded containment remained ambiguous until budget expiry"
                if ambiguous
                else "bounded containment budget expired"
            )
            return _blocked(containment, error)
        _poll(now_ns, poll, deadline)


def observe_containment(
    record: dict,
    handles: dict,
    *,
    host_boot_identity: str | None,
    now_ns: Callable[[], int] = time.monotonic_ns,
    poll: Callable[[float], None] = time.sleep,
) -> dict:
    """Run one persisted containment state to its next durable transition."""
    if record["containment"]["state"] == "proven":
        return dict(record["containment"])
    original_state = record["containment"]["state"]
    if original_state == "blocked":
        containment = normalize_legacy_blocked_containment(record, now_ns=now_ns)
    elif original_state in {"not_started", "pane_close_pending"}:
        containment = arm_phase(record["containment"], "grace", now_ns=now_ns)
    elif host_boot_identity != record["boot_id"]:
        containment = arm_phase(record["containment"], original_state, now_ns=now_ns)
    else:
        try:
            containment = continue_phase(record["containment"])
        except ValueError as error:
            return _blocked(dict(record["containment"]), str(error))
    if containment["state"] == "proven":
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

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Acceptance tests for captured pending-action containment records."""

import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hopper import actions, teardown
from hopper.lodes import lode_stage_session, project_lode_claude_state
from hopper.projects import Project
from hopper.server import Server
from hopper.tmux import Liveness

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pending-actions"


def _load_captured_record(tmp_path: Path, fixture_name: str) -> tuple[dict, bytes]:
    """Copy captured bytes across the real pending-action load boundary."""
    fixture_bytes = (FIXTURE_DIR / fixture_name).read_bytes()
    lode_id = json.loads(fixture_bytes)["lode_id"]
    target = tmp_path / "lodes" / lode_id / "pending-completion.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(fixture_bytes)
    return actions.load_pending_action(lode_id), fixture_bytes


def _join_action_step(server: Server, record: dict, phase: str) -> dict:
    thread = server.action_threads[(record["action_id"], phase)]
    thread.join(timeout=2)
    assert not thread.is_alive()
    internal, _conn = server.event_queue.get(timeout=2)
    assert internal["type"] == "_action_step_result"
    assert internal["action_id"] == record["action_id"]
    assert internal["phase"] == phase
    return internal


def _root_free_process_table(ownership: dict, identities: list[dict]) -> dict:
    root_pids = {
        ownership["pane"]["root_process"]["pid"],
        ownership["supervisor"]["pid"],
        ownership["worker"]["pid"],
    }
    assert root_pids.isdisjoint(identity["pid"] for identity in identities)
    return {"state": "complete", "identities": identities, "error": None}


def _reset_for_pane_close_retry(record: dict) -> dict:
    """Build and validate a vendored-record variant at the pane-close retry boundary."""
    if record["markers"]["ownership_capture"]["state"] != "done":
        record["markers"]["ownership_capture"] = actions.new_marker()
        actions.transition_marker(record, "ownership_capture", "intent")
        actions.transition_marker(
            record,
            "ownership_capture",
            "done",
            attempt_id=record["markers"]["ownership_capture"]["attempt_id"],
        )
    record["ownership"].update(captured=True, captured_at_ms=1_001)
    record["markers"]["pane_close"] = actions.new_marker()
    actions.transition_marker(record, "pane_close", "intent")
    actions.transition_marker(
        record,
        "pane_close",
        "blocked",
        attempt_id=record["markers"]["pane_close"]["attempt_id"],
        detail="pane-close discovery interrupted",
    )
    for marker_name in ("containment", "scope_kill", "supervisor_kill"):
        record["markers"][marker_name] = actions.new_marker()
    record["containment"].update(
        state="pane_close_pending",
        started_monotonic_ns=None,
        deadline_monotonic_ns=None,
        last_cgroup_observation=None,
        last_supervisor_observation=None,
        last_owned_process_count=None,
        result=None,
        proof_label=None,
        last_error=None,
    )
    record["phase"] = "containment_blocked"
    record["recovery"] = {
        "kind": "ownership",
        "message": "pane-close discovery interrupted",
        "command": actions.recovery_command(record, "ownership"),
    }
    actions.write_pending_action(record)
    return actions.load_pending_action(record["lode_id"])


def _clock(start: int = 1_000_000_000):
    state = {"now": start}

    def now_ns() -> int:
        return state["now"]

    def poll(seconds: float) -> None:
        state["now"] += int(seconds * 1_000_000_000)

    return state, now_ns, poll


def _drive_strict_record(
    record: dict,
    *,
    cgroup_observations: list[str],
    force_outcome: str = "signalled",
) -> tuple[dict, list[str]]:
    """Drive a byte-loaded strict record with an explicit host observation stream."""
    _state, now_ns, poll = _clock()
    observations = iter(cgroup_observations)
    last_observation = cgroup_observations[-1]
    signals = []

    def observe_cgroup() -> str:
        nonlocal last_observation
        last_observation = next(observations, last_observation)
        return last_observation

    def kill_cgroup() -> dict:
        signals.append("cgroup")
        return {"state": force_outcome, "error": None}

    identity = (record["lode_id"], record["action_id"], record["expected_generation"])
    containment = teardown.observe_containment(
        record,
        {
            "observe_cgroup": observe_cgroup,
            "observe_supervisor": lambda: "gone",
            "observe_pane_root": lambda: "gone",
            "kill_cgroup": kill_cgroup,
            "kill_supervisor": lambda: {"state": "already-gone", "error": None},
            "kill_pane_root": lambda: {"state": "already-gone", "error": None},
        },
        host_boot_identity=record["boot_id"],
        now_ns=now_ns,
        poll=poll,
    )
    record["containment"] = containment
    assert (record["lode_id"], record["action_id"], record["expected_generation"]) == identity
    return record, signals


def test_captured_populated_scope_reaps_in_verification_budget(tmp_path):
    record, _fixture_bytes = _load_captured_record(tmp_path, "wedged-populated-scope-5tuofgwh.json")

    terminal, signals = _drive_strict_record(
        record,
        cgroup_observations=["populated", "empty"],
    )

    assert signals == ["cgroup"]
    assert terminal["containment"]["state"] == "proven"
    assert terminal["containment"]["result"] == "linux-strict-killed-empty"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "wedged-drained-scope-jklqlbm7.json",
        "wedged-killfailed-absent-cgroup-x627n2uj.json",
    ],
)
def test_captured_absent_scope_proves_without_a_signal(tmp_path, fixture_name):
    record, _fixture_bytes = _load_captured_record(tmp_path, fixture_name)

    terminal, signals = _drive_strict_record(record, cgroup_observations=["empty"])

    assert signals == []
    assert terminal["containment"]["state"] == "proven"
    assert terminal["containment"]["result"] == "linux-strict-empty"


def test_captured_kill_fired_record_blocks_with_verification_cursor(tmp_path):
    record, _fixture_bytes = _load_captured_record(
        tmp_path, "wedged-killfired-verify-expired-jjihelyd.json"
    )

    terminal, signals = _drive_strict_record(record, cgroup_observations=["populated"])

    assert signals == ["cgroup"]
    assert terminal["containment"]["state"] == "verify_after_kill"
    assert terminal["containment"]["result"] is None
    assert "verification budget expired" in terminal["containment"]["last_error"]


def test_captured_restart_adopts_ownership_and_proves_without_force(tmp_path, make_lode):
    """AC9: the committed restart traverses capture, strict proof, mutation, and spawn."""
    record, fixture_bytes = _load_captured_record(
        tmp_path, "wedged-restart-ownership-blghq7to.json"
    )
    ownership = record["ownership"]
    roots_absent = _root_free_process_table(ownership, [])
    supervisor_fd, supervisor_write = os.pipe()
    pane_fd, pane_write = os.pipe()
    cgroup_fd, cgroup_write = os.pipe()
    signals = []
    reopened = []

    def reject_capture_probe(*_args, **_kwargs):
        raise AssertionError("capture phase consulted a live probe")

    def reopen_pane_root(identity, **_kwargs):
        reopened.append(identity)
        assert identity == ownership["pane"]["root_process"]
        return {"state": "alive", "fd": pane_fd, "error": None}

    def fake_spawn(snapshot, _context):
        spawn = snapshot["spawn"]
        receipt = {
            "schema_version": actions.SPAWN_RECEIPT_SCHEMA_VERSION,
            "action_id": snapshot["action_id"],
            "source_lode_id": snapshot["lode_id"],
            "target_lode_id": spawn["target_lode_id"],
            "target_generation": spawn["target_generation"],
            "pane_id": "%successor",
        }
        actions.write_spawn_receipt(receipt)
        actions.write_run_ownership(
            {
                "schema_version": 1,
                "lode_id": spawn["target_lode_id"],
                "run_generation": spawn["target_generation"],
                "registered_at_ms": 2_000,
                "boot_id": snapshot["boot_id"],
                "platform": ownership["platform"],
                "proof_mode": ownership["proof_mode"],
                "degraded_reason": None,
                "pane": {**copy.deepcopy(ownership["pane"]), "pane_id": "%successor"},
                "supervisor": copy.deepcopy(ownership["supervisor"]),
                "worker": copy.deepcopy(ownership["worker"]),
                "process_group": ownership["process_group"],
                "descendants": [],
                "unit": copy.deepcopy(ownership["unit"]),
                "cgroup": copy.deepcopy(ownership["cgroup"]),
                "unit_name": ownership["unit"]["name"],
            }
        )
        return {"ok": True, "pane_id": receipt["pane_id"], "adopted": True}

    with patch("hopper.server.get_git_hash", return_value="test-hash"):
        server = Server(tmp_path / "server.sock")
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        project="fixture-project",
        state="teardown",
        active=True,
        tmux_pane=ownership["pane"]["pane_id"],
        pid=ownership["supervisor"]["pid"],
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    lode_stage_session(lode, record["stage"])["started"] = True
    project_lode_claude_state(lode)
    server.lodes = [lode]

    try:
        with (
            patch("hopper.server.teardown.tmux.pane_identity", side_effect=reject_capture_probe),
            patch("hopper.server.teardown.read_process_identity", side_effect=reject_capture_probe),
            patch("hopper.server.teardown.read_boot_id", side_effect=reject_capture_probe),
            patch(
                "hopper.server.teardown.read_host_boot_identity",
                side_effect=reject_capture_probe,
            ),
            patch("hopper.server.teardown.read_process_table", side_effect=reject_capture_probe),
            patch("hopper.server.teardown.capture_scope_cgroup", side_effect=reject_capture_probe),
            patch("hopper.server.teardown._opened_cgroup", side_effect=reject_capture_probe),
            patch(
                "hopper.server.teardown.resolve_pidfd_interface",
                side_effect=reject_capture_probe,
            ),
            patch(
                "hopper.server.teardown.reopen_process_pidfd",
                side_effect=reject_capture_probe,
            ),
        ):
            server._retry_action(record["lode_id"], None)
            captured = _join_action_step(server, record, "capturing_ownership")

        descriptor_keys = {
            "pidfd",
            "pidfd_owned",
            "cgroup_fd",
            "cgroup_fd_owned",
            "pane_root_pidfd",
            "pane_root_pidfd_owned",
        }
        assert captured["result"]["ok"] is True
        assert descriptor_keys.isdisjoint(captured["result"])
        server.supervisor_pidfds[(record["lode_id"], record["expected_generation"])] = supervisor_fd

        with (
            patch("hopper.server.teardown.read_boot_id", return_value=record["boot_id"]),
            patch("hopper.server.teardown.read_process_table", return_value=roots_absent),
            patch(
                "hopper.server.teardown.close_owned_pane",
                return_value={"state": "gone", "error": None},
            ),
            patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            patch("hopper.server.teardown.reopen_process_pidfd", side_effect=reopen_pane_root),
            patch(
                "hopper.server.teardown._opened_cgroup", return_value=(cgroup_fd, None)
            ) as open_cgroup,
            patch("hopper.server.teardown.observe_retained_cgroup", return_value="empty"),
            patch("hopper.server.teardown.observe_pidfd", return_value="gone"),
            patch(
                "hopper.server.teardown.kill_cgroup",
                side_effect=lambda *_args, **_kwargs: signals.append("cgroup"),
            ),
            patch(
                "hopper.server.teardown.signal_process_pidfd",
                side_effect=lambda *_args, **_kwargs: signals.append("process"),
            ),
            patch("hopper.server.oom.find_systemctl", return_value="/bin/systemctl"),
            patch(
                "hopper.server.oom.read_scope_control_group",
                return_value={"state": "absent", "control_group": None},
            ),
            patch("hopper.server.oom.is_linux", return_value=False),
            patch(
                "hopper.server.find_project",
                return_value=Project(path=str(tmp_path), name="fixture-project"),
            ),
            patch.object(server, "_spawn_action_successor", side_effect=fake_spawn),
        ):
            server._handle_action_step_result(captured)
            closed = _join_action_step(server, record, "closing_pane")
            assert closed["result"] == {"ok": True, "error": None, "descendants": []}
            server._handle_action_step_result(closed)

            observed = _join_action_step(server, record, "observing_containment")
            assert observed["result"]["containment"]["result"] == "linux-strict-empty"
            assert observed["result"]["pane_root_pidfd"] == pane_fd
            assert observed["result"]["pane_root_pidfd_owned"] is True
            assert observed["result"]["cgroup_fd"] == cgroup_fd
            assert observed["result"]["cgroup_fd_owned"] is True
            assert "pidfd" not in observed["result"]
            assert "force_killing" not in {
                phase
                for action_id, phase in server.action_threads
                if action_id == record["action_id"]
            }
            server._handle_action_step_result(observed)

            spawned = _join_action_step(server, record, "spawning")
            server._handle_action_step_result(spawned)

        assert reopened == [ownership["pane"]["root_process"]]
        open_cgroup.assert_called_once_with(ownership["cgroup"])
        assert signals == []
        assert actions.load_pending_action(record["lode_id"]) is None
        assert lode["action_results"][-1]["terminal_disposition"] == "replacement_spawned"
        assert json.loads(fixture_bytes)["action_id"] == record["action_id"]
    finally:
        for fd in (
            supervisor_fd,
            pane_fd,
            cgroup_fd,
            supervisor_write,
            pane_write,
            cgroup_write,
        ):
            try:
                os.close(fd)
            except OSError:
                pass


def test_pane_close_discovery_merges_recorded_and_new_identities(tmp_path):
    """AC3: closure discovery unions identities without accepting PID reuse."""
    record, _fixture_bytes = _load_captured_record(tmp_path, "wedged-populated-scope-5tuofgwh.json")
    record = _reset_for_pane_close_retry(record)
    ownership = record["ownership"]
    recorded = copy.deepcopy(ownership["descendants"][0])
    reused = copy.deepcopy(recorded)
    reused["birth"]["value"] = f"{recorded['birth']['value']}-reused"
    new_identity = copy.deepcopy(recorded)
    new_identity.update(pid=recorded["pid"] + 10, ppid=999_999)
    new_identity["birth"]["value"] = f"{recorded['birth']['value']}-new"
    table = _root_free_process_table(ownership, [reused, new_identity])
    by_pid = {identity["pid"]: identity for identity in table["identities"]}
    server = Server(tmp_path / "server.sock")

    with (
        patch("hopper.server.teardown.read_boot_id", return_value=record["boot_id"]),
        patch("hopper.server.teardown.read_process_table", return_value=table),
        patch(
            "hopper.server.teardown.read_process_identity",
            side_effect=lambda pid, **_kwargs: {
                "state": "alive",
                "identity": by_pid[pid],
                "error": None,
            },
        ),
        patch(
            "hopper.server.teardown.close_owned_pane",
            return_value={"state": "cannot-tell", "error": "test stop"},
        ),
    ):
        server._retry_action(record["lode_id"], None)
        closed = _join_action_step(server, record, "closing_pane")

    assert closed["result"]["descendants"] == [recorded, new_identity]
    assert reused not in closed["result"]["descendants"]
    root_pids = {
        ownership["pane"]["root_process"]["pid"],
        ownership["supervisor"]["pid"],
        ownership["worker"]["pid"],
    }
    result_pids = [identity["pid"] for identity in closed["result"]["descendants"]]
    assert result_pids == sorted(set(result_pids))
    assert root_pids.isdisjoint(result_pids)
    server._handle_action_step_result(closed)
    durable = actions.load_pending_action(record["lode_id"])
    assert durable["ownership"]["descendants"] == [recorded, new_identity]


def test_pane_close_discovers_before_removing_the_process(tmp_path):
    """AC4: a process removed by pane closure is already retained in the owned set."""
    record, _fixture_bytes = _load_captured_record(
        tmp_path, "wedged-restart-ownership-blghq7to.json"
    )
    record = _reset_for_pane_close_retry(record)
    ownership = record["ownership"]
    candidate = copy.deepcopy(ownership["worker"])
    candidate.update(pid=ownership["worker"]["pid"] + 10, ppid=999_999)
    candidate["birth"]["value"] = f"{ownership['worker']['birth']['value']}-owned"
    current_processes = [candidate]
    server = Server(tmp_path / "server.sock")

    def read_table(**_kwargs):
        return _root_free_process_table(ownership, list(current_processes))

    def read_identity(pid, **_kwargs):
        if pid == candidate["pid"] and candidate in current_processes:
            identity = candidate
        else:
            assert pid == ownership["pane"]["root_process"]["pid"]
            identity = ownership["pane"]["root_process"]
        return {"state": "alive", "identity": identity, "error": None}

    def close_pane(_pane_id):
        current_processes.clear()
        return True

    pane = ownership["pane"]
    with (
        patch("hopper.server.teardown.read_boot_id", return_value=record["boot_id"]),
        patch("hopper.server.teardown.read_process_table", side_effect=read_table),
        patch("hopper.server.teardown.read_process_identity", side_effect=read_identity),
        patch(
            "hopper.server.teardown.tmux.pane_identity",
            return_value={
                "pane_id": pane["pane_id"],
                "window_id": pane["window_id"],
                "pane_pid": pane["root_process"]["pid"],
            },
        ),
        patch("hopper.server.teardown.tmux.pane_liveness", return_value=Liveness.GONE),
        patch("hopper.server.teardown.tmux.kill_pane", side_effect=close_pane),
    ):
        server._retry_action(record["lode_id"], None)
        closed = _join_action_step(server, record, "closing_pane")

    assert current_processes == []
    assert candidate in closed["result"]["descendants"]


def test_captured_restart_marks_adopted_ownership_done_together(tmp_path):
    """AC7: the blocked committed fixture adopts and durably completes capture."""
    record, _fixture_bytes = _load_captured_record(
        tmp_path, "wedged-restart-ownership-blghq7to.json"
    )
    assert record["markers"]["ownership_capture"]["state"] == "blocked"
    assert record["ownership"]["captured"] is False
    assert record["ownership"]["captured_at_ms"] is None
    server = Server(tmp_path / "server.sock")

    def reject_probe(*_args, **_kwargs):
        raise AssertionError("capture phase consulted a live probe")

    with (
        patch("hopper.server.teardown.tmux.pane_identity", side_effect=reject_probe),
        patch("hopper.server.teardown.read_process_identity", side_effect=reject_probe),
        patch("hopper.server.teardown.read_boot_id", side_effect=reject_probe),
        patch("hopper.server.teardown.read_host_boot_identity", side_effect=reject_probe),
        patch("hopper.server.teardown.read_process_table", side_effect=reject_probe),
        patch("hopper.server.teardown.capture_scope_cgroup", side_effect=reject_probe),
        patch("hopper.server.teardown._opened_cgroup", side_effect=reject_probe),
        patch("hopper.server.teardown.resolve_pidfd_interface", side_effect=reject_probe),
        patch("hopper.server.teardown.reopen_process_pidfd", side_effect=reject_probe),
    ):
        server._retry_action(record["lode_id"], None)
        captured = _join_action_step(server, record, "capturing_ownership")

    assert captured["result"]["error"] is None
    assert captured["result"]["ok"] is True
    table = _root_free_process_table(record["ownership"], [])
    with (
        patch("hopper.server.teardown.read_boot_id", return_value=record["boot_id"]),
        patch("hopper.server.teardown.read_process_table", return_value=table),
        patch(
            "hopper.server.teardown.close_owned_pane",
            return_value={"state": "cannot-tell", "error": "test stop"},
        ),
    ):
        server._handle_action_step_result(captured)
        _join_action_step(server, record, "closing_pane")

    durable = actions.load_pending_action(record["lode_id"])
    assert durable["markers"]["ownership_capture"]["state"] == "done"
    assert durable["ownership"]["captured"] is True
    assert isinstance(durable["ownership"]["captured_at_ms"], int)


def test_captured_strict_record_drives_through_server_force_and_proof_gates(tmp_path):
    loaded, fixture_bytes = _load_captured_record(tmp_path, "wedged-populated-scope-5tuofgwh.json")
    fixture_identity = (
        loaded["lode_id"],
        loaded["action_id"],
        loaded["expected_generation"],
    )
    supervisor_fd, supervisor_write = os.pipe()
    pane_fd, pane_write = os.pipe()
    cgroup_fd, cgroup_write = os.pipe()
    cgroup_observations = iter(["populated", "populated", "empty"])
    signals = []
    durable_intents = []

    def kill_cgroup(_cgroup, **_kwargs):
        durable = actions.load_pending_action(loaded["lode_id"])
        durable_intents.append(
            (
                durable["markers"]["scope_kill"]["state"],
                durable["markers"]["supervisor_kill"]["state"],
            )
        )
        signals.append("cgroup")
        return {"state": "signalled", "error": None}

    with patch("hopper.server.get_git_hash", return_value="test-hash"):
        server = Server(tmp_path / "server.sock")
    try:
        with (
            patch(
                "hopper.server.teardown.read_host_boot_identity",
                return_value=loaded["boot_id"],
            ),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            patch(
                "hopper.server.teardown.reopen_process_pidfd",
                side_effect=[
                    {"state": "alive", "fd": supervisor_fd, "error": None},
                    {"state": "alive", "fd": pane_fd, "error": None},
                ],
            ),
            patch(
                "hopper.server.teardown._opened_cgroup",
                return_value=(cgroup_fd, None),
            ),
            patch(
                "hopper.server.teardown.observe_retained_cgroup",
                side_effect=lambda *_args, **_kwargs: next(cgroup_observations),
            ),
            patch("hopper.server.teardown.observe_pidfd", return_value="gone"),
            patch("hopper.server.teardown.kill_cgroup", side_effect=kill_cgroup),
            patch("hopper.server.oom.find_systemctl", return_value="/bin/systemctl"),
            patch(
                "hopper.server.oom.read_scope_control_group",
                return_value={"state": "absent", "control_group": None},
            ),
        ):
            server._retry_action(loaded["lode_id"], None)
            armed = actions.load_pending_action(loaded["lode_id"])
            assert armed["phase"] == "force_killing"
            assert armed["markers"]["containment"]["state"] == "intent"
            assert armed["markers"]["scope_kill"]["state"] == "intent"
            assert armed["markers"]["supervisor_kill"]["state"] == "intent"

            thread = server.action_threads[(loaded["action_id"], "force_killing")]
            thread.join(timeout=2)
            assert not thread.is_alive()
            internal, _conn = server.event_queue.get(timeout=2)
            result = internal["result"]
            assert result["pidfd"] == supervisor_fd
            assert result["pane_root_pidfd"] == pane_fd
            assert result["cgroup_fd"] == cgroup_fd

            with patch.object(server, "_continue_completion_action") as continuation:
                server._handle_action_step_result(internal)

        terminal = actions.load_pending_action(loaded["lode_id"])
        assert terminal["phase"] == "publishing_terminal"
        assert terminal["containment"]["state"] == "proven"
        assert terminal["containment"]["result"] == "linux-strict-killed-empty"
        assert terminal["markers"]["containment"]["state"] == "done"
        assert durable_intents == [("intent", "intent")]
        assert signals == ["cgroup"]
        continuation.assert_called_once()
        continued = continuation.call_args.args[0]
        assert actions.containment_is_proven(continued)
        assert (
            continued["lode_id"],
            continued["action_id"],
            continued["expected_generation"],
        ) == fixture_identity
        assert json.loads(fixture_bytes)["action_id"] == terminal["action_id"]
    finally:
        for fd in (
            supervisor_fd,
            pane_fd,
            cgroup_fd,
            supervisor_write,
            pane_write,
            cgroup_write,
        ):
            try:
                os.close(fd)
            except OSError:
                pass


def test_captured_records_keep_the_exact_schema_boundary(tmp_path):
    fixture_bytes = (FIXTURE_DIR / "wedged-populated-scope-5tuofgwh.json").read_bytes()
    source = json.loads(fixture_bytes)
    lode_id = source["lode_id"]
    target = tmp_path / "lodes" / lode_id / "pending-completion.json"
    target.parent.mkdir(parents=True)

    changed_interval = fixture_bytes.replace(b'"poll_interval_ms":50', b'"poll_interval_ms":250', 1)
    target.write_bytes(changed_interval)
    with pytest.raises(ValueError, match="^containment.poll_interval_ms must be 50$"):
        actions.load_pending_action(lode_id)

    del source["containment"]["started_monotonic_ns"]
    target.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(
        ValueError,
        match=(
            r"^containment has missing keys \['started_monotonic_ns'\] "
            r"and unknown keys \[\]$"
        ),
    ):
        actions.load_pending_action(lode_id)

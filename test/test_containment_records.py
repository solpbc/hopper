# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Acceptance tests for captured pending-action containment records."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hopper import actions, teardown
from hopper.server import Server

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pending-actions"


def _load_captured_record(tmp_path: Path, fixture_name: str) -> tuple[dict, bytes]:
    """Copy captured bytes across the real pending-action load boundary."""
    fixture_bytes = (FIXTURE_DIR / fixture_name).read_bytes()
    lode_id = json.loads(fixture_bytes)["lode_id"]
    target = tmp_path / "lodes" / lode_id / "pending-completion.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(fixture_bytes)
    return actions.load_pending_action(lode_id), fixture_bytes


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


def test_captured_restart_remains_blocked_at_ownership_capture(tmp_path):
    record, fixture_bytes = _load_captured_record(
        tmp_path, "wedged-restart-ownership-blghq7to.json"
    )
    original_containment = record["containment"].copy()
    signals = []

    with patch("hopper.server.get_git_hash", return_value="test-hash"):
        server = Server(tmp_path / "server.sock")
    with (
        patch("hopper.teardown.kill_cgroup", side_effect=lambda *_args: signals.append("cgroup")),
        patch(
            "hopper.teardown.signal_process_pidfd",
            side_effect=lambda *_args: signals.append("process"),
        ),
    ):
        server._resume_action(record["lode_id"])

    reloaded = actions.load_pending_action(record["lode_id"])
    assert reloaded["action_id"] == json.loads(fixture_bytes)["action_id"]
    assert reloaded["phase"] == "containment_blocked"
    assert reloaded["markers"]["ownership_capture"]["state"] == "blocked"
    assert reloaded["containment"] == original_containment
    assert signals == []


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

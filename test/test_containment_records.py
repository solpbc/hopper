# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Acceptance tests for captured pending-action containment records."""

import json
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

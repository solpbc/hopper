# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the hopper server."""

import base64
import copy
import fcntl
import hashlib
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

import hopper.client as hopper_client
import hopper.server as hopper_server
from hopper import actions, config, git, teardown
from hopper.backlog import BacklogItem
from hopper.client import (
    HopperConnection,
    read_lode_snapshot,
    send_message,
    send_pane_input,
    submit_lode_action,
)
from hopper.client import create_lode as request_lode_creation
from hopper.config import config_transaction
from hopper.lodes import (
    format_terminal_failure_status,
    lode_driver,
    lode_gate,
    lode_stage_session,
    project_lode_claude_state,
    publish_lode_gate,
    save_archived_lodes,
    save_lodes,
    update_lode_state,
)
from hopper.projects import Project, touch_project
from hopper.server import (
    LISTEN_BACKLOG,
    Server,
    ServerLockHeld,
    SpawnOutcome,
    get_git_hash,
    start_server_with_tui,
)
from hopper.tmux import Liveness, WindowSpawnOutcome
from hopper.wait import classify


class TestGetGitHash:
    def test_returns_short_hash(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "abc1234\n"
            result = get_git_hash()
            assert result == "abc1234"
            mock_run.assert_called_once_with(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            )

    def test_returns_none_when_git_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            result = get_git_hash()
            assert result is None

    def test_returns_none_when_git_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_git_hash()
            assert result is None


def test_server_stores_git_hash():
    """Server captures git hash at initialization."""
    with patch("hopper.server.get_git_hash", return_value="abc1234"):
        srv = Server(socket_path="/tmp/unused.sock")
        assert srv.git_hash == "abc1234"


@pytest.fixture
def socket_path(tmp_path):
    """Provide a temporary socket path."""
    return tmp_path / "test.sock"


@pytest.fixture
def server(socket_path):
    """Start a server in a background thread."""
    srv = Server(socket_path)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()
    assert srv.ready.wait(5), "Server did not start"

    yield srv

    srv.stop()
    thread.join(timeout=2)


def _recv_messages_until(
    client: socket.socket, expected_types: set[str], timeout: float = 2.0
) -> list[dict]:
    """Receive broadcast messages until expected types are observed or timeout."""
    messages: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        seen_types = {msg.get("type") for msg in messages}
        if expected_types.issubset(seen_types):
            break
        try:
            data = client.recv(4096).decode("utf-8")
        except socket.timeout:
            continue
        for line in data.strip().split("\n"):
            if line:
                messages.append(json.loads(line))
    return messages


def _decode_mock_response(conn: MagicMock) -> dict:
    """Decode the last JSON response sent through a mocked socket."""
    payload = conn.sendall.call_args.args[0].decode("utf-8").strip()
    return json.loads(payload)


def _mark_stage_started(lode: dict, stage: str, started: bool = True) -> None:
    """Update a test lode's canonical session and compatibility projection together."""
    lode_stage_session(lode, stage)["started"] = started
    project_lode_claude_state(lode)


def _mock_client(server: Server) -> MagicMock:
    """Register a mocked connection with its required write lock."""
    conn = MagicMock()
    with server.lock:
        server.clients.append(conn)
        server.write_locks[conn] = threading.Lock()
    return conn


TEST_RUN_GENERATION = "a" * 32
PENDING_ACTION_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pending-actions"


def test_reconnecting_is_server_only_and_rejects_progress():
    assert "reconnecting" not in hopper_server.SUPPORTED_LODE_STATES
    assert "reconnecting" in hopper_server.PROGRESS_REJECT_STATES
    assert hopper_server.HELD_RUNNER_MUTATION_TYPES == (
        hopper_client.RUNNER_MUTATION_TYPES - {"lode_register", "lode_supervisor_register"}
    )


def _spawned(pane_id: str) -> tuple[WindowSpawnOutcome, str]:
    return WindowSpawnOutcome.SPAWNED, pane_id


def _completion_process(pid: int, ppid: int, pgid: int) -> dict:
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid,
        "birth": {
            "kind": "linux-proc-starttime",
            "boot_id": "boot-one",
            "value": str(pid * 10),
        },
    }


def _completion_run_ownership(lode_id="abcd2345", generation=TEST_RUN_GENERATION) -> dict:
    supervisor = _completion_process(101, 100, 100)
    return {
        "schema_version": 1,
        "lode_id": lode_id,
        "run_generation": generation,
        "registered_at_ms": 1_000,
        "boot_id": "boot-one",
        "platform": "linux",
        "proof_mode": "linux-degraded",
        "degraded_reason": "systemd scope unavailable",
        "pane": {
            "pane_id": "%1",
            "window_id": "@1",
            "root_process": _completion_process(100, 1, 100),
        },
        "supervisor": supervisor,
        "worker": supervisor,
        "process_group": 100,
        "descendants": [_completion_process(102, 101, 100)],
        "unit": None,
        "cgroup": None,
        "unit_name": None,
    }


def _strict_completion_run_ownership(lode_id="abcd2345", generation=TEST_RUN_GENERATION) -> dict:
    record = _completion_run_ownership(lode_id, generation)
    unit_name = f"hopper-lode-{lode_id}-{generation}.scope"
    control_group = f"/user.slice/{unit_name}"
    record.update(
        proof_mode="linux-strict",
        degraded_reason=None,
        unit_name=unit_name,
        unit={
            "name": unit_name,
            "load_state": "loaded",
            "control_group": control_group,
        },
        cgroup={
            "relative_path": control_group,
            "absolute_path": f"/sys/fs/cgroup{control_group}",
            "identity": {"st_dev": 1, "st_ino": 2},
            "boot_id": "boot-one",
        },
    )
    return record


def _strict_registration_facts(
    lode_id="abcd2345", generation=TEST_RUN_GENERATION
) -> tuple[dict, dict, dict]:
    final = _strict_completion_run_ownership(lode_id, generation)
    worker = _completion_process(102, 101, 102)
    source = {
        **final,
        "worker": None,
        "descendants": [],
        "unit": None,
        "cgroup": None,
    }
    ownership = {
        "platform": "linux",
        "proof_mode": "linux-strict",
        "pane": copy.deepcopy(source["pane"]),
        "supervisor": copy.deepcopy(source["supervisor"]),
        "worker": worker,
        "process_group": source["process_group"],
        "descendants": [],
        "unit": copy.deepcopy(final["unit"]),
        "cgroup": copy.deepcopy(final["cgroup"]),
    }
    message = {
        "type": "lode_register",
        "lode_id": lode_id,
        "run_generation": generation,
        "tmux_pane": source["pane"]["pane_id"],
        "pid": worker["pid"],
        "ppid": worker["ppid"],
        "pgid": worker["pgid"],
        "actual_unit": source["unit_name"],
    }
    return source, ownership, message


def _ship_completion_facts(lode_id: str, action_id: str) -> dict:
    worktree = f"/tmp/{lode_id}"
    return {
        "provenance": {
            name: {"realpath": f"/tmp/{name}", "identity": {"st_dev": 1, "st_ino": index}}
            for index, name in enumerate(
                ("project", "git_common_dir", "worktree", "worktree_git_dir"), start=1
            )
        }
        | {
            "worktree": {"realpath": worktree, "identity": {"st_dev": 1, "st_ino": 3}},
            "branch_ref": f"refs/heads/hopper-{lode_id}",
            "branch_oid": "d" * 40,
            "head_oid": "d" * 40,
        },
        "landing": {"cause": None, "base_ref": None, "detail": None, "accepted": False},
        "backlog": {
            "planned": False,
            "selected_item_id": None,
            "promoted_lode_id": None,
            "remaining_item_ids": [],
            "applied": False,
        },
        "archive_published": False,
        "quarantine": {
            "original_path": worktree,
            "quarantine_path": f"/tmp/.{lode_id}-quarantine-{action_id}",
            "expected_identity": {"st_dev": 1, "st_ino": 3},
            "registration_repaired": False,
            "removal_outcome": "pending",
            "branch_outcome": "pending",
        },
        "cleanup_failure": None,
    }


def _pending_completion_record(
    lode_id="abcd2345", generation=TEST_RUN_GENERATION, *, stage="mill"
) -> dict:
    ownership = _completion_run_ownership(lode_id, generation)
    source = actions.write_run_ownership(ownership)
    output = actions.stage_output(lode_id, b"accepted\n", blob_id="b" * 32)
    record = actions.new_pending_action(
        lode_id=lode_id,
        stage=stage,
        expected_generation=generation,
        action_type="completion",
        target_disposition={
            "mill": "advance_refine",
            "refine": "advance_ship",
            "ship": "shipped_archived",
        }[stage],
        force_consent=False,
        output_facts=output,
        ownership_record=ownership,
        source_record_sha256=actions.durable_json_sha256(source),
        action_id="c" * 32,
        accepted_ms=1_001,
        ship=(_ship_completion_facts(lode_id, "c" * 32) if stage == "ship" else None),
    )
    actions.write_pending_action(record)
    return record


def _blocked_output_record(
    lode_id="abcd2345", generation=TEST_RUN_GENERATION, *, stage="mill"
) -> dict:
    record = _pending_completion_record(lode_id, generation, stage=stage)
    actions.transition_marker(record, "output_publish", "intent", attempt_id="d" * 32)
    actions.transition_marker(
        record,
        "output_publish",
        "blocked",
        attempt_id="d" * 32,
        detail="staged output unavailable",
    )
    record["phase"] = "output_blocked"
    record["output"]["failure"] = "staged output unavailable"
    record["recovery"] = {
        "kind": "output",
        "message": "staged output unavailable",
        "command": f"hop lode repair-output {lode_id}",
    }
    actions.write_pending_action(record)
    return record


def _repair_output_message(record: dict, data=b"accepted\n") -> dict:
    return {
        "type": "lode_repair_output",
        "lode_id": record["lode_id"],
        "action_id": record["action_id"],
        "stage": record["stage"],
        "expected_generation": record["expected_generation"],
        "next_action": record["next_action"],
        "token": record["output"]["repair_token"],
        "output_base64": base64.b64encode(data).decode("ascii"),
        "byte_length": len(data),
        "digest_algorithm": "sha256",
        "digest_hex": hashlib.sha256(data).hexdigest(),
    }


def _complete_marker(record: dict, marker_name: str) -> None:
    actions.transition_marker(record, marker_name, "intent")
    actions.transition_marker(
        record,
        marker_name,
        "done",
        attempt_id=record["markers"][marker_name]["attempt_id"],
    )


def _earn_degraded_containment_proof(record: dict) -> dict:
    """Run the real pure machine to earn a non-shortcut empty proof."""
    record["containment"].update(
        state="not_started",
        started_monotonic_ns=None,
        deadline_monotonic_ns=None,
        result=None,
        proof_label=None,
        last_error=None,
    )
    record["ownership"]["proof_mode"] = "linux-degraded"
    record["ownership"]["platform"] = "linux"
    record["ownership"]["unit"] = None
    record["ownership"]["cgroup"] = None
    proof = teardown.observe_containment(
        record,
        {
            "observe_bounded": lambda: {
                "state": "empty",
                "count": 0,
                "identities": [],
                "resolution": "complete",
            },
            "observe_pane": lambda: "gone",
        },
        host_boot_identity=record["boot_id"],
        now_ns=lambda: 1_000_000_000,
        poll=lambda _seconds: None,
    )
    assert proof["result"] == "linux-degraded-bounded-empty"
    record["containment"] = proof
    return proof


def _set_marker_state(record: dict, marker_name: str, state: str) -> None:
    if state == "not_started":
        return
    actions.transition_marker(record, marker_name, "intent")
    if state in {"blocked", "done"}:
        actions.transition_marker(
            record,
            marker_name,
            state,
            attempt_id=record["markers"][marker_name]["attempt_id"],
            detail="test state",
        )


def _manual_action_message(
    action_type: str,
    *,
    action_id: str = "d" * 32,
    lode_id: str = "abcd2345",
    generation: str | None = TEST_RUN_GENERATION,
    stage: str = "mill",
    force: bool = False,
) -> dict:
    return {
        "type": "lode_action",
        "action_id": action_id,
        "lode_id": lode_id,
        "expected_generation": generation,
        "action_type": action_type,
        "target_disposition": {
            "pause": "paused",
            "restart": "replacement_spawned",
            "kill": "killed_archived",
            "archive": "archived",
        }[action_type],
        "force_consent": force,
        "stage": stage,
    }


def _post_containment_manual_record(action_type: str, *, force: bool = False) -> dict:
    ownership = _completion_run_ownership()
    source = actions.write_run_ownership(ownership)
    checked = {
        "outcome": "consent_override" if force else "safe",
        "count": None if force else 0,
        "basis": "kill --force" if force else "origin/main",
        "error": None,
        "checked_at_ms": 1_000,
    }
    record = actions.new_pending_action(
        lode_id="abcd2345",
        stage="mill",
        expected_generation=TEST_RUN_GENERATION,
        action_type=action_type,
        target_disposition={
            "pause": "paused",
            "restart": "replacement_spawned",
            "kill": "killed_archived",
            "archive": "archived",
        }[action_type],
        force_consent=force,
        ownership_record=ownership,
        source_record_sha256=actions.durable_json_sha256(source),
        action_id="d" * 32,
        durability=(
            {
                "required": True,
                "preflight": checked,
                "final": {
                    "outcome": "not_required",
                    "count": 0,
                    "basis": "pending post-containment recheck",
                    "error": None,
                    "checked_at_ms": None,
                },
            }
            if action_type in {"kill", "archive"}
            else None
        ),
    )
    for marker_name in ("ownership_capture", "pane_close", "containment"):
        _complete_marker(record, marker_name)
    record["ownership"]["captured"] = True
    record["ownership"]["captured_at_ms"] = 1_001
    record["containment"].update(
        state="proven",
        result="linux-degraded-bounded-empty",
        proof_label="bounded Linux containment observed",
    )
    record["phase"] = "publishing_terminal"
    actions.write_pending_action(record)
    return record


def _pending_manual_containment_record(
    action_type: str, *, failure_point: str, force: bool = False
) -> dict:
    """Build an accepted manual action at an inspection or exact-kill boundary."""
    ownership = _strict_completion_run_ownership()
    source = actions.write_run_ownership(ownership)
    checked = {
        "outcome": "consent_override" if force and action_type == "kill" else "safe",
        "count": None if force and action_type == "kill" else 0,
        "basis": "kill --force" if force and action_type == "kill" else "origin/main",
        "error": None,
        "checked_at_ms": 1_000,
    }
    record = actions.new_pending_action(
        lode_id="abcd2345",
        stage="mill",
        expected_generation=TEST_RUN_GENERATION,
        action_type=action_type,
        target_disposition={
            "pause": "paused",
            "restart": "replacement_spawned",
            "kill": "killed_archived",
            "archive": "archived",
        }[action_type],
        force_consent=force,
        ownership_record=ownership,
        source_record_sha256=actions.durable_json_sha256(source),
        action_id="d" * 32,
        durability=(
            {
                "required": True,
                "preflight": dict(checked),
                "final": {
                    "outcome": "not_required",
                    "count": 0,
                    "basis": "pending post-containment recheck",
                    "error": None,
                    "checked_at_ms": None,
                },
            }
            if action_type in {"kill", "archive"}
            else None
        ),
    )
    for marker_name in ("ownership_capture", "pane_close"):
        _complete_marker(record, marker_name)
    record["ownership"].update(captured=True, captured_at_ms=1_001)
    actions.transition_marker(record, "containment", "intent", attempt_id="e" * 32)
    record["containment"].update(
        state="grace",
        started_monotonic_ns=1_000,
        deadline_monotonic_ns=2_000,
    )
    record["phase"] = "observing_containment"
    if failure_point == "kill":
        record["containment"].update(
            state="kill_pending",
            last_cgroup_observation="populated",
            last_supervisor_observation="alive",
        )
        actions.transition_marker(record, "scope_kill", "intent", attempt_id="f" * 32)
        actions.transition_marker(record, "supervisor_kill", "intent", attempt_id="f" * 32)
        record["phase"] = "force_killing"
    actions.write_pending_action(record)
    return record


def _action_step_result(record: dict, *, phase: str, result: dict) -> dict:
    marker_name = {
        "capturing_ownership": "ownership_capture",
        "closing_pane": "pane_close",
        "observing_containment": "containment",
        "force_killing": "containment",
        "rechecking_durability": "durability_recheck",
        "spawning": "spawn",
    }[phase]
    return {
        "type": "_action_step_result",
        "lode_id": record["lode_id"],
        "expected_generation": record["expected_generation"],
        "action_id": record["action_id"],
        "marker_name": marker_name,
        "phase": phase,
        "attempt_id": record["markers"][marker_name]["attempt_id"],
        "result": result,
    }


def _join_action_step(server: Server, record: dict, phase: str) -> dict:
    """Join a real action worker and return its queued result."""
    thread = server.action_threads[(record["action_id"], phase)]
    thread.join(timeout=2)
    assert not thread.is_alive()
    internal, _conn = server.event_queue.get(timeout=2)
    assert internal["type"] == "_action_step_result"
    assert internal["action_id"] == record["action_id"]
    assert internal["phase"] == phase
    return internal


def _root_free_process_table(
    ownership: dict,
    identities: list[dict],
    *,
    state: str = "complete",
    error: str | None = None,
) -> dict:
    """Build a fake table that explicitly excludes every recorded root."""
    root_pids = {
        ownership["pane"]["root_process"]["pid"],
        ownership["supervisor"]["pid"],
        ownership["worker"]["pid"],
    }
    assert root_pids.isdisjoint(identity["pid"] for identity in identities)
    return {"state": state, "identities": identities, "error": error}


def _validated_pending_variant(record: dict) -> dict:
    """Exercise the durable writer and real pending-action validator."""
    actions.write_pending_action(record)
    return actions.load_pending_action(record["lode_id"])


def _blocked_capture_completion_record() -> dict:
    record = _pending_completion_record()
    _complete_marker(record, "output_publish")
    record["output"].update(published=True, failure=None)
    actions.transition_marker(record, "ownership_capture", "intent")
    actions.transition_marker(
        record,
        "ownership_capture",
        "blocked",
        attempt_id=record["markers"]["ownership_capture"]["attempt_id"],
        detail="capture interrupted",
    )
    record["phase"] = "containment_blocked"
    record["recovery"] = {
        "kind": "ownership",
        "message": "capture interrupted",
        "command": actions.recovery_command(record, "ownership"),
    }
    return _validated_pending_variant(record)


def _pending_manual_record_with_ownership(ownership: dict, *, action_id: str = "d" * 32) -> dict:
    source = actions.write_run_ownership(ownership)
    record = actions.new_pending_action(
        lode_id=ownership["lode_id"],
        stage="mill",
        expected_generation=ownership["run_generation"],
        action_type="pause",
        target_disposition="paused",
        force_consent=False,
        ownership_record=ownership,
        source_record_sha256=actions.durable_json_sha256(source),
        action_id=action_id,
    )
    return _validated_pending_variant(record)


def _blocked_pane_close_record(ownership: dict) -> dict:
    record = _pending_manual_record_with_ownership(ownership)
    _complete_marker(record, "ownership_capture")
    record["ownership"].update(captured=True, captured_at_ms=1_001)
    record["containment"]["state"] = "pane_close_pending"
    actions.transition_marker(record, "pane_close", "intent")
    actions.transition_marker(
        record,
        "pane_close",
        "blocked",
        attempt_id=record["markers"]["pane_close"]["attempt_id"],
        detail="pane-close discovery interrupted",
    )
    record["phase"] = "containment_blocked"
    record["recovery"] = {
        "kind": "ownership",
        "message": "pane-close discovery interrupted",
        "command": actions.recovery_command(record, "ownership"),
    }
    return _validated_pending_variant(record)


def _capture_without_live_probes(server: Server, record: dict) -> dict:
    """Retry capture while making every former live dependency fatal."""

    def rejected_probe(*_args, **_kwargs):
        raise AssertionError("capture phase consulted a live probe")

    with (
        patch("hopper.server.teardown.tmux.pane_identity", side_effect=rejected_probe),
        patch("hopper.server.teardown.read_process_identity", side_effect=rejected_probe),
        patch("hopper.server.teardown.read_boot_id", side_effect=rejected_probe),
        patch("hopper.server.teardown.read_host_boot_identity", side_effect=rejected_probe),
        patch("hopper.server.teardown.read_process_table", side_effect=rejected_probe),
        patch("hopper.server.teardown.capture_scope_cgroup", side_effect=rejected_probe),
        patch("hopper.server.teardown._opened_cgroup", side_effect=rejected_probe),
        patch("hopper.server.teardown.resolve_pidfd_interface", side_effect=rejected_probe),
        patch("hopper.server.teardown.reopen_process_pidfd", side_effect=rejected_probe),
    ):
        server._retry_action(record["lode_id"], None)
        return _join_action_step(server, record, "capturing_ownership")


def _spawning_completion_record() -> dict:
    record = _pending_completion_record()
    _complete_marker(record, "containment")
    record["containment"].update(
        state="proven",
        result="linux-degraded-bounded-empty",
        proof_label="bounded empty containment",
    )
    _complete_marker(record, "lode_mutation")
    record["spawn"] = {
        "target_lode_id": record["lode_id"],
        "target_generation": "e" * 32,
        "receipt_relative_path": f"spawn-{record['action_id']}.json",
        "pane_id": None,
        "supervisor_adopted": False,
        "worker_adopted": False,
    }
    actions.transition_marker(record, "spawn", "intent")
    record["phase"] = "spawning"
    actions.write_pending_action(record)
    return record


class _InjectedActionCrash(BaseException):
    pass


def _new_manual_action_record(action_type: str) -> dict:
    ownership = _strict_completion_run_ownership()
    source = actions.write_run_ownership(ownership)
    safe = {
        "outcome": "safe",
        "count": 0,
        "basis": "origin/main",
        "error": None,
        "checked_at_ms": 1_000,
    }
    return actions.new_pending_action(
        lode_id="abcd2345",
        stage="mill",
        expected_generation=TEST_RUN_GENERATION,
        action_type=action_type,
        target_disposition={
            "pause": "paused",
            "restart": "replacement_spawned",
            "kill": "killed_archived",
            "archive": "archived",
        }[action_type],
        force_consent=action_type == "restart",
        ownership_record=ownership,
        source_record_sha256=actions.durable_json_sha256(source),
        action_id="d" * 32,
        durability=(
            {
                "required": True,
                "preflight": dict(safe),
                "final": {
                    "outcome": "not_required",
                    "count": 0,
                    "basis": "pending post-containment recheck",
                    "error": None,
                    "checked_at_ms": None,
                },
            }
            if action_type in {"kill", "archive"}
            else None
        ),
    )


def _install_synchronous_action_workers(
    server: Server,
    counters: dict[str, int],
    *,
    crash_after_spawn_receipt: bool = False,
) -> None:
    """Replace daemon workers with serialized, identity-bound fake results."""

    def schedule(record: dict, marker_name: str, phase: str) -> None:
        marker = record["markers"][marker_name]
        if marker["state"] in {"not_started", "blocked"}:
            actions.transition_marker(record, marker_name, "intent")
        elif marker["state"] != "intent":
            return
        record["phase"] = phase
        record["recovery"] = {"kind": None, "message": None, "command": None}
        server._persist_action(record, via=f"action_intent:{marker_name}")

        if phase == "capturing_ownership":
            ownership = copy.deepcopy(record["ownership"])
            ownership.update(captured=True, captured_at_ms=1_001)
            result = {"ok": True, "ownership": ownership}
        elif phase == "closing_pane":
            counters["close"] += 1
            result = {"ok": True, "error": None}
        elif phase == "observing_containment":
            counters["grace"] += 1
            assert record["containment"]["state"] in {
                "not_started",
                "pane_close_pending",
                "grace",
            }
            containment = copy.deepcopy(record["containment"])
            containment.update(
                state="kill_pending",
                started_monotonic_ns=31_000_000_000,
                deadline_monotonic_ns=61_000_000_000,
                last_cgroup_observation="populated",
                last_supervisor_observation="alive",
                last_owned_process_count=None,
                result=None,
                proof_label=None,
                last_error=None,
            )
            result = {"ok": True, "containment": containment, "error": None}
        elif phase == "force_killing":
            counters["kill"] += 1
            assert record["containment"]["state"] in {
                "kill_pending",
                "verify_after_kill",
            }
            containment = copy.deepcopy(record["containment"])
            containment.update(
                state="proven",
                last_cgroup_observation="empty",
                last_supervisor_observation="gone",
                last_owned_process_count=0,
                result="linux-strict-killed-empty",
                proof_label="strict Linux containment proven",
                last_error=None,
            )
            result = {"ok": True, "containment": containment, "error": None}
        elif phase == "rechecking_durability":
            result = {
                "ok": True,
                "observation": {
                    "outcome": "safe",
                    "count": 0,
                    "basis": "origin/main",
                    "error": None,
                    "checked_at_ms": 2_000,
                },
                "error": None,
            }
        elif phase == "spawning":
            spawn = record["spawn"]
            receipt = actions.load_spawn_receipt(record["lode_id"], record["action_id"])
            if receipt is None:
                counters["spawn"] += 1
                receipt = {
                    "schema_version": actions.SPAWN_RECEIPT_SCHEMA_VERSION,
                    "action_id": record["action_id"],
                    "source_lode_id": record["lode_id"],
                    "target_lode_id": spawn["target_lode_id"],
                    "target_generation": spawn["target_generation"],
                    "pane_id": "%successor",
                }
                actions.write_spawn_receipt(receipt)
                actions.write_run_ownership(
                    _strict_completion_run_ownership(
                        spawn["target_lode_id"], spawn["target_generation"]
                    )
                )
                if crash_after_spawn_receipt:
                    raise _InjectedActionCrash("after replacement spawn")
            result = {"ok": True, "pane_id": receipt["pane_id"], "adopted": True}
        else:
            raise AssertionError(f"unexpected synchronous phase {phase}")

        server._handle_action_step_result(_action_step_result(record, phase=phase, result=result))

    server._schedule_action_step = schedule


def _apply_prepared_action(server: Server, lode_id: str, generation: str | None) -> None:
    thread = server.registration_threads[f"accept:{lode_id}:{generation}"]
    thread.join(timeout=2)
    assert not thread.is_alive()
    internal, response_conn = server.event_queue.get(timeout=2)
    assert internal["type"] == "_action_acceptance_result"
    server._handle_mutation(internal, response_conn)


def _apply_worker_registration_capture(
    server: Server,
    lode: dict,
    message: dict,
    conn: MagicMock,
) -> dict:
    server._start_registration_capture("worker", lode, message, conn)
    thread = server.registration_threads[f"worker:{lode['id']}:{lode['run_generation']}"]
    thread.join(timeout=2)
    assert not thread.is_alive()
    internal, response_conn = server.event_queue.get(timeout=2)
    assert internal["type"] == "_registration_capture_result"
    server._handle_registration_capture_result(internal, response_conn)
    return _decode_mock_response(conn)


def _runner_message(server: Server, msg_type: str, lode_id: str, **fields) -> dict:
    """Build a current-generation runner protocol message for server tests."""
    lode = server._find_lode(lode_id)
    assert lode is not None
    lode["run_generation"] = TEST_RUN_GENERATION
    message = {
        "type": msg_type,
        "lode_id": lode_id,
        "run_generation": TEST_RUN_GENERATION,
        "ts": 1,
        **fields,
    }
    if msg_type == "lode_register":
        message.update({"armed_mode": "non-linux", "actual_unit": None})
    return message


@pytest.fixture
def registered_generation_capture(monkeypatch):
    """Supply a previously acknowledged supervisor record to worker-registration tests."""

    def source(lode_id, generation, *, require_worker=False):
        birth = {"kind": "ps-lstart", "boot_id": None, "value": "opaque-start"}
        process = {"pid": 100, "ppid": 1, "pgid": 100, "birth": birth}
        return {
            "schema_version": 1,
            "lode_id": lode_id,
            "run_generation": generation,
            "registered_at_ms": 1_000,
            "boot_id": "ps-pid1:opaque-boot",
            "platform": "other",
            "proof_mode": "other-bounded-no-birth",
            "degraded_reason": "bounded test ownership",
            "pane": {"pane_id": "%1", "window_id": "@1", "root_process": process},
            "supervisor": process,
            "worker": process if require_worker else None,
            "process_group": 100,
            "descendants": [],
            "unit": None,
            "cgroup": None,
            "unit_name": None,
        }

    def capture(ownership, message):
        pid = message.get("pid") or 100
        worker = {
            "pid": pid,
            "ppid": message.get("ppid", 1),
            "pgid": message.get("pgid", 100),
            "birth": {"kind": "ps-lstart", "boot_id": None, "value": "worker-start"},
        }
        return {"record": {**ownership, "worker": worker}, "pidfd": None}

    monkeypatch.setattr(actions, "load_run_ownership", source)
    monkeypatch.setattr(actions, "write_run_ownership", lambda _record: Path("ownership"))
    monkeypatch.setattr(hopper_server, "_capture_worker_registration", capture)


@pytest.mark.parametrize(
    "armed_mode",
    ["supported", "degraded-no-controller", "degraded-no-score"],
)
def test_capture_worker_registration_accepts_strict_linux_oom_modes(armed_mode):
    source, ownership, message = _strict_registration_facts()
    message["armed_mode"] = armed_mode
    captured = {"state": "captured", "ownership": ownership, "error": None}
    membership = {
        "state": "proven",
        "control_group": ownership["cgroup"]["relative_path"],
        "error": None,
    }

    with (
        patch("hopper.server.oom.find_systemctl", return_value="systemctl"),
        patch("hopper.server.teardown.capture_ownership", return_value=captured) as capture,
        patch(
            "hopper.server.teardown.capture_worker_cgroup_membership",
            return_value=membership,
        ) as prove,
        patch("hopper.server.teardown.resolve_pidfd_interface", return_value=None),
    ):
        result = hopper_server._capture_worker_registration(source, message)

    assert result["record"]["proof_mode"] == "linux-strict"
    assert result["record"]["worker"] == ownership["worker"]
    assert result["pidfd"] is None
    capture.assert_called_once_with(
        pane_id=source["pane"]["pane_id"],
        supervisor_pid=source["supervisor"]["pid"],
        worker_pid=message["pid"],
        process_group=source["process_group"],
        unit_name=source["unit_name"],
        systemctl="systemctl",
        platform="linux",
    )
    prove.assert_called_once_with(ownership["worker"], ownership["cgroup"]["relative_path"])


def test_capture_worker_registration_opens_generic_pidfd_when_available():
    source, ownership, message = _strict_registration_facts()
    message["armed_mode"] = "supported"
    captured = {"state": "captured", "ownership": ownership, "error": None}
    membership = {
        "state": "proven",
        "control_group": ownership["cgroup"]["relative_path"],
        "error": None,
    }
    read_fd, write_fd = os.pipe()
    pidfd_interface = {
        "source": "test",
        "open": MagicMock(return_value=read_fd),
        "send_signal": MagicMock(),
    }

    try:
        with (
            patch("hopper.server.oom.find_systemctl", return_value="systemctl"),
            patch("hopper.server.teardown.capture_ownership", return_value=captured),
            patch(
                "hopper.server.teardown.capture_worker_cgroup_membership",
                return_value=membership,
            ),
            patch(
                "hopper.server.teardown.resolve_pidfd_interface",
                return_value=pidfd_interface,
            ),
            patch(
                "hopper.teardown.read_boot_id",
                return_value=ownership["supervisor"]["birth"]["boot_id"],
            ),
            patch(
                "hopper.teardown.read_linux_process_identity",
                return_value={
                    "state": "alive",
                    "identity": ownership["supervisor"],
                    "error": None,
                },
            ),
        ):
            result = hopper_server._capture_worker_registration(source, message)

        assert result["pidfd"] == read_fd
        pidfd_interface["open"].assert_called_once_with(ownership["supervisor"]["pid"], 0)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    "armed_mode",
    ["supported", "degraded-no-controller", "degraded-no-score"],
)
def test_strict_registration_accepts_and_preserves_scope(
    socket_path, make_lode, armed_mode, caplog
):
    source, ownership, message = _strict_registration_facts()
    message["armed_mode"] = armed_mode
    actions.write_run_ownership(source)
    server = Server(socket_path)
    lode = make_lode(
        id=source["lode_id"],
        state="running",
        tmux_pane=source["pane"]["pane_id"],
        run_generation=source["run_generation"],
        oom_scope=source["unit_name"],
    )
    server.lodes = [lode]
    conn = _mock_client(server)
    captured = {"state": "captured", "ownership": ownership, "error": None}
    membership = {
        "state": "proven",
        "control_group": ownership["cgroup"]["relative_path"],
        "error": None,
    }

    with (
        patch("hopper.server.oom.find_systemctl", return_value="systemctl"),
        patch("hopper.server.teardown.capture_ownership", return_value=captured),
        patch(
            "hopper.server.teardown.capture_worker_cgroup_membership",
            return_value=membership,
        ) as prove,
        patch("hopper.server.teardown.resolve_pidfd_interface", return_value=None),
        caplog.at_level(logging.INFO, logger="hopper.server"),
    ):
        response = _apply_worker_registration_capture(server, lode, message, conn)

    assert response["type"] == "lode_registered"
    assert response["accepted"] is True
    assert server.lode_clients[lode["id"]] is conn
    assert lode["oom_scope"] == source["unit_name"]
    assert lode["run_generation"] == source["run_generation"]
    durable = actions.load_run_ownership(lode["id"], lode["run_generation"], require_worker=True)
    assert durable is not None
    assert durable["proof_mode"] == "linux-strict"
    assert durable["unit_name"] == source["unit_name"]
    prove.assert_called_once_with(ownership["worker"], ownership["cgroup"]["relative_path"])
    expected_log = (
        f"Worker registration accepted lode={lode['id']} "
        f"generation={lode['run_generation']} containment=linux-strict oom_mode={armed_mode}"
    )
    assert caplog.messages.count(expected_log) == 1


@pytest.mark.parametrize(
    "armed_mode",
    ["supported", "degraded-no-controller", "degraded-no-score"],
)
def test_strict_registration_refuses_unit_claim_mismatch_without_mutation(
    socket_path, make_lode, armed_mode
):
    source, _ownership, message = _strict_registration_facts()
    message.update(armed_mode=armed_mode, actual_unit="hopper-other.scope")
    path = actions.write_run_ownership(source)
    before_bytes = path.read_bytes()
    server = Server(socket_path)
    lode = make_lode(
        id=source["lode_id"],
        state="running",
        tmux_pane=source["pane"]["pane_id"],
        run_generation=source["run_generation"],
        oom_scope=source["unit_name"],
    )
    before_lode = copy.deepcopy(lode)
    server.lodes = [lode]
    conn = _mock_client(server)

    with (
        patch("hopper.server.oom.find_systemctl") as systemctl,
        patch("hopper.server.teardown.capture_ownership") as capture,
        patch("hopper.server.teardown.capture_worker_cgroup_membership") as prove,
    ):
        response = _apply_worker_registration_capture(server, lode, message, conn)

    response.pop("ts", None)
    assert response == {
        "type": "lode_register_refused",
        "lode_id": lode["id"],
        "accepted": False,
        "reason": "worker did not enter the recorded systemd unit",
    }
    assert lode["id"] not in server.lode_clients
    assert lode == before_lode
    assert lode["oom_scope"] == source["unit_name"]
    assert lode["run_generation"] == source["run_generation"]
    assert path.read_bytes() == before_bytes
    systemctl.assert_not_called()
    capture.assert_not_called()
    prove.assert_not_called()


@pytest.mark.parametrize(
    "armed_mode",
    ["supported", "degraded-no-controller", "degraded-no-score"],
)
def test_strict_registration_refuses_unproven_membership_without_mutation(
    socket_path, make_lode, armed_mode
):
    source, ownership, message = _strict_registration_facts()
    message["armed_mode"] = armed_mode
    path = actions.write_run_ownership(source)
    before_bytes = path.read_bytes()
    server = Server(socket_path)
    lode = make_lode(
        id=source["lode_id"],
        state="running",
        tmux_pane=source["pane"]["pane_id"],
        run_generation=source["run_generation"],
        oom_scope=source["unit_name"],
    )
    before_lode = copy.deepcopy(lode)
    server.lodes = [lode]
    conn = _mock_client(server)
    captured = {"state": "captured", "ownership": ownership, "error": None}
    reason = (
        "worker cgroup membership /user.slice/other.scope does not match "
        f"unit control group {ownership['cgroup']['relative_path']}"
    )

    with (
        patch("hopper.server.oom.find_systemctl", return_value="systemctl"),
        patch("hopper.server.teardown.capture_ownership", return_value=captured),
        patch(
            "hopper.server.teardown.capture_worker_cgroup_membership",
            return_value={
                "state": "cannot-tell",
                "control_group": "/user.slice/other.scope",
                "error": reason,
            },
        ) as prove,
        patch("hopper.server.teardown.resolve_pidfd_interface") as pidfd,
    ):
        response = _apply_worker_registration_capture(server, lode, message, conn)

    response.pop("ts", None)
    assert response == {
        "type": "lode_register_refused",
        "lode_id": lode["id"],
        "accepted": False,
        "reason": reason,
    }
    assert lode["id"] not in server.lode_clients
    assert lode == before_lode
    assert lode["oom_scope"] == source["unit_name"]
    assert lode["run_generation"] == source["run_generation"]
    assert path.read_bytes() == before_bytes
    prove.assert_called_once_with(ownership["worker"], ownership["cgroup"]["relative_path"])
    pidfd.assert_not_called()


def test_linux_degraded_registration_refuses_worker_unit_claim():
    source = _completion_run_ownership()
    source.update(worker=None, descendants=[])
    message = {
        "armed_mode": "degraded-no-controller",
        "actual_unit": "hopper-test.scope",
    }

    with (
        patch("hopper.server.oom.find_systemctl") as systemctl,
        patch("hopper.server.teardown.capture_ownership") as capture,
    ):
        with pytest.raises(ValueError, match="degraded worker cannot claim a systemd unit"):
            hopper_server._capture_worker_registration(source, message)

    systemctl.assert_not_called()
    capture.assert_not_called()


def test_late_registration_claim_race_sends_specific_reason(socket_path, make_lode):
    source, ownership, message = _strict_registration_facts()
    message["armed_mode"] = "supported"
    final = {
        **source,
        "worker": ownership["worker"],
        "descendants": ownership["descendants"],
        "unit": ownership["unit"],
        "cgroup": ownership["cgroup"],
    }
    server = Server(socket_path)
    lode = make_lode(
        id=source["lode_id"],
        run_generation=source["run_generation"],
        oom_scope=source["unit_name"],
    )
    server.lodes = [lode]
    conn = _mock_client(server)
    internal = {
        "type": "_registration_capture_result",
        "kind": "worker",
        "key": f"worker:{lode['id']}:{lode['run_generation']}",
        "lode_id": lode["id"],
        "run_generation": lode["run_generation"],
        "request": message,
        "result": {"ok": True, "record": final, "pidfd": None},
    }

    with patch.object(server, "_register_lode_client", return_value=False):
        server._handle_registration_capture_result(internal, conn)

    response = _decode_mock_response(conn)
    response.pop("ts", None)
    assert response == {
        "type": "lode_register_refused",
        "lode_id": lode["id"],
        "accepted": False,
        "reason": "worker registration claim no longer matches the current generation",
    }


def test_completion_acceptance_commits_fence_before_publication(
    socket_path, make_lode, temp_config
):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    actions.write_run_ownership(_completion_run_ownership(lode_id, generation))
    server = Server(socket_path)
    lode = make_lode(
        id=lode_id,
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
    )
    server.lodes = [lode]
    owner = _mock_client(server)
    submitter = _mock_client(server)
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = generation
    output = b"durable output\n"
    message = {
        "type": "lode_action",
        "action_id": "a" * 32,
        "lode_id": lode_id,
        "expected_generation": generation,
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
        "stage": "mill",
        "output_base64": base64.b64encode(output).decode("ascii"),
        "byte_length": len(output),
        "digest_algorithm": "sha256",
        "digest_hex": hashlib.sha256(output).hexdigest(),
        "exchange_id": "f" * 32,
    }

    server._handle_lode_action(message, submitter)
    thread = server.registration_threads[f"accept:{lode_id}:{generation}"]
    thread.join(timeout=2)
    internal, response_conn = server.event_queue.get(timeout=2)
    assert response_conn is None
    assert internal["type"] == "_action_acceptance_result"
    assert internal["exchange_id"] == "f" * 32
    assert not (temp_config / f"lodes/{lode_id}/mill_out.md").exists()

    with patch.object(server, "_schedule_action_step") as schedule:
        server._handle_mutation(internal, response_conn)

    record = actions.load_pending_action(lode_id)
    assert record is not None
    assert record["output"]["digest_hex"] == hashlib.sha256(output).hexdigest()
    assert server.lodes[0]["state"] == "teardown"
    assert not (temp_config / f"lodes/{lode_id}/mill_out.md").exists()
    response = _decode_mock_response(submitter)
    assert response["accepted"] is True
    assert response["exchange_id"] == "f" * 32
    schedule.assert_called_once_with(record, "output_publish", "publishing_output")


def test_completion_staging_failure_reports_its_real_phase(socket_path, make_lode):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    actions.write_run_ownership(_completion_run_ownership(lode_id, generation))
    server = Server(socket_path)
    lode = make_lode(
        id=lode_id,
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
    )
    server.lodes = [lode]
    owner = _mock_client(server)
    submitter = _mock_client(server)
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = generation
    output = b"durable output\n"
    message = {
        "type": "lode_action",
        "action_id": "a" * 32,
        "lode_id": lode_id,
        "expected_generation": generation,
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
        "stage": "mill",
        "output_base64": base64.b64encode(output).decode("ascii"),
        "byte_length": len(output),
        "digest_algorithm": "sha256",
        "digest_hex": hashlib.sha256(output).hexdigest(),
    }

    with patch("hopper.server.actions.stage_output", side_effect=OSError("disk full")):
        server._handle_lode_action(message, submitter)
        server.registration_threads[f"accept:{lode_id}:{generation}"].join(timeout=2)
    internal, response_conn = server.event_queue.get(timeout=2)
    server._handle_mutation(internal, response_conn)

    response = _decode_mock_response(submitter)
    assert response["accepted"] is False
    assert response["reason"] == "output_staging_unavailable"
    assert "disk full" in response["detail"]
    assert actions.load_pending_action(lode_id) is None


def test_completion_record_failure_reports_persistence_not_ownership(socket_path, make_lode):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    actions.write_run_ownership(_completion_run_ownership(lode_id, generation))
    server = Server(socket_path)
    lode = make_lode(
        id=lode_id,
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
    )
    server.lodes = [lode]
    owner = _mock_client(server)
    submitter = _mock_client(server)
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = generation
    output = b"durable output\n"
    message = {
        "type": "lode_action",
        "action_id": "a" * 32,
        "lode_id": lode_id,
        "expected_generation": generation,
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
        "stage": "mill",
        "output_base64": base64.b64encode(output).decode("ascii"),
        "byte_length": len(output),
        "digest_algorithm": "sha256",
        "digest_hex": hashlib.sha256(output).hexdigest(),
    }

    server._handle_lode_action(message, submitter)
    server.registration_threads[f"accept:{lode_id}:{generation}"].join(timeout=2)
    internal, response_conn = server.event_queue.get(timeout=2)
    with patch("hopper.server.actions.write_pending_action", side_effect=OSError("disk full")):
        server._handle_mutation(internal, response_conn)

    response = _decode_mock_response(submitter)
    assert response["accepted"] is False
    assert response["reason"] == "completion_persistence_unavailable"
    assert "disk full" in response["detail"]
    assert actions.load_pending_action(lode_id) is None


def test_lode_action_raw_boundary_rejects_non_boolean_force_without_mutation(
    socket_path, make_lode
):
    server = Server(socket_path)
    lode = make_lode(
        id="abcd2345",
        state="running",
        active=True,
        run_generation=TEST_RUN_GENERATION,
    )
    server.lodes = [lode]
    before = copy.deepcopy(lode)
    conn = _mock_client(server)
    message = {
        "type": "lode_action",
        "action_id": "a" * 32,
        "lode_id": lode["id"],
        "expected_generation": TEST_RUN_GENERATION,
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": 0,
        "stage": "mill",
        "output_base64": "eA==",
        "byte_length": 1,
        "digest_algorithm": "sha256",
        "digest_hex": hashlib.sha256(b"x").hexdigest(),
    }

    server._handle_mutation(message, conn)

    assert lode == before
    assert actions.load_pending_action(lode["id"]) is None
    assert _decode_mock_response(conn)["reason"] == "invalid_action"


@pytest.mark.parametrize("action_type", ["pause", "restart", "kill", "archive"])
@pytest.mark.parametrize("invalid", ["missing_identity", "stale_generation", "invalid_force"])
def test_manual_action_raw_boundary_refusal_is_byte_for_byte_side_effect_free(
    socket_path, make_lode, action_type, invalid
):
    server = Server(socket_path)
    lode = make_lode(
        id="abcd2345",
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=TEST_RUN_GENERATION,
    )
    server.lodes = [lode]
    server.pending_disconnects[(lode["id"], TEST_RUN_GENERATION)] = {
        "deadline": 100,
        "unit_name": "hopper-test.scope",
    }
    server.runner_results[(lode["id"], TEST_RUN_GENERATION)] = (None, 0)
    before_lode = copy.deepcopy(lode)
    before_archived = copy.deepcopy(server.archived_lodes)
    before_containment = copy.deepcopy(server.pending_disconnects)
    before_results = copy.deepcopy(server.runner_results)
    conn = _mock_client(server)
    message = _manual_action_message(action_type)
    if invalid == "missing_identity":
        message.pop("action_id")
    elif invalid == "stale_generation":
        message["expected_generation"] = "f" * 32
    else:
        message["force_consent"] = 1

    with (
        patch("hopper.server.teardown.close_owned_pane") as close,
        patch("hopper.server.archive_lode_for_action") as archive,
    ):
        server._handle_mutation(message, conn)

    assert lode == before_lode
    assert server.archived_lodes == before_archived
    assert server.pending_disconnects == before_containment
    assert server.runner_results == before_results
    assert actions.load_pending_action(lode["id"]) is None
    assert _decode_mock_response(conn)["accepted"] is False
    close.assert_not_called()
    archive.assert_not_called()


@pytest.mark.parametrize(
    ("action_type", "count"),
    [("kill", None), ("kill", 2), ("archive", None), ("archive", 1)],
)
def test_raw_kill_and_guarded_archive_cannot_bypass_durability(
    socket_path, make_lode, action_type, count
):
    lode_id = "abcd2345"
    ownership = _completion_run_ownership(lode_id, TEST_RUN_GENERATION)
    source_path = actions.write_run_ownership(ownership)
    source_before = source_path.read_bytes()
    server = Server(socket_path)
    lode = make_lode(
        id=lode_id,
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=TEST_RUN_GENERATION,
    )
    server.lodes = [lode]
    owner = _mock_client(server)
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = TEST_RUN_GENERATION
    submitter = _mock_client(server)
    before_lode = copy.deepcopy(lode)
    before_archived = copy.deepcopy(server.archived_lodes)
    before_containment = copy.deepcopy(server.pending_disconnects)

    with (
        patch("hopper.server.git.unpushed_commits", return_value=(count, "origin/main")),
        patch("hopper.server.teardown.close_owned_pane") as close,
        patch("hopper.server.archive_lode_for_action") as archive,
    ):
        server._handle_mutation(_manual_action_message(action_type), submitter)
        _apply_prepared_action(server, lode_id, TEST_RUN_GENERATION)

    response = _decode_mock_response(submitter)
    assert response["accepted"] is False
    assert response["reason"] in {"durability_unknown", "durability_unpushed"}
    assert lode == before_lode
    assert server.archived_lodes == before_archived
    assert server.pending_disconnects == before_containment
    assert source_path.read_bytes() == source_before
    assert actions.load_pending_action(lode_id) is None
    close.assert_not_called()
    archive.assert_not_called()


@pytest.mark.parametrize("safety", ["registered", "started"])
def test_restart_force_consent_is_enforced_at_raw_server_boundary(socket_path, make_lode, safety):
    lode_id = "abcd2345"
    actions.write_run_ownership(_completion_run_ownership(lode_id, TEST_RUN_GENERATION))
    server = Server(socket_path)
    lode = make_lode(
        id=lode_id,
        state="running",
        active=safety == "registered",
        tmux_pane="%1",
        pid=101,
        run_generation=TEST_RUN_GENERATION,
    )
    _mark_stage_started(lode, "mill", safety == "started")
    server.lodes = [lode]
    if safety == "registered":
        owner = _mock_client(server)
        server.lode_clients[lode_id] = owner
        server.client_lodes[owner] = lode_id
        server.client_generations[owner] = TEST_RUN_GENERATION
    before = copy.deepcopy(lode)
    conn = _mock_client(server)

    server._handle_mutation(_manual_action_message("restart"), conn)
    _apply_prepared_action(server, lode_id, TEST_RUN_GENERATION)

    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    expected_reason = (
        "registered_runner_requires_force"
        if safety == "registered"
        else "started_stage_requires_force"
    )
    assert response["reason"] == expected_reason
    assert lode == before
    assert actions.load_pending_action(lode_id) is None


@pytest.mark.parametrize("generation", [None, "b" * 32])
def test_inactive_archive_accepts_no_owner_proof_and_publishes_one_receipt(
    socket_path, make_lode, generation
):
    lode_id = "abcd2345"
    server = Server(socket_path)
    lode = make_lode(id=lode_id, state="running", active=False, run_generation=generation)
    server.lodes = [lode]
    conn = _mock_client(server)

    with patch("hopper.server.git.unpushed_commits") as durability:
        server._handle_mutation(_manual_action_message("archive", generation=generation), conn)
        _apply_prepared_action(server, lode_id, generation)

    durability.assert_not_called()
    assert server.lodes == []
    assert len(server.archived_lodes) == 1
    archived = server.archived_lodes[0]
    assert archived["archive_action_id"] == "d" * 32
    assert archived["pending_action"] is None
    assert archived["action_results"][-1]["terminal_disposition"] == "archived"
    assert actions.load_pending_action(lode_id) is None
    response = _decode_mock_response(conn)
    assert response["accepted"] is True
    assert response["outcome"] == "completed"

    retry = _mock_client(server)
    archived_before = copy.deepcopy(archived)
    server._handle_mutation(_manual_action_message("archive", generation=generation), retry)
    retry_response = _decode_mock_response(retry)
    assert retry_response["outcome"] == "idempotent"
    assert retry_response["reason"] == "already_completed"
    assert retry_response["disposition"] == "archived"
    assert archived == archived_before
    assert lode_id not in server.action_acceptances


def test_terminal_oom_archive_requires_absent_exact_scope_and_missing_worktree(
    socket_path, make_lode, tmp_path
):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    lode = make_lode(
        id=lode_id,
        state="error",
        active=False,
        run_generation=generation,
        failure_kind="oom",
        oom_scope=hopper_server.oom.scope_unit_name(lode_id, generation),
    )
    server = Server(socket_path)
    server.lodes = [lode]
    conn = _mock_client(server)
    missing_worktree = tmp_path / "missing-worktree"

    with (
        patch(
            "hopper.server.resolve_worktree_path",
            return_value={"path": missing_worktree, "basis": "recorded", "reason": None},
        ),
        patch("hopper.server.oom.find_systemctl", return_value="systemctl"),
        patch(
            "hopper.server.oom.read_scope_control_group",
            return_value={"state": "absent", "error": None},
        ) as scope,
        patch("hopper.server.git.unpushed_commits") as durability,
    ):
        server._handle_mutation(_manual_action_message("archive", generation=generation), conn)
        _apply_prepared_action(server, lode_id, generation)

    scope.assert_called_once_with(
        "systemctl", hopper_server.oom.scope_unit_name(lode_id, generation)
    )
    durability.assert_not_called()
    assert server.lodes == []
    assert len(server.archived_lodes) == 1
    assert _decode_mock_response(conn)["outcome"] == "completed"


def test_terminal_oom_archive_refuses_when_scope_is_still_present(socket_path, make_lode, tmp_path):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    lode = make_lode(
        id=lode_id,
        state="error",
        active=False,
        run_generation=generation,
        failure_kind="oom",
        oom_scope=hopper_server.oom.scope_unit_name(lode_id, generation),
    )
    server = Server(socket_path)
    server.lodes = [lode]
    conn = _mock_client(server)

    with (
        patch(
            "hopper.server.resolve_worktree_path",
            return_value={
                "path": tmp_path / "missing-worktree",
                "basis": "recorded",
                "reason": None,
            },
        ),
        patch("hopper.server.oom.find_systemctl", return_value="systemctl"),
        patch(
            "hopper.server.oom.read_scope_control_group",
            return_value={"state": "present", "error": None},
        ),
    ):
        server._handle_mutation(_manual_action_message("archive", generation=generation), conn)
        _apply_prepared_action(server, lode_id, generation)

    response = _decode_mock_response(conn)
    assert response["outcome"] == "refused"
    assert response["reason"] == "ownership_unavailable"
    assert "OOM scope is not proven absent" in response["detail"]
    assert server.lodes == [lode]
    assert server.archived_lodes == []
    assert actions.load_pending_action(lode_id) is None


def test_terminal_oom_archive_refuses_when_worktree_still_exists(socket_path, make_lode, tmp_path):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    lode = make_lode(
        id=lode_id,
        state="error",
        active=False,
        run_generation=generation,
        failure_kind="oom",
        oom_scope=hopper_server.oom.scope_unit_name(lode_id, generation),
    )
    server = Server(socket_path)
    server.lodes = [lode]
    conn = _mock_client(server)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with (
        patch(
            "hopper.server.resolve_worktree_path",
            return_value={"path": worktree, "basis": "recorded", "reason": None},
        ),
        patch("hopper.server.oom.find_systemctl", return_value="systemctl"),
        patch("hopper.server.oom.read_scope_control_group") as scope,
    ):
        server._handle_mutation(_manual_action_message("archive", generation=generation), conn)
        _apply_prepared_action(server, lode_id, generation)

    scope.assert_not_called()
    response = _decode_mock_response(conn)
    assert response["outcome"] == "refused"
    assert response["reason"] == "ownership_unavailable"
    assert f"worktree still exists at {worktree}" in response["detail"]
    assert server.lodes == [lode]
    assert server.archived_lodes == []


def test_async_manual_action_response_keeps_exchange_id_over_real_socket(
    server, socket_path, make_lode
):
    """The client must accept a terminal response emitted by a later server event."""
    lode = make_lode(id="abcd2345", state="running", active=False, run_generation=None)
    server.lodes[:] = [lode]
    save_lodes(server.lodes)

    response = submit_lode_action(
        socket_path,
        action_id="d" * 32,
        lode_id=lode["id"],
        expected_generation=None,
        action_type="archive",
        target_disposition="archived",
        force_consent=False,
        stage="mill",
        timeout=2,
    )

    assert response is not None
    assert response["outcome"] == "completed"
    assert response["action_id"] == "d" * 32
    assert response["expected_generation"] is None


def test_async_manual_preparation_refusal_keeps_exchange_id_over_real_socket(
    server, socket_path, make_lode
):
    generation = TEST_RUN_GENERATION
    lode = make_lode(
        id="abcd2345",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
    )
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    actions.write_run_ownership(_completion_run_ownership(lode["id"], generation))

    with patch("hopper.server.git.unpushed_commits", return_value=(1, "origin/main")):
        response = submit_lode_action(
            socket_path,
            action_id="d" * 32,
            lode_id=lode["id"],
            expected_generation=generation,
            action_type="archive",
            target_disposition="archived",
            force_consent=False,
            stage="mill",
            timeout=2,
        )

    assert response is not None
    assert response["outcome"] == "refused"
    assert response["reason"] == "durability_unpushed"
    assert response["action_id"] == "d" * 32


def test_async_manual_blocked_response_keeps_exchange_id_over_real_socket(
    server, socket_path, make_lode, monkeypatch
):
    generation = TEST_RUN_GENERATION
    lode = make_lode(
        id="abcd2345",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
    )
    server.lodes[:] = [lode]
    save_lodes(server.lodes)
    actions.write_run_ownership(_completion_run_ownership(lode["id"], generation))

    def block_first_step(record: dict, marker_name: str, phase: str) -> None:
        actions.transition_marker(record, marker_name, "intent")
        record["phase"] = phase
        server._persist_action(record, via=f"action_intent:{marker_name}")
        server._block_action(record, marker_name, "ownership", "injected inspection failure")

    monkeypatch.setattr(server, "_schedule_action_step", block_first_step)
    response = submit_lode_action(
        socket_path,
        action_id="d" * 32,
        lode_id=lode["id"],
        expected_generation=generation,
        action_type="pause",
        target_disposition="paused",
        force_consent=False,
        stage="mill",
        timeout=2,
    )

    assert response is not None
    assert response["outcome"] == "blocked"
    assert response["reason"] == "ownership_blocked"
    assert response["action_id"] == "d" * 32


def test_pause_publishes_terminal_identity_only_after_empty_proof(socket_path, make_lode):
    record = _post_containment_manual_record("pause")
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        state="teardown",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
        runs={"mill": {"started_at": 1000}},
    )
    server.lodes = [lode]
    conn = _mock_client(server)
    server.action_waiters[record["action_id"]] = [(conn, None)]

    server._continue_action(record)

    assert lode["state"] == "paused"
    assert lode["active"] is False
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert lode["run_generation"] == record["expected_generation"]
    assert lode["runs"]["mill"]["stopped_at"] >= 1000
    assert lode["action_results"][-1]["containment_proof"] == record["containment"]["proof_label"]
    assert actions.load_pending_action(lode["id"]) is None
    assert _decode_mock_response(conn)["disposition"] == "paused"


def test_restart_resets_stage_then_records_one_successor_spawn(socket_path, make_lode):
    record = _post_containment_manual_record("restart", force=True)
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage="mill",
        state="teardown",
        active=False,
        tmux_pane="%1",
        pid=101,
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    _mark_stage_started(lode, "mill")
    old_session = lode_stage_session(lode, "mill")["provider_session_id"]
    server.lodes = [lode]

    with (
        patch.object(server, "_schedule_action_step") as schedule,
        patch.object(server, "_gated_spawn") as ordinary_spawn,
    ):
        server._continue_action(record)

    assert lode_stage_session(lode, "mill")["started"] is False
    assert lode_stage_session(lode, "mill")["provider_session_id"] != old_session
    assert record["markers"]["containment"]["state"] == "done"
    assert record["markers"]["lode_mutation"]["state"] == "done"
    assert record["markers"]["spawn"]["state"] == "intent"
    assert record["spawn"]["target_lode_id"] == lode["id"]
    assert lode["run_generation"] == record["spawn"]["target_generation"]
    schedule.assert_called_once_with(record, "spawn", "spawning")
    ordinary_spawn.assert_not_called()


@pytest.mark.parametrize(
    ("action_type", "force", "count", "completed"),
    [
        ("kill", False, 0, True),
        ("kill", False, 1, False),
        ("kill", False, None, False),
        ("kill", True, None, True),
        ("archive", False, 0, True),
        ("archive", False, 1, False),
        ("archive", False, None, False),
    ],
)
def test_archive_publication_requires_post_empty_durability_recheck(
    socket_path, make_lode, action_type, force, count, completed
):
    record = _post_containment_manual_record(action_type, force=force)
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        state="teardown",
        active=False,
        tmux_pane="%1",
        pid=101,
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    server.lodes = [lode]
    conn = _mock_client(server)
    server.action_waiters[record["action_id"]] = [(conn, None)]

    with (
        patch("hopper.server.git.unpushed_commits", return_value=(count, "origin/main")) as probe,
        patch("hopper.server.remove_worktree") as remove,
        patch("hopper.server.delete_branch") as delete,
    ):
        server._continue_action(record)
        thread = server.action_threads[(record["action_id"], "rechecking_durability")]
        thread.join(timeout=2)
        assert not thread.is_alive()
        internal, response_conn = server.event_queue.get(timeout=2)
        server._handle_mutation(internal, response_conn)

    if force:
        probe.assert_not_called()
    else:
        probe.assert_called_once()
    remove.assert_not_called()
    delete.assert_not_called()
    response = _decode_mock_response(conn)
    if completed:
        assert response["outcome"] == "completed"
        assert server.lodes == []
        assert len(server.archived_lodes) == 1
        assert (
            server.archived_lodes[0]["action_results"][-1]["terminal_disposition"]
            == record["target_disposition"]
        )
        assert actions.load_pending_action(record["lode_id"]) is None
    else:
        assert response["outcome"] == "blocked"
        assert server.archived_lodes == []
        blocked = actions.load_pending_action(record["lode_id"])
        assert blocked["phase"] == "durability_blocked"
        assert blocked["containment"]["state"] == "proven"


def test_startup_never_reuses_precrash_durability_proof_before_archive(socket_path, make_lode):
    record = _post_containment_manual_record("archive")
    record["durability"]["final"] = {
        "outcome": "safe",
        "count": 0,
        "basis": "origin/main",
        "error": None,
        "checked_at_ms": 2_000,
    }
    _complete_marker(record, "durability_recheck")
    actions.write_pending_action(record)
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        state="teardown",
        active=False,
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    server.lodes = [lode]

    with patch.object(server, "_schedule_action_step") as schedule:
        server._resume_action(lode["id"], startup=True)

    resumed = actions.load_pending_action(lode["id"])
    assert resumed["markers"]["durability_recheck"] == actions.new_marker()
    assert resumed["durability"]["final"]["outcome"] == "not_required"
    schedule.assert_called_once_with(resumed, "durability_recheck", "rechecking_durability")
    assert server.archived_lodes == []


@pytest.mark.parametrize("action_type", ["pause", "restart", "kill", "archive"])
@pytest.mark.parametrize("failure_point", ["inspection", "kill"])
def test_manual_action_containment_failure_preserves_identity_and_exact_recovery(
    socket_path, make_lode, temp_config, action_type, failure_point
):
    force = action_type == "restart"
    record = _pending_manual_containment_record(
        action_type, failure_point=failure_point, force=force
    )
    lode = make_lode(
        id=record["lode_id"],
        project="project-one",
        branch="hopper-abcd2345",
        stage="mill",
        state="teardown",
        active=True,
        tmux_pane="%1",
        pid=101,
        oom_scope=record["ownership"]["unit"]["name"],
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    original_session = lode_stage_session(lode, "mill")["provider_session_id"]
    worktree = temp_config / "worktrees" / lode["id"]
    worktree.mkdir(parents=True)
    artifact = worktree / "retained.txt"
    artifact.write_text("accepted work\n")
    server = Server(socket_path)
    server.lodes = [lode]
    blocked_conn = _mock_client(server)
    server.action_waiters[record["action_id"]] = [(blocked_conn, None)]
    containment = copy.deepcopy(record["containment"])
    containment.update(
        state="grace" if failure_point == "inspection" else "kill_pending",
        last_cgroup_observation=("cannot-tell" if failure_point == "inspection" else "populated"),
        last_supervisor_observation=("cannot-tell" if failure_point == "inspection" else "alive"),
        last_owned_process_count=None,
        result=None,
        proof_label=None,
        last_error=(
            "strict Linux containment is ambiguous"
            if failure_point == "inspection"
            else "strict Linux force verification budget expired"
        ),
    )
    phase = "observing_containment" if failure_point == "inspection" else "force_killing"

    with (
        patch.object(server, "_spawn_action_successor") as spawn,
        patch("hopper.server.archive_lode_for_action") as archive,
    ):
        server._handle_action_step_result(
            _action_step_result(
                record,
                phase=phase,
                result={
                    "ok": False,
                    "containment": containment,
                    "error": containment["last_error"],
                },
            )
        )

    blocked = actions.load_pending_action(lode["id"])
    assert blocked is not None
    assert blocked["phase"] == "containment_blocked"
    expected_cursor = "grace" if failure_point == "inspection" else "kill_pending"
    assert blocked["containment"]["state"] == expected_cursor
    assert blocked["containment"]["proof_label"] is None
    assert blocked["containment"]["result"] is None
    assert blocked["containment"]["last_cgroup_observation"] in {
        "cannot-tell",
        "populated",
    }
    assert blocked["ownership"] == record["ownership"]
    assert lode["tmux_pane"] == "%1"
    assert lode["pid"] == 101
    assert lode["oom_scope"] == record["ownership"]["unit"]["name"]
    assert lode["branch"] == "hopper-abcd2345"
    assert lode_stage_session(lode, "mill")["provider_session_id"] == original_session
    assert artifact.read_text() == "accepted work\n"
    assert blocked["result"] is None
    assert blocked["markers"]["lode_mutation"]["state"] == "not_started"
    assert blocked["markers"]["archive"]["state"] == "not_started"
    assert blocked["markers"]["spawn"]["state"] == "not_started"
    assert server.archived_lodes == []
    spawn.assert_not_called()
    archive.assert_not_called()
    blocked_response = _decode_mock_response(blocked_conn)
    assert blocked_response["outcome"] == "blocked"
    expected_command = f"hop lode {action_type} {lode['id']}"
    if force and action_type != "archive":
        expected_command += " --force"
    assert blocked_response["recovery_command"] == expected_command
    assert blocked["recovery"]["command"] == expected_command
    assert blocked_response["action_id"] == blocked["action_id"]
    assert blocked_response["expected_generation"] == blocked["expected_generation"]
    assert blocked_response["disposition"] == blocked["target_disposition"]
    assert blocked_response["containment"]["state"] == expected_cursor
    assert blocked_response["preserved"]["worktree"] is True
    assert blocked_response["preserved"]["branch"] is True
    assert expected_command in blocked_response["status"]

    retry_conn = _mock_client(server)
    with patch.object(server, "_schedule_action_step") as schedule:
        server._handle_mutation(_manual_action_message(action_type, force=force), retry_conn)

    retry_record = schedule.call_args.args[0]
    assert actions.record_binding(retry_record) == actions.record_binding(blocked)
    assert retry_record["action_id"] == blocked["action_id"]
    assert schedule.call_args.args[1:] == (
        "containment",
        "force_killing" if failure_point == "kill" else "observing_containment",
    )


def test_forced_restart_cannot_publish_or_spawn_before_empty_proof(socket_path, make_lode):
    record = _pending_manual_containment_record("restart", failure_point="inspection", force=True)
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        state="teardown",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    _mark_stage_started(lode, "mill")
    before = copy.deepcopy(lode)
    server.lodes = [lode]

    with (
        patch.object(server, "_apply_manual_lode_mutation") as mutate,
        patch.object(server, "_spawn_action_successor") as spawn,
    ):
        server._continue_action(record)

    assert lode == before
    mutate.assert_not_called()
    spawn.assert_not_called()


def test_containment_retry_preserves_the_remaining_waiting_budget(socket_path):
    record = _pending_manual_containment_record("pause", failure_point="inspection")
    actions.transition_marker(
        record,
        "containment",
        "blocked",
        attempt_id=record["markers"]["containment"]["attempt_id"],
        detail="transient ambiguity",
    )
    record["phase"] = "containment_blocked"
    record["containment"]["last_error"] = "transient ambiguity"
    record["recovery"] = {
        "kind": "containment",
        "message": "transient ambiguity",
        "command": actions.recovery_command(record, "containment"),
    }
    actions.write_pending_action(record)
    budget = (
        record["containment"]["started_monotonic_ns"],
        record["containment"]["deadline_monotonic_ns"],
    )
    server = Server(socket_path)

    with patch.object(server, "_schedule_action_step") as schedule:
        server._retry_action(record["lode_id"], None)

    retry = schedule.call_args.args[0]
    assert schedule.call_args.args[1:] == ("containment", "observing_containment")
    assert (
        retry["containment"]["started_monotonic_ns"],
        retry["containment"]["deadline_monotonic_ns"],
    ) == budget
    assert retry["containment"]["last_error"] is None


@pytest.mark.parametrize("record_kind", ["completion", "restart"])
def test_real_capture_adopts_recorded_ownership_without_live_probes(
    tmp_path, socket_path, record_kind
):
    """AC1: completion and restart capture adopt without consulting the host."""
    if record_kind == "completion":
        record = _blocked_capture_completion_record()
    else:
        fixture_bytes = (
            PENDING_ACTION_FIXTURE_DIR / "wedged-restart-ownership-blghq7to.json"
        ).read_bytes()
        lode_id = json.loads(fixture_bytes)["lode_id"]
        target = tmp_path / "lodes" / lode_id / "pending-completion.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fixture_bytes)
        record = actions.load_pending_action(lode_id)
    server = Server(socket_path)

    captured = _capture_without_live_probes(server, record)

    assert captured["result"]["error"] is None
    assert captured["result"]["ok"] is True
    assert captured["result"]["ownership"]["captured"] is True
    assert isinstance(captured["result"]["ownership"]["captured_at_ms"], int)
    descriptor_keys = {
        "pidfd",
        "pidfd_owned",
        "cgroup_fd",
        "cgroup_fd_owned",
        "pane_root_pidfd",
        "pane_root_pidfd_owned",
    }
    assert descriptor_keys.isdisjoint(captured["result"])

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
        closed = _join_action_step(server, record, "closing_pane")
    assert closed["result"]["error"] == "test stop"


def test_pane_close_discovery_seeds_from_recorded_process_group(socket_path):
    """AC2: an escaped same-PGID process enters the owned set at closure."""
    record = _blocked_pane_close_record(_completion_run_ownership())
    ownership = record["ownership"]
    escaped = _completion_process(150, 999, ownership["process_group"])
    root_pids = {
        ownership["pane"]["root_process"]["pid"],
        ownership["supervisor"]["pid"],
        ownership["worker"]["pid"],
    }
    recorded_descendant_pids = {item["pid"] for item in ownership["descendants"]}
    assert escaped["pgid"] == ownership["process_group"]
    assert escaped["pid"] not in recorded_descendant_pids
    assert escaped["ppid"] not in root_pids
    table = _root_free_process_table(ownership, [escaped])
    server = Server(socket_path)

    with (
        patch("hopper.server.teardown.read_boot_id", return_value=record["boot_id"]),
        patch("hopper.server.teardown.read_process_table", return_value=table),
        patch(
            "hopper.server.teardown.read_process_identity",
            return_value={"state": "alive", "identity": escaped, "error": None},
        ),
        patch(
            "hopper.server.teardown.close_owned_pane",
            return_value={"state": "cannot-tell", "error": "test stop"},
        ),
    ):
        server._retry_action(record["lode_id"], None)
        closed = _join_action_step(server, record, "closing_pane")

    assert closed["result"]["ok"] is False
    assert escaped in closed["result"]["descendants"]
    server._handle_action_step_result(closed)
    durable = actions.load_pending_action(record["lode_id"])
    assert escaped in durable["ownership"]["descendants"]


@pytest.mark.parametrize("failure_mode", ["incomplete", "ambiguous"])
@pytest.mark.parametrize("proof_mode", ["linux-degraded", "linux-strict"])
def test_pane_close_discovery_failure_uses_platform_policy(
    socket_path, caplog, failure_mode, proof_mode
):
    """AC5: bounded discovery blocks while strict containment warns and proceeds."""
    ownership = (
        _strict_completion_run_ownership()
        if proof_mode == "linux-strict"
        else _completion_run_ownership()
    )
    record = _blocked_pane_close_record(ownership)
    if failure_mode == "incomplete":
        table = _root_free_process_table(
            ownership,
            [],
            state="unknown",
            error="process table unavailable",
        )
        process_observation = {"state": "gone", "identity": None, "error": None}
        detail = "process table unavailable"
    else:
        candidate = _completion_process(150, 999, ownership["process_group"])
        table = _root_free_process_table(ownership, [candidate])
        process_observation = {
            "state": "cannot-tell",
            "identity": None,
            "error": "birth unavailable",
        }
        detail = "descendant identity became ambiguous during owned-set discovery"
    server = Server(socket_path)

    with (
        patch("hopper.server.teardown.read_boot_id", return_value=record["boot_id"]),
        patch("hopper.server.teardown.read_process_table", return_value=table),
        patch(
            "hopper.server.teardown.read_process_identity",
            return_value=process_observation,
        ),
        patch(
            "hopper.server.teardown.close_owned_pane",
            return_value={"state": "gone", "error": None},
        ) as close_pane,
    ):
        with caplog.at_level(logging.WARNING, logger="hopper.server"):
            server._retry_action(record["lode_id"], None)
            closed = _join_action_step(server, record, "closing_pane")

    if proof_mode != "linux-strict":
        assert closed["result"] == {
            "ok": False,
            "error": f"owned process enumeration before pane close failed: {detail}",
        }
        close_pane.assert_not_called()
        server._handle_action_step_result(closed)
        durable = actions.load_pending_action(record["lode_id"])
        assert durable["markers"]["ownership_capture"]["state"] == "done"
        assert durable["markers"]["pane_close"]["state"] == "blocked"
        assert durable["phase"] == "containment_blocked"
        assert durable["recovery"]["kind"] == "ownership"
        return

    assert closed["result"] == {"ok": True, "error": None}
    assert "descendants" not in closed["result"]
    close_pane.assert_called_once()
    assert any(
        "Owned process enumeration failed before pane close; strict containment will continue"
        in message
        and detail in message
        for message in caplog.messages
    )
    with (
        patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
        patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
        patch(
            "hopper.server.teardown.reopen_process_pidfd",
            return_value={"state": "gone", "fd": None, "error": None},
        ),
        patch("hopper.server.teardown._opened_cgroup", return_value=(None, "absent")),
        patch("hopper.server.oom.find_systemctl", return_value="/bin/systemctl"),
        patch(
            "hopper.server.oom.read_scope_control_group",
            return_value={"state": "absent", "control_group": None},
        ),
        patch(
            "hopper.server.teardown.observe_bounded_processes",
            side_effect=AssertionError("strict containment consulted descendants"),
        ) as bounded_observer,
        patch.object(
            server,
            "_merge_observed_descendants",
            side_effect=AssertionError("strict containment merged descendants"),
        ) as descendant_merge,
    ):
        server._handle_action_step_result(closed)
        observed = _join_action_step(server, record, "observing_containment")
    assert observed["result"]["containment"]["result"] == "linux-strict-empty"
    with patch.object(server, "_continue_action"):
        server._handle_action_step_result(observed)
    durable = actions.load_pending_action(record["lode_id"])
    assert durable["phase"] == "publishing_terminal"
    assert durable["markers"]["pane_close"]["state"] == "done"
    bounded_observer.assert_not_called()
    descendant_merge.assert_not_called()


@pytest.mark.parametrize("proof_mode", ["linux-degraded", "linux-strict"])
def test_pane_close_cross_boot_discovery_uses_platform_policy(socket_path, caplog, proof_mode):
    """A foreign-boot process cannot enter recorded ownership at pane close."""
    ownership = (
        _strict_completion_run_ownership()
        if proof_mode == "linux-strict"
        else _completion_run_ownership()
    )
    record = _blocked_pane_close_record(ownership)
    recorded_descendants = copy.deepcopy(record["ownership"]["descendants"])
    recorded_boot_id = ownership["pane"]["root_process"]["birth"]["boot_id"]
    current_boot_id = "boot-two"
    assert current_boot_id != recorded_boot_id
    foreign = _completion_process(150, 999, ownership["process_group"])
    foreign["birth"]["boot_id"] = current_boot_id
    assert foreign["pid"] not in {item["pid"] for item in recorded_descendants}
    assert foreign["pgid"] == ownership["process_group"]
    table = _root_free_process_table(ownership, [foreign])
    detail = f"Linux boot identity mismatch: recorded {recorded_boot_id}, current {current_boot_id}"
    server = Server(socket_path)

    with (
        patch("hopper.server.teardown.read_boot_id", return_value=current_boot_id),
        patch("hopper.server.teardown.read_process_table", return_value=table) as table_reader,
        patch(
            "hopper.server.teardown.read_process_identity",
            return_value={"state": "alive", "identity": foreign, "error": None},
        ) as process_reader,
        patch(
            "hopper.server.teardown.close_owned_pane",
            return_value={"state": "gone", "error": None},
        ) as close_pane,
        patch("hopper.server.teardown.read_host_boot_identity", return_value=current_boot_id),
        patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
        patch(
            "hopper.server.teardown.reopen_process_pidfd",
            return_value={"state": "gone", "fd": None, "error": None},
        ),
        patch("hopper.server.teardown._opened_cgroup", return_value=(None, "absent")),
        patch("hopper.server.oom.find_systemctl", return_value="/bin/systemctl"),
        patch(
            "hopper.server.oom.read_scope_control_group",
            return_value={"state": "absent", "control_group": None},
        ),
        caplog.at_level(logging.WARNING, logger="hopper.server"),
    ):
        server._retry_action(record["lode_id"], None)
        closed = _join_action_step(server, record, "closing_pane")
        server._handle_action_step_result(closed)
        observed = (
            _join_action_step(server, record, "observing_containment")
            if proof_mode == "linux-strict"
            else None
        )

    table_reader.assert_not_called()
    process_reader.assert_not_called()
    assert "descendants" not in closed["result"]
    durable = actions.load_pending_action(record["lode_id"])
    assert durable["ownership"]["descendants"] == recorded_descendants
    assert foreign not in durable["ownership"]["descendants"]
    if proof_mode != "linux-strict":
        assert closed["result"] == {
            "ok": False,
            "error": f"owned process enumeration before pane close failed: {detail}",
        }
        close_pane.assert_not_called()
        assert durable["markers"]["ownership_capture"]["state"] == "done"
        assert durable["markers"]["pane_close"]["state"] == "blocked"
        assert durable["recovery"]["kind"] == "ownership"
        return

    assert closed["result"] == {"ok": True, "error": None}
    close_pane.assert_called_once()
    assert any(detail in message for message in caplog.messages)
    assert durable["markers"]["pane_close"]["state"] == "done"
    assert durable["phase"] == "observing_containment"
    assert observed["result"]["containment"]["result"] == "linux-strict-empty"
    with patch.object(server, "_continue_action"):
        server._handle_action_step_result(observed)


@pytest.mark.parametrize("instrument_recovers", [True, False])
def test_pane_close_ownership_retry_repeats_real_discovery(
    socket_path, make_lode, instrument_recovers
):
    """AC6: pane-close ownership retry either completes or blocks again."""
    record = _blocked_pane_close_record(_completion_run_ownership())
    table = _root_free_process_table(record["ownership"], [])
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        state="teardown",
        active=True,
        tmux_pane=record["ownership"]["pane"]["pane_id"],
        pid=record["ownership"]["supervisor"]["pid"],
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    server.lodes = [lode]
    observed_table = (
        table if instrument_recovers else {**table, "state": "unknown", "error": "still rejecting"}
    )

    with (
        patch("hopper.server.teardown.read_boot_id", return_value=record["boot_id"]),
        patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
        patch("hopper.server.teardown.read_process_table", return_value=observed_table),
        patch(
            "hopper.server.teardown.close_owned_pane",
            return_value={"state": "gone", "error": None},
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
    ):
        server._retry_action(record["lode_id"], None)
        closed = _join_action_step(server, record, "closing_pane")
        server._handle_action_step_result(closed)
        if instrument_recovers:
            observed = _join_action_step(server, record, "observing_containment")
            server._handle_action_step_result(observed)

    if instrument_recovers:
        assert actions.load_pending_action(record["lode_id"]) is None
        assert lode["state"] == "paused"
        assert lode["action_results"][-1]["terminal_disposition"] == "paused"
    else:
        # cto-70 carries the fix for persistently rejecting process tables.
        durable = actions.load_pending_action(record["lode_id"])
        assert durable["markers"]["ownership_capture"]["state"] == "done"
        assert durable["markers"]["pane_close"]["state"] == "blocked"
        assert durable["phase"] == "containment_blocked"
        assert durable["recovery"]["kind"] == "ownership"


def test_same_boot_resume_schedules_the_recorded_cursor_without_rearming(socket_path):
    record = _pending_manual_containment_record("pause", failure_point="inspection")
    actions.write_pending_action(record)
    budget = copy.deepcopy(record["containment"])
    server = Server(socket_path)

    with patch.object(server, "_schedule_action_step") as schedule:
        server._resume_action(record["lode_id"])

    schedule.assert_called_once_with(record, "containment", "observing_containment")
    assert record["containment"] == budget


@pytest.mark.parametrize("marker_name", ["containment", "scope_kill", "supervisor_kill"])
@pytest.mark.parametrize("marker_state", ["not_started", "intent", "blocked", "done"])
def test_containment_retry_never_raises_for_any_legal_marker_state(
    socket_path, marker_name, marker_state
):
    record = _pending_manual_containment_record("pause", failure_point="kill")
    for name in ("containment", "scope_kill", "supervisor_kill"):
        record["markers"][name] = actions.new_marker()
        _set_marker_state(record, name, marker_state if name == marker_name else "intent")
    record["phase"] = "containment_blocked"
    record["containment"]["last_error"] = "retry requested"
    record["recovery"] = {
        "kind": "containment",
        "message": "retry requested",
        "command": actions.recovery_command(record, "containment"),
    }
    actions.write_pending_action(record)
    server = Server(socket_path)

    with patch.object(server, "_schedule_action_step"):
        server._retry_action(record["lode_id"], None)

    persisted = actions.load_pending_action(record["lode_id"])
    if marker_state == "done":
        assert persisted["phase"] == "containment_blocked"


def test_continue_entry_requires_an_earned_containment_proof(socket_path):
    record = _pending_completion_record()
    _earn_degraded_containment_proof(record)
    _complete_marker(record, "containment")
    server = Server(socket_path)

    with patch.object(server, "_continue_completion_action") as continuation:
        server._continue_action(record)
    continuation.assert_called_once_with(record)

    record["containment"]["proof_label"] = None
    with patch.object(server, "_continue_completion_action") as continuation:
        server._continue_action(record)
    continuation.assert_not_called()
    blocked = actions.load_pending_action(record["lode_id"])
    assert blocked["phase"] == "containment_blocked"
    assert "proof label is missing" in blocked["recovery"]["message"]


def test_resume_terminal_entry_requires_an_earned_containment_proof(socket_path):
    record = _pending_completion_record()
    _earn_degraded_containment_proof(record)
    for marker_name in ("output_publish", "ownership_capture", "pane_close"):
        _complete_marker(record, marker_name)
    _complete_marker(record, "containment")
    record["phase"] = "publishing_terminal"
    actions.write_pending_action(record)
    server = Server(socket_path)

    with patch.object(server, "_continue_action") as continuation:
        server._resume_action(record["lode_id"])
    continuation.assert_called_once()

    record["containment"]["result"] = None
    actions.write_pending_action(record)
    with patch.object(server, "_continue_action") as continuation:
        server._resume_action(record["lode_id"])
    continuation.assert_not_called()
    blocked = actions.load_pending_action(record["lode_id"])
    assert blocked["phase"] == "containment_blocked"
    assert "result is missing" in blocked["recovery"]["message"]


def test_cleanup_retry_entry_requires_an_earned_containment_proof(socket_path):
    record = _pending_completion_record()
    _earn_degraded_containment_proof(record)
    _complete_marker(record, "containment")
    record["phase"] = "cleanup_blocked"
    record["recovery"] = {
        "kind": "cleanup",
        "message": "cleanup interrupted",
        "command": actions.recovery_command(record, "cleanup"),
    }
    actions.write_pending_action(record)
    server = Server(socket_path)

    with patch.object(server, "_continue_action") as continuation:
        server._retry_action(record["lode_id"], None)
    continuation.assert_called_once()

    record["phase"] = "cleanup_blocked"
    record["containment"]["state"] = "grace"
    record["containment"]["result"] = None
    record["containment"]["proof_label"] = None
    record["recovery"] = {
        "kind": "cleanup",
        "message": "cleanup interrupted",
        "command": actions.recovery_command(record, "cleanup"),
    }
    actions.write_pending_action(record)
    with patch.object(server, "_continue_action") as continuation:
        server._retry_action(record["lode_id"], None)
    continuation.assert_not_called()
    blocked = actions.load_pending_action(record["lode_id"])
    assert "cleanup retry refused" in blocked["recovery"]["message"]


def test_spawn_entry_requires_an_earned_containment_proof():
    record = _spawning_completion_record()
    _earn_degraded_containment_proof(record)
    context = {"target": {"id": record["spawn"]["target_lode_id"]}}

    with patch(
        "hopper.server.actions.load_spawn_receipt", side_effect=OSError("receipt unreadable")
    ):
        positive = Server._spawn_action_successor(record, context)
    assert positive["error"] == "spawn receipt is invalid: receipt unreadable"

    record["containment"]["last_error"] = "stale proof"
    negative = Server._spawn_action_successor(record, context)
    assert negative["error"].startswith("action successor refused:")
    assert "last error is stale proof" in negative["error"]


def test_done_pending_clear_fence_is_not_proof_gated_on_resume(socket_path):
    record = _pending_completion_record()
    for marker_name in ("output_publish", "ownership_capture", "pane_close"):
        _complete_marker(record, marker_name)
    _complete_marker(record, "containment")
    _complete_marker(record, "pending_clear")
    record["phase"] = "complete"
    actions.write_pending_action(record)
    server = Server(socket_path)

    with patch.object(server, "_clear_completed_action") as clear:
        server._resume_action(record["lode_id"])

    clear.assert_called_once_with(record)


def test_restart_recovery_of_failed_completion_keeps_completion_identity(socket_path, make_lode):
    record = _pending_completion_record()
    for marker_name in ("output_publish", "ownership_capture", "pane_close"):
        _complete_marker(record, marker_name)
    actions.transition_marker(record, "containment", "intent", attempt_id="e" * 32)
    actions.transition_marker(
        record,
        "containment",
        "blocked",
        attempt_id="e" * 32,
        detail="containment inspection failed",
    )
    record["phase"] = "containment_blocked"
    record["containment"].update(
        state="blocked",
        last_error="containment inspection failed",
    )
    record["recovery"] = {
        "kind": "containment",
        "message": "containment inspection failed",
        "command": f"hop lode restart {record['lode_id']}",
    }
    actions.write_pending_action(record)
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        state="teardown",
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    before_session = lode_stage_session(lode, record["stage"])["provider_session_id"]
    server.lodes = [lode]
    conn = _mock_client(server)

    with (
        patch.object(server, "_schedule_action_step") as schedule,
        patch.object(server, "_apply_manual_lode_mutation") as restart_stage,
    ):
        server._handle_mutation(
            {
                "type": "lode_action",
                "action_id": record["action_id"],
                "lode_id": record["lode_id"],
                "expected_generation": record["expected_generation"],
                "action_type": record["action_type"],
                "target_disposition": record["target_disposition"],
                "force_consent": record["force_consent"],
                "stage": record["stage"],
                "wait_for_disposition": True,
            },
            conn,
        )

    retry_record = schedule.call_args.args[0]
    assert retry_record["action_type"] == "completion"
    assert retry_record["action_id"] == record["action_id"]
    assert schedule.call_args.args[1:] == ("containment", "observing_containment")
    assert lode_stage_session(lode, record["stage"])["provider_session_id"] == before_session
    restart_stage.assert_not_called()


@pytest.mark.parametrize("action_type", ["pause", "restart", "kill", "archive"])
@pytest.mark.parametrize(
    "crash_point",
    [
        "intent_persistence",
        "pty_close",
        "grace_kill",
        "empty_proof",
        "terminal_state_archive",
        "replacement_spawn",
        "response",
        "pending_clear",
    ],
)
def test_manual_action_fault_matrix_converges_after_fresh_server_reconciliation(
    socket_path, make_lode, temp_config, action_type, crash_point
):
    record = _new_manual_action_record(action_type)
    lode = make_lode(
        id=record["lode_id"],
        project="project-one",
        branch="hopper-abcd2345",
        stage=record["stage"],
        state="running",
        active=True,
        tmux_pane=record["ownership"]["pane"]["pane_id"],
        pid=record["ownership"]["worker"]["pid"],
        run_generation=record["expected_generation"],
        oom_scope=record["ownership"]["unit"]["name"],
    )
    original_session = lode_stage_session(lode, record["stage"])["provider_session_id"]
    worktree = temp_config / "worktrees" / lode["id"]
    worktree.mkdir(parents=True)
    artifact = worktree / "retained.txt"
    artifact.write_text("retained across crash\n")
    first = Server(socket_path)
    first.lodes = [lode]
    save_lodes(first.lodes)
    counters = {"close": 0, "grace": 0, "kill": 0, "spawn": 0, "archive": 0}
    response_payloads = []

    if crash_point == "intent_persistence":
        real_write_pending = actions.write_pending_action

        def write_accepted_then_crash(current):
            real_write_pending(current)
            if current["phase"] == "accepted":
                raise _InjectedActionCrash("after intent persistence")
            return actions.pending_action_path(current["lode_id"])

        prepared = {
            "ok": True,
            "ownership": _strict_completion_run_ownership(),
            "source_digest": record["ownership"]["source_record_sha256"],
            "durability": record["durability"],
            "already_empty": False,
        }
        with (
            patch(
                "hopper.server.actions.write_pending_action",
                side_effect=write_accepted_then_crash,
            ),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            pytest.raises(_InjectedActionCrash),
        ):
            first._open_lode_action(
                action_type=action_type,
                message=_manual_action_message(action_type, force=action_type == "restart"),
                prepared=prepared,
            )
    else:
        actions.write_pending_action(record)
        first._project_action(record, via="test_action_acceptance")
        waiter = _mock_client(first)
        first.action_waiters[record["action_id"]] = [(waiter, None)]
        real_persist = first._persist_action
        real_save_lodes = hopper_server.save_lodes
        real_archive = hopper_server.archive_lode_for_action
        real_send_response = first._send_response
        real_clear = actions.clear_pending_action

        def persist_with_fault(current, *, via):
            real_persist(current, via=via)
            fault_via = {
                "pty_close": "action_result:pane_close",
                "grace_kill": "action_intent:force_killing",
                "empty_proof": "action_result:containment_proven",
            }.get(crash_point)
            if via == fault_via:
                raise _InjectedActionCrash(f"after {crash_point}")

        def save_lodes_with_fault(items):
            real_save_lodes(items)
            if crash_point not in {
                "terminal_state_archive",
                *({"replacement_spawn"} if action_type in {"pause"} else set()),
            }:
                return
            current = next((item for item in items if item["id"] == record["lode_id"]), None)
            if current is None:
                return
            terminal = current["state"] == "paused" or (
                action_type == "restart"
                and lode_stage_session(current, record["stage"])["provider_session_id"]
                != original_session
                and current.get("run_generation") == record["expected_generation"]
            )
            if terminal:
                raise _InjectedActionCrash("after terminal lode state")

        def archive_with_fault(active, archived, lode_id, action_id):
            was_active = any(item["id"] == lode_id for item in active)
            published = real_archive(active, archived, lode_id, action_id)
            if was_active:
                counters["archive"] += 1
            if crash_point in {"terminal_state_archive", "replacement_spawn"}:
                raise _InjectedActionCrash("after terminal archive")
            return published

        def send_with_fault(conn, payload, **kwargs):
            if crash_point == "response":
                response_payloads.append(copy.deepcopy(payload))
                raise _InjectedActionCrash("after response")
            return real_send_response(conn, payload, **kwargs)

        def clear_with_fault(current):
            real_clear(current)
            if crash_point == "pending_clear":
                raise _InjectedActionCrash("after pending clear")

        _install_synchronous_action_workers(
            first,
            counters,
            crash_after_spawn_receipt=(
                action_type == "restart" and crash_point == "replacement_spawn"
            ),
        )
        with (
            patch.object(first, "_persist_action", side_effect=persist_with_fault),
            patch("hopper.server.save_lodes", side_effect=save_lodes_with_fault),
            patch("hopper.server.archive_lode_for_action", side_effect=archive_with_fault),
            patch.object(first, "_send_response", side_effect=send_with_fault),
            patch("hopper.server.actions.clear_pending_action", side_effect=clear_with_fault),
            pytest.raises(_InjectedActionCrash),
        ):
            first._resume_action(record["lode_id"])

    if crash_point == "response":
        assert response_payloads[-1]["outcome"] == "completed"

    restarted = Server(socket_path.with_name("restart.sock"))
    restarted.lodes = hopper_server.load_lodes()
    restarted.archived_lodes = hopper_server.load_archived_lodes()
    pending_before_restart = actions.load_pending_action(record["lode_id"])
    source = restarted._find_lode(record["lode_id"])
    if (
        pending_before_restart is not None
        and source is not None
        and source.get("run_generation") == record["expected_generation"]
    ):
        assert (
            restarted._set_terminal_failure(
                source, "runner_exit_unverified", record["expected_generation"]
            )
            is False
        )
        assert source["failure_kind"] is None

    _install_synchronous_action_workers(restarted, counters)
    real_archive = hopper_server.archive_lode_for_action

    def count_reconciled_archive(active, archived, lode_id, action_id):
        was_active = any(item["id"] == lode_id for item in active)
        published = real_archive(active, archived, lode_id, action_id)
        if was_active:
            counters["archive"] += 1
        return published

    with patch("hopper.server.archive_lode_for_action", side_effect=count_reconciled_archive):
        restarted._reconcile_action_records()
        for lode_id in tuple(restarted._startup_actions):
            restarted._resume_action(lode_id, startup=True)

    matches = [
        item
        for item in [*restarted.lodes, *restarted.archived_lodes]
        if item["id"] == record["lode_id"]
    ]
    assert len(matches) == 1
    settled = matches[0]
    receipts = [
        receipt
        for receipt in settled["action_results"]
        if receipt["action_id"] == record["action_id"]
    ]
    assert len(receipts) == 1
    assert receipts[0]["terminal_disposition"] == record["target_disposition"]
    assert settled["pending_action"] is None
    assert settled["branch"] == "hopper-abcd2345"
    assert settled["failure_kind"] is None
    assert artifact.read_text() == "retained across crash\n"
    assert actions.load_pending_action(record["lode_id"]) is None
    assert counters["close"] == 1
    assert counters["kill"] == 1
    assert counters["spawn"] == (1 if action_type == "restart" else 0)
    assert counters["archive"] == (1 if action_type in {"kill", "archive"} else 0)
    if action_type == "restart":
        assert (
            lode_stage_session(settled, record["stage"])["provider_session_id"] != original_session
        )
        assert receipts[0]["successor"] is not None
    else:
        assert (
            lode_stage_session(settled, record["stage"])["provider_session_id"] == original_session
        )
        assert receipts[0]["successor"] is None
    if action_type in {"kill", "archive"}:
        assert restarted.lodes == []
        assert len(restarted.archived_lodes) == 1
    else:
        assert len(restarted.lodes) == 1
        assert restarted.archived_lodes == []


def test_pending_action_retry_compares_serialized_bound_projection_without_mutation(
    socket_path, make_lode
):
    record = _pending_completion_record()
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        state="teardown",
        run_generation=record["expected_generation"],
    )
    server.lodes = [lode]
    fields = {
        "lode_id": record["lode_id"],
        "expected_generation": record["expected_generation"],
        "action_type": record["action_type"],
        "target_disposition": record["target_disposition"],
        "force_consent": record["force_consent"],
    }
    reordered = {key: fields[key] for key in reversed(tuple(fields))}
    message = {
        "action_id": record["action_id"],
        **json.loads(json.dumps(reordered, indent=2)),
        "stage": record["stage"],
    }
    path = actions.pending_action_path(record["lode_id"])
    before = path.read_bytes()

    result = server._open_lode_action(
        action_type="completion", message=message, prepared={"ok": False}
    )

    assert result["outcome"] == "idempotent"
    assert result["reason"] == "already_accepted"
    assert path.read_bytes() == before
    assert actions.load_pending_action(record["lode_id"])["next_action"] == record["next_action"]

    conflict = server._open_lode_action(
        action_type="completion",
        message={**message, "action_id": "d" * 32},
        prepared={"ok": False},
    )
    mismatch = server._open_lode_action(
        action_type="completion",
        message={**message, "target_disposition": "advance_ship"},
        prepared={"ok": False},
    )

    assert conflict["reason"] == "action_conflict"
    assert mismatch["reason"] == "action_identity_mismatch"
    assert path.read_bytes() == before


def test_evicted_action_retry_refuses_stale_generation_without_reexecution(socket_path, make_lode):
    source = _pending_completion_record()
    actions.pending_action_path(source["lode_id"]).unlink()
    lode = make_lode(
        id=source["lode_id"],
        stage="refine",
        state="running",
        run_generation="f" * 32,
    )
    for index in range(9):
        current = copy.deepcopy(source)
        current["action_id"] = f"{index + 1:032x}"
        actions.append_action_result(
            lode, actions.new_action_result(current, completed_ms=2_000 + index)
        )
    server = Server(socket_path)
    server.lodes = [lode]
    before = copy.deepcopy(lode)
    message = {
        "action_id": f"{1:032x}",
        "lode_id": lode["id"],
        "expected_generation": source["expected_generation"],
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
        "stage": "mill",
    }

    result = server._open_lode_action(
        action_type="completion", message=message, prepared={"ok": False}
    )

    assert result["outcome"] == "refused"
    assert result["reason"] == "stale_expected_generation"
    assert "recovery_command" not in result
    assert lode == before


def test_legacy_completion_wire_is_refusal_only(socket_path, make_lode):
    server = Server(socket_path)
    lode = make_lode(id="abcd2345", state="running", active=True)
    server.lodes = [lode]
    before = copy.deepcopy(lode)
    conn = _mock_client(server)

    server._handle_mutation({"type": "lode_complete", "lode_id": lode["id"]}, conn)

    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == "protocol_upgrade_required"
    assert "Action unbound did not acquire generation none" in response["status"]
    assert "Preserved: worktree, branch, stage session" in response["status"]
    assert "Inspect with: hop lode status abcd2345" in response["status"]
    assert lode == before


def test_strict_completion_refuses_missing_pidfd_interface_before_acceptance(
    socket_path, make_lode, temp_config, monkeypatch
):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    actions.write_run_ownership(_strict_completion_run_ownership(lode_id, generation))
    canonical = temp_config / "lodes" / lode_id / "mill_out.md"
    canonical.write_bytes(b"known good\n")
    server = Server(socket_path)
    server.lodes = [
        make_lode(
            id=lode_id,
            state="running",
            active=True,
            tmux_pane="%1",
            pid=101,
            run_generation=generation,
        )
    ]
    owner = _mock_client(server)
    submitter = _mock_client(server)
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = generation
    output = b"new output\n"
    message = {
        "type": "lode_action",
        "action_id": "a" * 32,
        "lode_id": lode_id,
        "expected_generation": generation,
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
        "stage": "mill",
        "output_base64": base64.b64encode(output).decode("ascii"),
        "byte_length": len(output),
        "digest_algorithm": "sha256",
        "digest_hex": hashlib.sha256(output).hexdigest(),
        "exchange_id": "f" * 32,
    }
    monkeypatch.setattr(hopper_server.teardown, "resolve_pidfd_interface", lambda: None)

    server._handle_lode_action(message, submitter)

    response = _decode_mock_response(submitter)
    assert response["type"] == "lode_action_ack"
    assert response["accepted"] is False
    assert response["reason"] == "pidfd_unavailable"
    assert canonical.read_bytes() == b"known good\n"
    assert actions.load_pending_action(lode_id) is None
    assert lode_id not in server.action_acceptances
    assert not server.registration_threads


def test_exact_output_repair_persists_publication_intent_before_ack(
    socket_path, make_lode, temp_config
):
    record = _blocked_output_record()
    staged = actions.lode_dir(record["lode_id"]) / record["output"]["staged_relative_path"]
    staged.unlink()
    canonical = actions.lode_dir(record["lode_id"]) / record["output"]["canonical_name"]
    canonical.write_bytes(b"known good\n")
    server = Server(socket_path)
    server.lodes = [
        make_lode(
            id=record["lode_id"],
            stage=record["stage"],
            state="teardown",
            run_generation=record["expected_generation"],
        )
    ]
    conn = _mock_client(server)
    entered = threading.Event()
    release = threading.Event()

    def hold_publication(_record):
        entered.set()
        assert release.wait(2)

    with patch("hopper.server.actions.publish_output", side_effect=hold_publication):
        server._repair_completion_output(_repair_output_message(record), conn)
        assert entered.wait(2)
        repaired = actions.load_pending_action(record["lode_id"])
        assert repaired is not None
        assert repaired["phase"] == "publishing_output"
        assert repaired["markers"]["output_publish"]["state"] == "intent"
        assert repaired["output"]["failure"] is None
        assert (
            actions.verify_output_file(
                staged,
                repaired["output"]["byte_length"],
                repaired["output"]["digest_hex"],
            )
            == repaired["output"]["staged_identity"]
        )
        assert canonical.read_bytes() == b"known good\n"
        response = _decode_mock_response(conn)
        assert response["accepted"] is True
        assert response["reason"] == "accepted"
        release.set()
        thread = server.action_threads[(record["action_id"], "publishing_output")]
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing_token", "unauthenticated"),
        ("wrong_token", "unauthenticated"),
        ("wrong_action", "action_mismatch"),
        ("wrong_stage", "action_mismatch"),
        ("wrong_generation", "action_mismatch"),
        ("wrong_next_action", "action_mismatch"),
        ("lode_identity", "lode_identity_mismatch"),
        ("published", "already_published"),
        ("wrong_phase", "no_pending_output_failure"),
        ("malformed_base64", "output_mismatch"),
        ("wrong_length", "output_mismatch"),
        ("wrong_digest", "output_mismatch"),
    ],
)
def test_output_repair_guard_refusals_leave_all_bytes_unchanged(
    socket_path, make_lode, case, reason
):
    record = _blocked_output_record()
    staged = actions.lode_dir(record["lode_id"]) / record["output"]["staged_relative_path"]
    canonical = actions.lode_dir(record["lode_id"]) / record["output"]["canonical_name"]
    canonical.write_bytes(b"known good\n")
    staged_before = staged.read_bytes()
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        state="teardown",
        run_generation=record["expected_generation"],
    )
    server.lodes = [lode]
    message = _repair_output_message(record)
    if case == "missing_token":
        message.pop("token")
    elif case == "wrong_token":
        message["token"] = "wrong"
    elif case == "wrong_action":
        message["action_id"] = "e" * 32
    elif case == "wrong_stage":
        message["stage"] = "refine"
    elif case == "wrong_generation":
        message["expected_generation"] = "e" * 32
    elif case == "wrong_next_action":
        message["next_action"] = {"kind": "advance", "target_stage": "ship"}
    elif case == "lode_identity":
        lode["stage"] = "refine"
    elif case == "published":
        record["output"]["published"] = True
        actions.write_pending_action(record)
    elif case == "wrong_phase":
        record["phase"] = "containment_blocked"
        record["recovery"] = {
            "kind": "containment",
            "message": "still populated",
            "command": f"hop lode restart {record['lode_id']}",
        }
        actions.write_pending_action(record)
    elif case == "malformed_base64":
        message["output_base64"] = "%%%"
    elif case == "wrong_length":
        message["byte_length"] += 1
    elif case == "wrong_digest":
        message["digest_hex"] = "0" * 64
    conn = _mock_client(server)

    with patch.object(server, "_schedule_action_step") as schedule:
        server._repair_completion_output(message, conn)

    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == reason
    assert staged.read_bytes() == staged_before
    assert canonical.read_bytes() == b"known good\n"
    schedule.assert_not_called()


def test_output_repair_without_pending_record_refuses_without_details(socket_path, make_lode):
    server = Server(socket_path)
    server.lodes = [make_lode(id="abcd2345", state="teardown")]
    conn = _mock_client(server)
    message = {
        "type": "lode_repair_output",
        "lode_id": "abcd2345",
        "token": "secret",
    }

    server._repair_completion_output(message, conn)

    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == "no_pending_output_failure"
    assert set(response) == {"type", "accepted", "reason", "ts"}


@pytest.mark.parametrize("msg_type", ["lode_pause", "lode_kill", "lode_archive"])
def test_pending_action_does_not_enable_legacy_manual_wire(socket_path, make_lode, msg_type):
    record = _pending_completion_record()
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        state="teardown",
        run_generation=record["expected_generation"],
    )
    server.lodes = [lode]
    before = copy.deepcopy(lode)
    conn = _mock_client(server)

    server._handle_mutation({"type": msg_type, "lode_id": record["lode_id"]}, conn)

    assert lode == before
    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == "protocol_upgrade_required"
    assert "retired mixed-version control message" in response["status"]
    assert f"Action {record['action_id']} (completion) owns generation" in response["status"]
    assert "Preserved: worktree, branch, stage session" in response["status"]
    assert f"Inspect with: hop lode status {record['lode_id']}" in response["status"]


def test_pending_action_fences_spawn_after_generation_moves(socket_path, make_lode):
    record = _pending_completion_record()
    lode = make_lode(
        id=record["lode_id"],
        stage="refine",
        state="teardown",
        run_generation="f" * 32,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    before = copy.deepcopy(lode)

    with patch("hopper.server.spawn_lode_processor") as spawn:
        outcome, pane_id = server._gated_spawn(lode, "/repo")

    assert outcome is SpawnOutcome.UNKNOWN
    assert pane_id is None
    assert lode == before
    spawn.assert_not_called()
    assert server._generation_has_teardown_intent(lode["id"], "f" * 32) is False


@pytest.mark.parametrize("msg_type", ["lode_spawn", "lode_resume", "lode_resume_refine"])
def test_every_ordinary_spawn_path_uses_lode_wide_action_fence(socket_path, make_lode, msg_type):
    record = _pending_completion_record()
    lode = make_lode(
        id=record["lode_id"],
        project="proj",
        stage="refine",
        state="paused",
        run_generation="f" * 32,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    before = copy.deepcopy(lode)

    with (
        patch("hopper.server.find_project", return_value=Project(path="/repo", name="proj")),
        patch("hopper.server.spawn_lode_processor") as spawn,
    ):
        server._handle_mutation({"type": msg_type, "lode_id": lode["id"]}, None)

    assert lode == before
    spawn.assert_not_called()


def test_legacy_stage_restart_refuses_without_mutation(socket_path, make_lode):
    record = _pending_completion_record()
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        state="teardown",
        run_generation=record["expected_generation"],
    )
    server.lodes = [lode]
    before = copy.deepcopy(lode)
    conn = _mock_client(server)

    server._handle_mutation(
        {
            "type": "lode_reset_claude_stage",
            "lode_id": record["lode_id"],
            "claude_stage": record["stage"],
            "spawn": True,
            "ack_requested": True,
        },
        conn,
    )

    assert lode == before
    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == "protocol_upgrade_required"
    assert "retired mixed-version control message" in response["status"]
    assert f"Action {record['action_id']} (completion) owns generation" in response["status"]
    assert "Preserved: worktree, branch, stage session" in response["status"]
    assert f"Inspect with: hop lode status {record['lode_id']}" in response["status"]


def test_accepted_generation_absorbs_disconnect_result_hold_and_terminal_failure(
    socket_path, make_lode
):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    _pending_completion_record(lode_id, generation)
    server = Server(socket_path)
    lode = make_lode(
        id=lode_id,
        state="teardown",
        status="Teardown: publishing accepted mill output",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
        oom_scope="hopper-test.scope",
    )
    server.lodes = [lode]
    owner = _mock_client(server)
    result_conn = _mock_client(server)
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = generation
    key = (lode_id, generation)
    server.pending_disconnects[key] = {
        "deadline": 0.0,
        "unit_name": "hopper-test.scope",
    }
    server.runner_results[key] = (None, 137)

    with patch.object(server, "_enqueue_event"):
        server._on_client_disconnect(owner)

    assert lode["active"] is False
    assert lode["tmux_pane"] == "%1"
    assert lode["pid"] == 101
    assert key not in server.pending_disconnects
    assert key not in server.runner_results
    assert server._set_terminal_failure(lode, "oom", generation) is False
    assert lode["failure_kind"] is None

    server._handle_lode_run_result(
        {
            "lode_id": lode_id,
            "run_generation": generation,
            "unit_name": "hopper-test.scope",
            "unit_result": "oom-kill",
            "worker_returncode": 137,
        },
        result_conn,
    )
    response = _decode_mock_response(result_conn)
    assert response["disposition"] == "expected-teardown"
    assert response["durable"] is False
    assert lode["failure_kind"] is None

    server.pending_disconnects[key] = {
        "deadline": 0.0,
        "unit_name": "hopper-test.scope",
    }
    server.runner_results[key] = (None, 137)
    with patch("hopper.server.time.monotonic", return_value=1.0):
        server._drain_due_disconnects()
    assert key not in server.pending_disconnects
    assert key not in server.runner_results
    assert lode["failure_kind"] is None


def test_action_step_result_discards_stale_attempt(socket_path, make_lode):
    record = _pending_completion_record()
    actions.transition_marker(record, "output_publish", "intent", attempt_id="d" * 32)
    record["phase"] = "publishing_output"
    actions.write_pending_action(record)
    server = Server(socket_path)
    server.lodes = [
        make_lode(
            id=record["lode_id"],
            state="teardown",
            run_generation=record["expected_generation"],
        )
    ]

    server._handle_action_step_result(
        {
            "lode_id": record["lode_id"],
            "expected_generation": record["expected_generation"],
            "action_id": record["action_id"],
            "phase": "publishing_output",
            "attempt_id": "e" * 32,
            "result": {"ok": True},
        }
    )

    assert actions.load_pending_action(record["lode_id"]) == record


def test_containment_step_exception_closes_handles_opened_before_failure(socket_path):
    record = _pending_manual_containment_record("pause", failure_point="inspection")
    server = Server(socket_path)
    read_fd, write_fd = os.pipe()

    try:
        with (
            patch(
                "hopper.server.teardown.read_host_boot_identity",
                return_value=record["boot_id"],
            ),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            patch(
                "hopper.server.teardown.reopen_process_pidfd",
                side_effect=[
                    {"state": "alive", "fd": read_fd, "error": None},
                    RuntimeError("pane pidfd acquisition failed"),
                ],
            ),
            pytest.raises(RuntimeError, match="pane pidfd acquisition failed"),
        ):
            server._observe_action_containment(record, retained_pidfd=None)

        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


@pytest.mark.parametrize("event_type", ["_action_step_result", "_registration_capture_result"])
@pytest.mark.parametrize("drop_reason", ["queue-full", "server-stopped"])
def test_dropped_worker_result_closes_owned_descriptor(socket_path, event_type, drop_reason):
    server = Server(socket_path)
    server.event_queue = queue.Queue(maxsize=1)
    if drop_reason == "queue-full":
        server.event_queue.put_nowait(({"type": "occupied"}, None))
    else:
        server.stop_event.set()
    read_fd, write_fd = os.pipe()
    result = {"ok": True, "pidfd": read_fd}
    if event_type == "_action_step_result":
        result["pidfd_owned"] = True

    try:
        server._enqueue_event({"type": event_type, "result": result})

        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


def test_stop_waits_for_descriptor_borrowers_before_closing_retained_handles(socket_path):
    server = Server(socket_path)
    read_fd, write_fd = os.pipe()
    key = ("abcd2345", TEST_RUN_GENERATION)
    server.supervisor_pidfds[key] = read_fd
    entered = threading.Event()
    release = threading.Event()

    def borrow_descriptor():
        entered.set()
        assert release.wait(2)
        os.fstat(read_fd)

    worker = threading.Thread(target=borrow_descriptor)
    server.action_threads[("a" * 32, "force_killing")] = worker
    worker.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=server.stop)
    stopper.start()
    assert server.stop_event.wait(2)

    try:
        assert stopper.is_alive()
        os.fstat(read_fd)
        release.set()
        stopper.join(timeout=2)
        assert not stopper.is_alive()
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        release.set()
        worker.join(timeout=2)
        stopper.join(timeout=2)
        os.close(write_fd)


@pytest.mark.parametrize(
    ("remove_staged", "expected_kind"),
    [(False, "publication"), (True, "output")],
)
def test_publication_failure_distinguishes_retryable_staging(
    socket_path, make_lode, remove_staged, expected_kind
):
    record = _pending_completion_record()
    actions.transition_marker(record, "output_publish", "intent", attempt_id="d" * 32)
    record["phase"] = "publishing_output"
    actions.write_pending_action(record)
    if remove_staged:
        staged = actions.lode_dir(record["lode_id"]) / record["output"]["staged_relative_path"]
        staged.unlink()
    server = Server(socket_path)
    server.lodes = [
        make_lode(
            id=record["lode_id"],
            state="teardown",
            run_generation=record["expected_generation"],
        )
    ]

    with patch("hopper.server.actions.publish_output", side_effect=OSError("read-only")):
        server._run_action_step(
            record,
            "output_publish",
            "publishing_output",
            record["markers"]["output_publish"]["attempt_id"],
            None,
            None,
        )
    internal, _conn = server.event_queue.get(timeout=2)
    server._handle_action_step_result(internal)

    blocked = actions.load_pending_action(record["lode_id"])
    assert blocked["recovery"]["kind"] == expected_kind
    if expected_kind == "publication":
        assert blocked["recovery"]["command"] == f"hop lode restart {record['lode_id']}"
        assert actions.pending_output_recovery(blocked) is None
        conn = _mock_client(server)
        with patch.object(server, "_schedule_action_step") as schedule:
            server._retry_action(record["lode_id"], conn)
        schedule.assert_called_once_with(blocked, "output_publish", "publishing_output")
        assert _decode_mock_response(conn)["type"] == "lode_action_retrying"
    else:
        assert blocked["recovery"]["command"] == actions.recovery_command(blocked, "output")
        assert actions.pending_output_recovery(blocked) is not None


def test_force_kill_refuses_without_durable_target_intents(socket_path):
    record = _pending_completion_record()
    record["ownership"]["proof_mode"] = "linux-strict"
    record["ownership"]["unit"] = {
        "name": "hopper-test.scope",
        "load_state": "loaded",
        "control_group": "/user.slice/hopper-test.scope",
    }
    record["ownership"]["cgroup"] = {
        "relative_path": "/user.slice/hopper-test.scope",
        "absolute_path": "/sys/fs/cgroup/user.slice/hopper-test.scope",
        "identity": {"st_dev": 1, "st_ino": 2},
        "boot_id": "boot-one",
    }
    record["containment"].update(
        state="kill_pending",
        last_cgroup_observation="populated",
        last_supervisor_observation="alive",
    )
    server = Server(socket_path)

    result = server._observe_action_containment(record, retained_pidfd=17)

    assert result == {"ok": False, "error": "cgroup kill intent is not durable"}


def test_fresh_force_observation_never_signals_a_target_without_durable_intent(
    socket_path, tmp_path
):
    record = _pending_manual_containment_record("pause", failure_point="inspection")
    record["containment"].update(
        state="kill_pending",
        last_cgroup_observation="empty",
        last_supervisor_observation="gone",
    )
    record["phase"] = "force_killing"
    record["ownership"]["pane"]["root_process"] = copy.deepcopy(record["ownership"]["supervisor"])
    server = Server(socket_path)
    key = (record["lode_id"], record["expected_generation"])
    supervisor_fd, supervisor_write = os.pipe()
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    cgroup_fd = os.open(cgroup, os.O_RDONLY | os.O_DIRECTORY)
    server.supervisor_pidfds[key] = supervisor_fd
    server.cgroup_fds[key] = cgroup_fd
    clock = {"now": record["containment"]["started_monotonic_ns"]}

    def poll(seconds):
        clock["now"] += int(seconds * 1_000_000_000)

    try:
        with (
            patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            patch("hopper.server.teardown.observe_retained_cgroup", return_value="populated"),
            patch("hopper.server.teardown.observe_pidfd", return_value="gone"),
            patch("hopper.server.teardown.kill_cgroup") as kill_cgroup,
        ):
            result = server._observe_action_containment(
                record,
                retained_pidfd=supervisor_fd,
                now_ns=lambda: clock["now"],
                poll=poll,
            )
        kill_cgroup.assert_not_called()
        assert result["containment"]["state"] == "kill_pending"
        assert "durable cgroup kill intent is unavailable" in result["error"]
    finally:
        server._close_containment_handles(record)
        os.close(supervisor_write)


def test_completion_worker_does_not_block_unrelated_ping(socket_path, make_lode):
    record = _pending_completion_record()
    server = Server(socket_path)
    server.lodes = [
        make_lode(
            id=record["lode_id"],
            state="teardown",
            run_generation=record["expected_generation"],
        ),
        make_lode(
            id="efgh2345",
            state="running",
            active=True,
            run_generation="e" * 32,
        ),
    ]
    barrier = threading.Barrier(2)

    def publish(_record):
        barrier.wait(timeout=2)
        barrier.wait(timeout=2)

    with patch("hopper.server.actions.publish_output", side_effect=publish):
        server._schedule_action_step(record, "output_publish", "publishing_output")
        barrier.wait(timeout=2)
        conn = _mock_client(server)
        with patch.object(server, "_send_response") as send:
            server._handle_read_only({"type": "ping"}, conn)
        assert send.call_args.args[1]["type"] == "pong"
        server._handle_mutation(
            {
                "type": "lode_set_title",
                "lode_id": "efgh2345",
                "run_generation": "e" * 32,
                "title": "unrelated control completed",
            },
            None,
        )
        assert server.lodes[1]["title"] == "unrelated control completed"
        barrier.wait(timeout=2)
        thread = server.action_threads[(record["action_id"], "publishing_output")]
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "acceptance_first", [False, True], ids=["result-first", "acceptance-first"]
)
def test_completion_acceptance_and_terminal_result_have_one_serialized_winner(
    socket_path, make_lode, temp_config, acceptance_first
):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    actions.write_run_ownership(_completion_run_ownership(lode_id, generation))
    server = Server(socket_path)
    lode = make_lode(
        id=lode_id,
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
        oom_scope="hopper-test.scope",
    )
    server.lodes = [lode]
    owner = _mock_client(server)
    submitter = _mock_client(server)
    result_conn = _mock_client(server)
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = generation
    output = b"accepted ordering output\n"
    message = {
        "type": "lode_action",
        "action_id": "a" * 32,
        "lode_id": lode_id,
        "expected_generation": generation,
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
        "stage": "mill",
        "output_base64": base64.b64encode(output).decode("ascii"),
        "byte_length": len(output),
        "digest_algorithm": "sha256",
        "digest_hex": hashlib.sha256(output).hexdigest(),
    }
    result = {
        "lode_id": lode_id,
        "run_generation": generation,
        "unit_name": "hopper-test.scope",
        "unit_result": "oom-kill",
        "worker_returncode": 137,
    }
    barrier = threading.Barrier(2)
    real_stage_output = actions.stage_output

    def stage_after_barrier(*args, **kwargs):
        barrier.wait(timeout=2)
        barrier.wait(timeout=2)
        return real_stage_output(*args, **kwargs)

    with patch("hopper.server.actions.stage_output", side_effect=stage_after_barrier):
        server._handle_lode_action(message, submitter)
        barrier.wait(timeout=2)
        if not acceptance_first:
            server._handle_lode_run_result(result, result_conn)
            assert lode["failure_kind"] == "oom"
        barrier.wait(timeout=2)
        thread = server.registration_threads[f"accept:{lode_id}:{generation}"]
        thread.join(timeout=2)
        internal, response_conn = server.event_queue.get(timeout=2)
        assert response_conn is None
        with patch.object(server, "_schedule_action_step"):
            server._handle_mutation(internal, response_conn)

    acceptance = _decode_mock_response(submitter)
    if acceptance_first:
        assert acceptance["accepted"] is True
        server._handle_lode_run_result(result, result_conn)
        assert _decode_mock_response(result_conn)["disposition"] == "expected-teardown"
        assert lode["failure_kind"] is None
        assert actions.load_pending_action(lode_id) is not None
    else:
        assert {
            key: acceptance[key]
            for key in ("type", "accepted", "outcome", "reason", "action_id", "action_type")
        } == {
            "type": "lode_action_ack",
            "accepted": False,
            "outcome": "refused",
            "reason": "terminal_failure",
            "action_id": "a" * 32,
            "action_type": "completion",
        }
        assert acceptance["expected_generation"] == generation
        assert acceptance["preserved"]["worktree"] is True
        assert f"hop lode status {lode_id}" in acceptance["status"]
        assert _decode_mock_response(result_conn)["disposition"] == "oom"
        assert actions.load_pending_action(lode_id) is None
    assert not (temp_config / f"lodes/{lode_id}/mill_out.md").exists()


@pytest.mark.parametrize("action_type", ["pause", "restart", "kill", "archive"])
@pytest.mark.parametrize("result_kind", ["oom", "unverified"])
@pytest.mark.parametrize(
    "intent_first", [False, True], ids=["result-before-intent", "intent-before-result"]
)
def test_manual_action_intent_and_terminal_result_have_one_barrier_serialized_winner(
    socket_path, make_lode, action_type, result_kind, intent_first
):
    lode_id = "abcd2345"
    generation = TEST_RUN_GENERATION
    actions.write_run_ownership(_completion_run_ownership(lode_id, generation))
    server = Server(socket_path)
    lode = make_lode(
        id=lode_id,
        stage="mill",
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=generation,
        oom_scope="hopper-test.scope",
    )
    server.lodes = [lode]
    owner = _mock_client(server)
    submitter = _mock_client(server)
    result_conn = _mock_client(server)
    server.lode_clients[lode_id] = owner
    server.client_lodes[owner] = lode_id
    server.client_generations[owner] = generation
    entered = threading.Event()
    release = threading.Event()
    real_digest = actions.durable_json_sha256

    def digest_after_barrier(path):
        entered.set()
        assert release.wait(2)
        return real_digest(path)

    result = {
        "lode_id": lode_id,
        "run_generation": generation,
        "unit_name": "hopper-test.scope",
        "unit_result": "oom-kill" if result_kind == "oom" else "failed",
        "worker_returncode": 137 if result_kind == "oom" else 1,
    }
    request = _manual_action_message(
        action_type,
        force=action_type == "restart",
    )

    with (
        patch("hopper.server.actions.durable_json_sha256", side_effect=digest_after_barrier),
        patch("hopper.server.git.unpushed_commits", return_value=(0, "origin/main")),
    ):
        server._handle_mutation(request, submitter)
        assert entered.wait(2)
        if not intent_first:
            server._handle_lode_run_result(result, result_conn)
        release.set()
        thread = server.registration_threads[f"accept:{lode_id}:{generation}"]
        thread.join(timeout=2)
        assert not thread.is_alive()
        internal, response_conn = server.event_queue.get(timeout=2)
        with patch.object(server, "_schedule_action_step"):
            server._handle_mutation(internal, response_conn)
        if intent_first:
            server._handle_lode_run_result(result, result_conn)

    result_response = _decode_mock_response(result_conn)
    if intent_first:
        pending = actions.load_pending_action(lode_id)
        assert pending is not None
        assert pending["action_id"] == request["action_id"]
        assert result_response["disposition"] == "expected-teardown"
        assert result_response["durable"] is False
        assert lode["failure_kind"] is None
    else:
        refusal = _decode_mock_response(submitter)
        assert refusal["accepted"] is False
        assert refusal["reason"] in {"terminal_failure", "runner_result_pending"}
        assert actions.load_pending_action(lode_id) is None
        assert result_response["disposition"] == ("oom" if result_kind == "oom" else "unverified")
        assert lode["failure_kind"] == ("oom" if result_kind == "oom" else "runner_exit_unverified")


@pytest.mark.parametrize("action_type", ["pause", "restart", "kill", "archive"])
def test_manual_action_exact_teardown_preserves_every_sibling_and_identity_until_proof(
    socket_path, make_lode, action_type
):
    record = _pending_manual_containment_record(
        action_type,
        failure_point="kill",
        force=action_type == "restart",
    )
    ownership = record["ownership"]
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        active=True,
        tmux_pane=ownership["pane"]["pane_id"],
        pid=ownership["worker"]["pid"],
        run_generation=record["expected_generation"],
        oom_scope=ownership["unit"]["name"],
    )
    server.lodes = [lode]
    lode_before_proof = copy.deepcopy(lode)
    panes = {ownership["pane"]["pane_id"], "%sibling"}
    process_groups = {ownership["process_group"], 900}
    cgroups = {ownership["cgroup"]["relative_path"], "/user.slice/sibling.scope"}
    pidfds = {
        17: ownership["supervisor"]["pid"],
        18: 777,
        19: ownership["pane"]["root_process"]["pid"],
    }
    processes = {
        os.getpid(),
        ownership["supervisor"]["pid"],
        ownership["pane"]["root_process"]["pid"],
        777,
    }

    def close_pane(pane_id):
        assert pane_id == ownership["pane"]["pane_id"]
        panes.remove(pane_id)
        return True

    close_result = hopper_server.teardown.close_owned_pane(
        ownership,
        pane_reader=lambda pane: {
            "pane_id": pane,
            "window_id": ownership["pane"]["window_id"],
            "pane_pid": ownership["pane"]["root_process"]["pid"],
        },
        pane_probe=lambda pane: Liveness.ALIVE if pane in panes else Liveness.GONE,
        process_reader=lambda _pid, **_kwargs: {
            "state": "alive",
            "identity": ownership["pane"]["root_process"],
            "error": None,
        },
        kill=close_pane,
    )
    assert close_result == {"state": "gone", "error": None}

    def kill_cgroup(cgroup, **_kwargs):
        assert cgroup == ownership["cgroup"]
        cgroups.remove(cgroup["relative_path"])
        return {"state": "signalled", "error": None}

    def signal_process(fd, identity, **_kwargs):
        assert pidfds[fd] == identity["pid"]
        pidfds.pop(fd)
        processes.remove(identity["pid"])
        return {"state": "signalled", "error": None}

    key = (record["lode_id"], record["expected_generation"])
    server.supervisor_pidfds[key] = 17
    server.pane_root_pidfds[key] = 19
    server.cgroup_fds[key] = 20
    clock = {"now": 1_000_000_000}

    def poll(seconds):
        clock["now"] += int(seconds * 1_000_000_000)

    unit_states = iter(
        [
            {
                "state": "present",
                "control_group": ownership["cgroup"]["relative_path"],
            },
            {"state": "absent", "control_group": None},
        ]
    )

    with (
        patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
        patch("hopper.server.oom.find_systemctl", return_value="/bin/systemctl"),
        patch(
            "hopper.server.oom.read_scope_control_group",
            side_effect=lambda *_args: next(unit_states),
        ),
        patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
        patch(
            "hopper.server.teardown.observe_retained_cgroup",
            side_effect=lambda *_args, **_kwargs: (
                "populated" if ownership["cgroup"]["relative_path"] in cgroups else "empty"
            ),
        ),
        patch(
            "hopper.server.teardown.observe_pidfd",
            side_effect=lambda fd, **_kwargs: "alive" if fd in pidfds else "gone",
        ),
        patch("hopper.server.teardown.kill_cgroup", side_effect=kill_cgroup),
        patch(
            "hopper.server.teardown.signal_process_pidfd",
            side_effect=signal_process,
        ),
        patch("hopper.server.oom.release_scope", return_value=True) as release_scope,
        patch("hopper.server.os.kill") as numeric_kill,
        patch("hopper.server.os.killpg") as process_group_kill,
    ):
        result = server._observe_action_containment(
            record,
            retained_pidfd=17,
            now_ns=lambda: clock["now"],
            poll=poll,
        )

    assert result["ok"] is True, result
    assert result["containment"]["state"] == "proven"
    assert panes == {"%sibling"}
    assert process_groups == {ownership["process_group"], 900}
    assert cgroups == {"/user.slice/sibling.scope"}
    assert pidfds == {18: 777}
    assert processes == {os.getpid(), 777}
    assert lode == lode_before_proof
    release_scope.assert_called_once_with("/bin/systemctl", ownership["unit"]["name"])
    numeric_kill.assert_not_called()
    process_group_kill.assert_not_called()
    server.supervisor_pidfds.clear()
    server.pane_root_pidfds.clear()
    server.cgroup_fds.clear()


def test_scope_release_residual_never_blocks_proven_terminal_progress(
    socket_path, make_lode, tmp_path
):
    record = _pending_manual_containment_record("pause", failure_point="kill")
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        state="teardown",
        active=True,
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    server.lodes = [lode]
    key = (record["lode_id"], record["expected_generation"])
    supervisor_fd, supervisor_write = os.pipe()
    pane_fd, pane_write = os.pipe()
    cgroup = tmp_path / "scope"
    cgroup.mkdir()
    cgroup_fd = os.open(cgroup, os.O_RDONLY | os.O_DIRECTORY)
    server.supervisor_pidfds[key] = supervisor_fd
    server.pane_root_pidfds[key] = pane_fd
    server.cgroup_fds[key] = cgroup_fd
    clock = {"now": 1_000_000_000, "polls": 0}

    def poll(seconds):
        clock["polls"] += 1
        clock["now"] += int(seconds * 1_000_000_000)

    try:
        with (
            patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            patch("hopper.server.teardown.observe_retained_cgroup", return_value="empty"),
            patch("hopper.server.teardown.observe_pidfd", return_value="gone"),
            patch("hopper.server.oom.find_systemctl", return_value="/bin/systemctl"),
            patch(
                "hopper.server.oom.read_scope_control_group",
                return_value={"state": "cannot-tell", "control_group": None},
            ),
            patch("hopper.server.oom.release_scope") as release,
        ):
            result = server._observe_action_containment(
                record,
                retained_pidfd=supervisor_fd,
                now_ns=lambda: clock["now"],
                poll=poll,
            )
        assert result["ok"] is True
        assert result["containment"]["state"] == "proven"
        assert result["containment"]["result"] == "linux-strict-empty"
        assert result["containment"]["proof_label"].endswith(
            f"scope {record['ownership']['unit']['name']} not released"
        )
        assert clock["polls"] == 600
        release.assert_not_called()

        with patch.object(server, "_continue_action") as continuation:
            server._handle_action_step_result(
                _action_step_result(record, phase="force_killing", result=result)
            )
        continuation.assert_called_once()
        terminal = actions.load_pending_action(record["lode_id"])
        projection = actions.pending_action_projection(terminal)
        assert terminal["phase"] == "publishing_terminal"
        assert record["ownership"]["unit"]["name"] in projection["status"]

        server._continue_action(terminal)
        assert lode["state"] == "paused"
        assert actions.load_pending_action(record["lode_id"]) is None
    finally:
        for fd in (supervisor_write, pane_write):
            os.close(fd)


@pytest.mark.parametrize(
    "process_api",
    ["subprocess.run", "subprocess.Popen", "os.posix_spawn"],
)
def test_strict_containment_cycle_creates_no_external_process_and_observes_pane_root(
    socket_path, tmp_path, process_api
):
    record = _pending_manual_containment_record("pause", failure_point="inspection")
    record["action_type"] = "kill"
    key = (record["lode_id"], record["expected_generation"])
    server = Server(socket_path)
    supervisor_fd, supervisor_write = os.pipe()
    pane_fd, pane_write = os.pipe()
    cgroup = tmp_path / process_api.replace(".", "-")
    cgroup.mkdir()
    cgroup_fd = os.open(cgroup, os.O_RDONLY | os.O_DIRECTORY)
    server.supervisor_pidfds[key] = supervisor_fd
    server.pane_root_pidfds[key] = pane_fd
    server.cgroup_fds[key] = cgroup_fd
    pane_observations = []
    patch_target = f"hopper.server.{process_api}"

    try:
        with (
            patch(
                patch_target, side_effect=AssertionError("external process created")
            ) as forbidden_process,
            patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            patch("hopper.server.teardown.observe_retained_cgroup", return_value="populated"),
            patch(
                "hopper.server.teardown.observe_pidfd",
                side_effect=lambda fd, **_kwargs: pane_observations.append(fd) or "alive",
            ),
        ):
            result = server._observe_action_containment(
                record,
                retained_pidfd=supervisor_fd,
                now_ns=lambda: 1_000_000_000,
                poll=lambda _seconds: None,
            )
        assert result["containment"]["state"] == "kill_pending"
        assert pane_fd in pane_observations
        forbidden_process.assert_not_called()
    finally:
        server._close_containment_handles(record)
        os.close(supervisor_write)
        os.close(pane_write)


def test_absent_cgroup_path_uses_one_restart_reconciliation(socket_path):
    record = _pending_manual_containment_record("pause", failure_point="inspection")
    server = Server(socket_path)
    key = (record["lode_id"], record["expected_generation"])
    supervisor_fd, supervisor_write = os.pipe()
    pane_fd, pane_write = os.pipe()
    server.supervisor_pidfds[key] = supervisor_fd
    server.pane_root_pidfds[key] = pane_fd
    clock = {"now": 1_000_000_000}

    def poll(seconds):
        clock["now"] += int(seconds * 1_000_000_000)

    try:
        with (
            patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            patch("hopper.server.teardown._opened_cgroup", return_value=(None, "absent")),
            patch("hopper.server.oom.find_systemctl", return_value="/bin/systemctl"),
            patch(
                "hopper.server.oom.read_scope_control_group",
                return_value={"state": "absent", "control_group": None},
            ) as reconciliation,
            patch("hopper.server.teardown.observe_pidfd", return_value="alive"),
        ):
            result = server._observe_action_containment(
                record,
                retained_pidfd=supervisor_fd,
                now_ns=lambda: clock["now"],
                poll=poll,
            )
        assert result["containment"]["state"] == "kill_pending"
        reconciliation.assert_called_once_with(
            "/bin/systemctl", record["ownership"]["unit"]["name"]
        )
    finally:
        server._close_containment_handles(record)
        os.close(supervisor_write)
        os.close(pane_write)


@pytest.mark.parametrize("retained_observation", ["empty", "cannot-tell"])
def test_removed_or_replaced_retained_cgroup_is_never_a_force_target(
    socket_path, tmp_path, retained_observation
):
    record = _pending_manual_containment_record("pause", failure_point="kill")
    server = Server(socket_path)
    key = (record["lode_id"], record["expected_generation"])
    supervisor_fd, supervisor_write = os.pipe()
    pane_fd, pane_write = os.pipe()
    cgroup = tmp_path / retained_observation
    cgroup.mkdir()
    cgroup_fd = os.open(cgroup, os.O_RDONLY | os.O_DIRECTORY)
    server.supervisor_pidfds[key] = supervisor_fd
    server.pane_root_pidfds[key] = pane_fd
    server.cgroup_fds[key] = cgroup_fd
    clock = {"now": 1_000_000_000}

    def poll(seconds):
        clock["now"] += int(seconds * 1_000_000_000)

    try:
        with (
            patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
            patch("hopper.server.teardown.resolve_pidfd_interface", return_value={}),
            patch(
                "hopper.server.teardown.observe_retained_cgroup",
                return_value=retained_observation,
            ),
            patch("hopper.server.teardown.observe_pidfd", return_value="gone"),
            patch("hopper.server.teardown.kill_cgroup") as kill_cgroup,
            patch("hopper.server.oom.find_systemctl", return_value="/bin/systemctl"),
            patch(
                "hopper.server.oom.read_scope_control_group",
                return_value={"state": "absent", "control_group": None},
            ),
        ):
            result = server._observe_action_containment(
                record,
                retained_pidfd=supervisor_fd,
                now_ns=lambda: clock["now"],
                poll=poll,
            )
        kill_cgroup.assert_not_called()
        if retained_observation == "empty":
            assert result["containment"]["result"] == "linux-strict-empty"
        else:
            assert result["containment"]["state"] == "kill_pending"
            assert result["containment"]["last_error"] is not None
    finally:
        server._close_containment_handles(record)
        os.close(supervisor_write)
        os.close(pane_write)


def test_degraded_identity_survives_block_step_and_server_restart(socket_path):
    record = _pending_completion_record()
    for marker_name in ("output_publish", "ownership_capture", "pane_close"):
        _complete_marker(record, marker_name)
    actions.transition_marker(record, "containment", "intent")
    record["phase"] = "observing_containment"
    record["ownership"].update(captured=True, captured_at_ms=1_001)
    descendant = copy.deepcopy(record["ownership"]["worker"])
    descendant.update(pid=909, ppid=record["ownership"]["worker"]["pid"], pgid=909)
    descendant["birth"]["value"] = "9090"
    record["ownership"]["descendants"] = [descendant]
    record["containment"] = teardown.start_containment(record, now_ns=lambda: 1_000_000_000)
    actions.write_pending_action(record)
    server = Server(socket_path)
    clock = {"now": 1_000_000_000}

    def poll(seconds):
        clock["now"] += int(seconds * 1_000_000_000)

    with (
        patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
        patch(
            "hopper.server.teardown.read_process_table",
            return_value={"state": "unknown", "identities": [], "error": "table unreadable"},
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
    ):
        result = server._observe_action_containment(
            record,
            retained_pidfd=None,
            now_ns=lambda: clock["now"],
            poll=poll,
        )
    server._handle_action_step_result(
        _action_step_result(record, phase="observing_containment", result=result)
    )

    restarted = Server(socket_path.with_name("restarted.sock"))
    persisted = actions.load_pending_action(record["lode_id"])
    assert persisted["ownership"]["descendants"] == [descendant]
    assert persisted["containment"]["state"] == "grace"
    assert persisted["containment"]["proof_label"] is None
    projection = actions.pending_action_projection(persisted)
    assert "ambiguous" in projection["recovery"]["message"]
    assert "containment: grace" in projection["status"]
    assert restarted._load_action_slot(record["lode_id"])["ownership"]["descendants"] == [
        descendant
    ]


def test_degraded_descendant_merge_releases_pid_reuse_across_observation_cycles(socket_path):
    record = _pending_completion_record()
    old_identity = record["ownership"]["descendants"][0]
    replacement = copy.deepcopy(old_identity)
    replacement["birth"]["value"] = "replacement-birth"
    table = {
        "state": "complete",
        "resolution": "complete",
        "identities": [
            record["ownership"]["pane"]["root_process"],
            record["ownership"]["supervisor"],
            replacement,
        ],
        "error": None,
    }
    clock = {"now": 1_000_000_000}
    record["containment"] = teardown.start_containment(record, now_ns=lambda: clock["now"])
    reads = []

    def poll(_seconds):
        clock["now"] = record["containment"]["deadline_monotonic_ns"]

    server = Server(socket_path)
    with (
        patch("hopper.server.teardown.read_host_boot_identity", return_value=record["boot_id"]),
        patch(
            "hopper.server.teardown.read_process_table",
            side_effect=lambda **_kwargs: reads.append("table") or table,
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
    ):
        result = server._observe_action_containment(
            record,
            retained_pidfd=None,
            now_ns=lambda: clock["now"],
            poll=poll,
        )

    assert reads == ["table", "table"]
    assert result["descendants"] == []
    assert replacement not in result["descendants"]


def test_post_containment_advance_reuses_one_generation_across_reconcile(socket_path, make_lode):
    record = _pending_completion_record()
    _complete_marker(record, "containment")
    record["containment"].update(
        state="proven",
        result="linux-degraded-bounded-empty",
        proof_label="bounded Linux containment observed",
        last_error=None,
    )
    actions.write_pending_action(record)
    server = Server(socket_path)
    server.lodes = [
        make_lode(
            id=record["lode_id"],
            stage="mill",
            state="teardown",
            run_generation=record["expected_generation"],
            tmux_pane=record["ownership"]["pane"]["pane_id"],
            pid=record["ownership"]["worker"]["pid"],
        )
    ]

    with patch.object(server, "_schedule_action_step") as schedule:
        server._continue_action(record)
        generation = record["spawn"]["target_generation"]
        server._continue_action(record)

    assert server.lodes[0]["stage"] == "refine"
    assert server.lodes[0]["run_generation"] == generation
    assert record["markers"]["lode_mutation"]["state"] == "done"
    assert record["markers"]["spawn"]["state"] == "intent"
    assert schedule.call_count == 2
    assert all(item.args[1:] == ("spawn", "spawning") for item in schedule.call_args_list)


def test_completion_spawn_refuses_retained_pane_from_another_generation(socket_path, make_lode):
    record = _pending_completion_record()
    _complete_marker(record, "containment")
    _complete_marker(record, "lode_mutation")
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage="refine",
        state="teardown",
        run_generation="f" * 32,
        tmux_pane=record["ownership"]["pane"]["pane_id"],
    )
    server.lodes = [lode]

    with patch.object(server, "_schedule_action_step") as schedule:
        assert server._prepare_action_spawn(record) is False

    schedule.assert_not_called()
    assert actions.load_pending_action(record["lode_id"])["recovery"]["kind"] == "spawn"


@pytest.mark.parametrize(
    "failure",
    [
        "absent_target",
        "generation_conflict",
        "pane_conflict",
        "invalid_receipt",
        "mismatched_receipt",
    ],
)
def test_action_spawn_preparation_failures_stay_structured_and_action_owned(
    socket_path, make_lode, failure
):
    record = _pending_completion_record()
    _complete_marker(record, "containment")
    _complete_marker(record, "lode_mutation")
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage="refine",
        state="teardown",
        active=False,
        project="proj",
        run_generation=record["expected_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    if failure != "absent_target":
        server.lodes = [lode]
    if failure == "generation_conflict":
        lode["run_generation"] = "f" * 32
    elif failure == "pane_conflict":
        lode["tmux_pane"] = "%99"

    def load_receipt(*args):
        if failure == "invalid_receipt":
            raise ValueError("invalid receipt")
        if failure == "mismatched_receipt":
            return {
                "target_lode_id": "bcde2345",
                "target_generation": "d" * 32,
                "pane_id": "%9",
            }
        return None

    with (
        patch("hopper.server.actions.load_spawn_receipt", side_effect=load_receipt),
        patch.object(server, "_schedule_action_step") as schedule,
    ):
        assert server._prepare_action_spawn(record) is False

    schedule.assert_not_called()
    blocked = actions.load_pending_action(record["lode_id"])
    assert blocked["phase"] == "cleanup_blocked"
    assert blocked["recovery"]["kind"] == "spawn"
    assert blocked["recovery"]["command"] == f"hop lode restart {record['lode_id']}"
    if server.lodes:
        assert lode["state"] == "teardown"
        assert lode["pending_action"]["phase"] == "cleanup_blocked"
        assert lode["state"] not in {"error", "gated"}


@pytest.mark.parametrize(
    "failure",
    [
        "absent_target",
        "missing_project",
        "invalid_receipt",
        "mismatched_receipt",
        "receipt_pane_not_live",
        "inventory_unavailable",
        "tag_without_receipt",
        "spawn_proven_absent",
        "spawn_unknown",
        "bootstrap_receipt_missing",
    ],
)
def test_action_successor_failures_block_only_the_durable_action(socket_path, make_lode, failure):
    record = _spawning_completion_record()
    target = make_lode(
        id=record["lode_id"],
        project="proj",
        stage="refine",
        state="teardown",
        active=False,
        run_generation=record["spawn"]["target_generation"],
        pending_action=actions.pending_action_projection(record),
    )
    server = Server(socket_path)
    if failure != "absent_target":
        server.lodes = [target]

    valid_receipt = {
        "action_id": record["action_id"],
        "source_lode_id": record["lode_id"],
        "target_lode_id": target["id"],
        "target_generation": record["spawn"]["target_generation"],
        "pane_id": "%9",
    }
    receipt_reads = 0

    def load_receipt(*args):
        nonlocal receipt_reads
        receipt_reads += 1
        if failure == "invalid_receipt":
            raise ValueError("invalid receipt")
        if failure == "mismatched_receipt":
            return {**valid_receipt, "action_id": "b" * 32}
        if failure == "receipt_pane_not_live":
            return valid_receipt
        if failure == "bootstrap_receipt_missing":
            return None
        return None

    with (
        patch(
            "hopper.server.find_project",
            return_value=(
                None if failure == "missing_project" else Project(path="/repo", name="proj")
            ),
        ),
        patch("hopper.server.actions.load_spawn_receipt", side_effect=load_receipt),
        patch(
            "hopper.server.completion_action_panes",
            return_value=(
                None
                if failure == "inventory_unavailable"
                else ["%8"]
                if failure == "tag_without_receipt"
                else []
            ),
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch(
            "hopper.server.spawn_lode_processor",
            return_value=(
                (WindowSpawnOutcome.PROVEN_NO_PANE, None)
                if failure == "spawn_proven_absent"
                else (WindowSpawnOutcome.UNKNOWN, None)
                if failure == "spawn_unknown"
                else (WindowSpawnOutcome.SPAWNED, "%9")
            ),
        ),
    ):
        context = server._action_step_context(record, "spawn")
        result = server._spawn_action_successor(record, context)

    assert result["ok"] is False
    if failure == "bootstrap_receipt_missing":
        assert receipt_reads == 2
    server._handle_action_step_result(_action_step_result(record, phase="spawning", result=result))

    blocked = actions.load_pending_action(record["lode_id"])
    assert blocked["phase"] == "cleanup_blocked"
    assert blocked["recovery"]["kind"] == "spawn"
    if server.lodes:
        assert target["state"] == "teardown"
        assert target["pending_action"]["phase"] == "cleanup_blocked"
        assert target["state"] not in {"error", "gated"}


@pytest.mark.parametrize("adoption_path", ["reconcile", "registration"])
def test_incomplete_action_spawn_adoption_remains_bounded_spawning(
    socket_path, make_lode, adoption_path
):
    record = _spawning_completion_record()
    record["spawn"]["pane_id"] = "%9"
    actions.write_pending_action(record)
    lode = make_lode(
        id=record["lode_id"],
        state="teardown",
        active=False,
        run_generation=record["spawn"]["target_generation"],
        tmux_pane="%9",
        pending_action=actions.pending_action_projection(record),
    )
    server = Server(socket_path)
    server.lodes = [lode]

    if adoption_path == "reconcile":
        with (
            patch(
                "hopper.server.actions.load_spawn_receipt",
                return_value={
                    "target_lode_id": lode["id"],
                    "target_generation": record["spawn"]["target_generation"],
                    "pane_id": "%9",
                },
            ),
            patch("hopper.server.actions.load_run_ownership", side_effect=[{}, None]),
        ):
            server._reconcile_spawn_adoption(record)
    else:
        server._record_action_spawn_adoption(
            record["lode_id"], record["spawn"]["target_generation"], "supervisor"
        )

    pending = actions.load_pending_action(record["lode_id"])
    assert pending["phase"] == "spawning"
    assert pending["markers"]["spawn"]["state"] == "intent"
    assert not (pending["spawn"]["supervisor_adopted"] and pending["spawn"]["worker_adopted"])
    assert lode["state"] == "teardown"
    assert lode["pending_action"]["phase"] == "spawning"


def test_completion_spawn_adopts_only_the_fsynced_receipt_pane(socket_path, make_lode):
    record = _pending_completion_record()
    _complete_marker(record, "containment")
    _complete_marker(record, "lode_mutation")
    generation = "e" * 32
    record["spawn"] = {
        "target_lode_id": record["lode_id"],
        "target_generation": generation,
        "receipt_relative_path": f"spawn-{record['action_id']}.json",
        "pane_id": None,
        "supervisor_adopted": False,
        "worker_adopted": False,
    }
    actions.transition_marker(record, "spawn", "intent")
    record["phase"] = "spawning"
    actions.write_pending_action(record)
    actions.write_spawn_receipt(
        {
            "schema_version": 1,
            "action_id": record["action_id"],
            "source_lode_id": record["lode_id"],
            "target_lode_id": record["lode_id"],
            "target_generation": generation,
            "pane_id": "%9",
        }
    )
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        stage="refine",
        state="teardown",
        run_generation=generation,
        tmux_pane=None,
    )
    server.lodes = [lode]

    assert (
        server._adopt_action_spawn_receipt(lode, {"run_generation": generation, "tmux_pane": "%8"})
        is False
    )
    assert (
        server._adopt_action_spawn_receipt(lode, {"run_generation": generation, "tmux_pane": "%9"})
        is True
    )
    assert lode["tmux_pane"] == "%9"
    assert actions.load_pending_action(record["lode_id"])["spawn"]["pane_id"] == "%9"


@pytest.mark.parametrize(
    ("source_coder", "expected_promoted_coder"),
    [
        (None, None),
        (
            {"provider": "grok", "session_id": "source-session"},
            {"provider": "grok", "session_id": None},
        ),
    ],
    ids=["codex", "grok"],
)
@pytest.mark.parametrize("source_driver", ["claude", "codex", "grok"])
def test_ship_action_archives_and_applies_one_recorded_backlog_disposition(
    socket_path, make_lode, monkeypatch, source_coder, expected_promoted_coder, source_driver
):
    record = _pending_completion_record(stage="ship")
    for marker_name in (
        "containment",
        "ship_landing",
        "quarantine_rename",
        "worktree_repair",
        "cleanup_authorization",
    ):
        _complete_marker(record, marker_name)
    record["containment"].update(
        state="proven",
        result="linux-degraded-bounded-empty",
        proof_label="bounded Linux containment observed",
        last_error=None,
    )
    record["ship"]["landing"].update(
        cause="ancestry_contained",
        base_ref="origin/main",
        detail="landed",
        accepted=True,
    )
    actions.write_pending_action(record)
    server = Server(socket_path)
    source_fields = {"coder": source_coder} if source_coder is not None else {}
    server.lodes = [
        make_lode(
            id=record["lode_id"],
            project="proj",
            stage="ship",
            state="teardown",
            run_generation=record["expected_generation"],
            driver=source_driver,
            **source_fields,
        )
    ]
    server.backlog = [
        BacklogItem("first001", "proj", "first", 1, queued=record["lode_id"]),
        BacklogItem("second01", "proj", "second", 2, queued=record["lode_id"]),
    ]
    monkeypatch.setattr(hopper_server, "reserve_lode_id", lambda _lodes: "bcde2345")

    with patch.object(server, "_schedule_action_step") as schedule:
        server._continue_action(record)
        server._continue_action(record)

    assert [lode["id"] for lode in server.archived_lodes] == [record["lode_id"]]
    assert server.archived_lodes[0]["archive_action_id"] == record["action_id"]
    promoted = next(lode for lode in server.lodes if lode["id"] == "bcde2345")
    assert promoted["backlog"]["id"] == "first001"
    if expected_promoted_coder is None:
        assert "coder" not in promoted
    else:
        assert promoted["coder"] == expected_promoted_coder
    assert lode_driver(promoted) == source_driver
    assert [item.id for item in server.backlog] == ["second01"]
    assert server.backlog[0].queued == promoted["id"]
    assert record["ship"]["backlog"]["selected_item_id"] == "first001"
    assert record["markers"]["archive"]["state"] == "done"
    assert record["markers"]["backlog"]["state"] == "done"
    assert schedule.call_count == 2


@pytest.mark.parametrize(
    ("state", "expected_ok"),
    [
        ("deleted", True),
        ("already-absent", True),
        ("retained", True),
        ("anomalous", False),
        ("unknown", False),
    ],
)
def test_ship_branch_cleanup_step_maps_git_outcomes(state, expected_ok):
    record = _pending_completion_record(stage="ship")
    record["ship"]["landing"]["base_ref"] = "origin/main"
    error = "branch retained" if state == "retained" else None
    fact = {"state": state, "error": error}

    with patch("hopper.server.git.delete_branch_if_unchanged", return_value=fact) as delete:
        result = Server._run_ship_cleanup_step(record, "branch_delete")

    delete.assert_called_once_with(record["ship"]["provenance"], base_ref="origin/main")
    assert result == {"ok": expected_ok, "fact": fact, "error": error}


@pytest.mark.parametrize(
    ("git_state", "record_outcome", "expected_detail"),
    [
        ("deleted", "deleted", "deleted"),
        ("already-absent", "already_absent", "already-absent"),
        ("retained", "retained", "not contained in origin/main"),
    ],
)
def test_ship_branch_cleanup_success_records_total_outcome_map(
    socket_path, git_state, record_outcome, expected_detail
):
    record = _pending_completion_record(stage="ship")
    actions.transition_marker(record, "branch_delete", "intent")
    record["phase"] = "quarantining"
    actions.write_pending_action(record)
    error = expected_detail if git_state == "retained" else None
    result = {"ok": True, "fact": {"state": git_state, "error": error}, "error": error}
    server = Server(socket_path)
    message = {
        "type": "_action_step_result",
        "lode_id": record["lode_id"],
        "expected_generation": record["expected_generation"],
        "action_id": record["action_id"],
        "marker_name": "branch_delete",
        "phase": "quarantining",
        "attempt_id": record["markers"]["branch_delete"]["attempt_id"],
        "result": result,
    }

    with patch.object(server, "_continue_action") as continuation:
        server._handle_action_step_result(message)

    persisted = actions.load_pending_action(record["lode_id"])
    assert persisted["ship"]["quarantine"]["branch_outcome"] == record_outcome
    assert persisted["ship"]["cleanup_failure"] is None
    assert persisted["markers"]["branch_delete"]["state"] == "done"
    assert persisted["markers"]["branch_delete"]["detail"] == expected_detail
    continuation.assert_called_once()


def test_retained_ship_branch_cleanup_completes_and_clears_pending_action(socket_path, make_lode):
    record = _pending_completion_record(stage="ship")
    for marker_name in (
        "containment",
        "ship_landing",
        "quarantine_rename",
        "worktree_repair",
        "cleanup_authorization",
        "lode_mutation",
        "archive",
        "backlog",
        "worktree_remove",
    ):
        _complete_marker(record, marker_name)
    record["containment"].update(
        state="proven",
        result="linux-degraded-bounded-empty",
        proof_label="bounded Linux containment observed",
        last_error=None,
    )
    record["ship"]["landing"].update(
        cause="ancestry_contained",
        base_ref="origin/main",
        detail="landed",
        accepted=True,
    )
    record["ship"]["archive_published"] = True
    record["ship"]["quarantine"]["removal_outcome"] = "removed"
    actions.transition_marker(record, "branch_delete", "intent")
    record["phase"] = "quarantining"
    actions.write_pending_action(record)
    archived = make_lode(
        id=record["lode_id"],
        stage="shipped",
        state="teardown",
        run_generation=record["expected_generation"],
        archive_action_id=record["action_id"],
    )
    server = Server(socket_path)
    server.archived_lodes = [archived]
    reason = "accepted branch OID containment in origin/main is not proven"
    completed_facts = {}
    real_clear = server._clear_completed_action

    def capture_completed_record(completed):
        completed_facts.update(
            branch_outcome=completed["ship"]["quarantine"]["branch_outcome"],
            cleanup_failure=completed["ship"]["cleanup_failure"],
            marker_detail=completed["markers"]["branch_delete"]["detail"],
        )
        real_clear(completed)

    message = {
        "type": "_action_step_result",
        "lode_id": record["lode_id"],
        "expected_generation": record["expected_generation"],
        "action_id": record["action_id"],
        "marker_name": "branch_delete",
        "phase": "quarantining",
        "attempt_id": record["markers"]["branch_delete"]["attempt_id"],
        "result": {
            "ok": True,
            "fact": {"state": "retained", "error": reason},
            "error": reason,
        },
    }

    with patch.object(server, "_clear_completed_action", side_effect=capture_completed_record):
        server._handle_action_step_result(message)

    assert actions.load_pending_action(record["lode_id"]) is None
    assert archived["pending_action"] is None
    assert archived["state"] == "ready"
    assert "cleanup blocked" not in archived["status"].lower()
    assert completed_facts == {
        "branch_outcome": "retained",
        "cleanup_failure": None,
        "marker_detail": reason,
    }


@pytest.mark.parametrize("state", ["anomalous", "unknown"])
def test_failed_ship_branch_cleanup_remains_blocked(socket_path, state):
    record = _pending_completion_record(stage="ship")
    actions.transition_marker(record, "branch_delete", "intent")
    record["phase"] = "quarantining"
    actions.write_pending_action(record)
    server = Server(socket_path)
    reason = f"branch deletion is {state}"
    message = {
        "type": "_action_step_result",
        "lode_id": record["lode_id"],
        "expected_generation": record["expected_generation"],
        "action_id": record["action_id"],
        "marker_name": "branch_delete",
        "phase": "quarantining",
        "attempt_id": record["markers"]["branch_delete"]["attempt_id"],
        "result": {
            "ok": False,
            "fact": {"state": state, "error": reason},
            "error": reason,
        },
    }

    server._handle_action_step_result(message)

    persisted = actions.load_pending_action(record["lode_id"])
    assert persisted["phase"] == "cleanup_blocked"
    assert persisted["markers"]["branch_delete"]["state"] == "blocked"
    assert persisted["ship"]["quarantine"]["branch_outcome"] == "retained"
    assert persisted["ship"]["cleanup_failure"] == reason


@pytest.mark.parametrize("disabled", [False, True], ids=["cross-project", "disabled-project"])
def test_ship_backlog_plan_detaches_items_that_cannot_be_promoted(
    socket_path, make_lode, monkeypatch, disabled
):
    record = _pending_completion_record(stage="ship")
    source = make_lode(
        id=record["lode_id"],
        project="proj",
        stage="shipped",
        state="teardown",
        run_generation=record["expected_generation"],
        archive_action_id=record["action_id"],
    )
    item_project = "proj" if disabled else "other"
    queued = BacklogItem("queued01", item_project, "keep me", 1, queued=record["lode_id"])
    server = Server(socket_path)
    server.archived_lodes = [source]
    server.backlog = [queued]
    monkeypatch.setattr(
        hopper_server,
        "find_project",
        lambda _name: Project(path="/repo", name="proj", disabled=disabled),
    )

    assert server._apply_completion_backlog(record) is True

    assert server.backlog == [queued]
    assert queued.queued is None
    assert record["ship"]["backlog"] == {
        "planned": True,
        "selected_item_id": None,
        "promoted_lode_id": None,
        "remaining_item_ids": [queued.id],
        "applied": True,
    }
    assert record["markers"]["backlog"]["state"] == "done"


def test_ship_completion_clear_replaces_teardown_projection_before_removing_record(
    socket_path, make_lode
):
    record = _pending_completion_record(stage="ship")
    archived = make_lode(
        id=record["lode_id"],
        stage="shipped",
        state="teardown",
        status="Teardown: cleaning quarantined worktree",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=record["expected_generation"],
        archive_action_id=record["action_id"],
    )
    server = Server(socket_path)
    server.archived_lodes = [archived]

    server._clear_completed_action(record)

    assert actions.load_pending_action(record["lode_id"]) is None
    assert archived["state"] == "ready"
    assert archived["active"] is False
    assert archived["tmux_pane"] is None
    assert archived["pid"] is None
    assert archived["status"] == (
        "Shipped (bounded Linux teardown; systemd proof and leak-free cleanup unproven)"
    )


def test_ship_landing_worker_calls_canonical_proof_directly(socket_path):
    record = _pending_completion_record(stage="ship")
    server = Server(socket_path)
    verdict = git.ShipLandingVerdict(
        "clean", "contained", "origin/main", "ancestry_contained", "landed"
    )

    with (
        patch("hopper.server.git.ship_landing_verdict", return_value=verdict) as proof,
        patch("hopper.server.git.is_dirty", return_value=False),
        patch("hopper.server.git.unpushed_commits", return_value=(0, "a remote branch")),
    ):
        result = server._prove_ship_landing(record)

    assert result["ok"] is True
    proof.assert_called_once_with(record["ship"]["quarantine"]["original_path"])


def test_ship_landing_budget_runs_off_event_loop(socket_path, make_lode):
    record = _pending_completion_record(stage="ship")
    server = Server(socket_path)
    server.lodes = [
        make_lode(
            id=record["lode_id"],
            stage="ship",
            state="teardown",
            run_generation=record["expected_generation"],
        )
    ]
    entered = threading.Event()
    release = threading.Event()
    verdict = git.ShipLandingVerdict(
        "clean", "contained", "origin/main", "ancestry_contained", "landed"
    )

    def prove(_path):
        entered.set()
        assert release.wait(2)
        return verdict

    with (
        patch("hopper.server.git.ship_landing_verdict", side_effect=prove),
        patch("hopper.server.git.is_dirty", return_value=False),
        patch("hopper.server.git.unpushed_commits", return_value=(0, "a remote branch")),
    ):
        server._schedule_action_step(record, "ship_landing", "proving_ship_landing")
        assert entered.wait(2)
        conn = _mock_client(server)
        with patch.object(server, "_send_response") as send:
            server._handle_read_only({"type": "ping"}, conn)
        assert send.call_args.args[1]["type"] == "pong"
        release.set()
        thread = server.action_threads[(record["action_id"], "proving_ship_landing")]
        thread.join(timeout=2)


def test_startup_completion_reconciliation_projects_before_ordinary_paths(socket_path, make_lode):
    record = _pending_completion_record()
    server = Server(socket_path)
    lode = make_lode(
        id=record["lode_id"],
        state="running",
        active=True,
        tmux_pane="%1",
        pid=101,
        run_generation=record["expected_generation"],
        oom_scope="hopper-test.scope",
    )
    server.lodes = [lode]

    server._reconcile_action_records()

    assert lode["state"] == "teardown"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%1"
    assert lode["pid"] == 101
    assert lode["oom_scope"] == "hopper-test.scope"
    assert server._startup_actions == [record["lode_id"]]
    with patch("hopper.server.pane_liveness") as liveness:
        server._reconcile_startup_lodes()
    liveness.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "action_type", "message"),
    [
        ('{"schema_version": 1}\n', "legacy-v1", "drained before this host is upgraded"),
        ('{"schema_version":', "invalid", "repaired or drained before upgrade"),
    ],
)
def test_startup_invalid_action_slot_stays_visible_and_fenced(
    socket_path, make_lode, payload, action_type, message
):
    lode = make_lode(id="abcd2345", state="running", active=True)
    path = actions.pending_action_path(lode["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)
    server = Server(socket_path)
    server.lodes = [lode]

    server._reconcile_action_records()

    assert path.exists()
    assert lode["state"] == "teardown"
    assert lode["active"] is False
    assert lode["pending_action"]["action_type"] == action_type
    assert message in lode["status"]
    assert server._lode_has_pending_action(lode["id"]) is True


def test_server_lode_snapshot_found_absent_and_ambiguous(socket_path, make_lode):
    active = make_lode(id="same-active", active=True)
    other_active = make_lode(id="active-only", active=True)
    archived = make_lode(id="same-archived", active=False)
    other_archived = make_lode(id="archived-only", active=False)
    server = Server(socket_path)
    server.lodes = [active, other_active]
    server.archived_lodes = [archived, other_archived]
    conn = _mock_client(server)

    with (
        patch.object(server, "_send_response") as send_response,
        patch.object(server, "broadcast") as broadcast,
        patch("hopper.server.save_lodes") as save_active,
    ):
        server._handle_mutation({"type": "lode_snapshot", "prefix": "active-o"}, conn)
        found = send_response.call_args.args[1]
        assert found == {
            "type": "lode_snapshot",
            "result": "found",
            "lode": {**other_active, "archived": False},
        }
        assert found["lode"] is not other_active
        found["lode"]["status"] = "changed"
        assert other_active["status"] == ""
        assert "archived" not in other_active

        send_response.reset_mock()
        server._handle_mutation({"type": "lode_snapshot", "prefix": "archived-o"}, conn)
        archived_response = send_response.call_args.args[1]["lode"]
        assert archived_response["id"] == "archived-only"
        assert archived_response["archived"] is True
        assert "archived_at" not in other_archived
        assert "archived" not in other_archived

        send_response.reset_mock()
        server._handle_mutation({"type": "lode_snapshot", "prefix": "missing"}, conn)
        assert send_response.call_args.args[1] == {"type": "lode_snapshot", "result": "absent"}

        send_response.reset_mock()
        server._handle_mutation({"type": "lode_snapshot", "prefix": "same-"}, conn)
        assert send_response.call_args.args[1] == {
            "type": "lode_snapshot",
            "result": "ambiguous",
            "matches": ["same-active", "same-archived"],
        }

    broadcast.assert_not_called()
    save_active.assert_not_called()
    assert server.lodes == [active, other_active]
    assert server.archived_lodes == [archived, other_archived]


@pytest.mark.parametrize("archived_first", [False, True], ids=["active-first", "archived-first"])
def test_lode_snapshot_twin_collapse_prefers_archived_in_both_orders(make_lode, archived_first):
    active = make_lode(id="same-id", status="active twin")
    archived = make_lode(id="same-id", status="archived twin")
    matches = (
        [(archived, True), (active, False)]
        if archived_first
        else [
            (active, False),
            (archived, True),
        ]
    )

    assert hopper_server._collapse_lode_snapshot_matches(matches) == [(archived, True)]


def test_server_lode_snapshot_returns_archived_twin_identity(socket_path, make_lode):
    active = make_lode(id="same-id", status="active twin", active=True)
    archived = make_lode(id="same-id", status="archived twin", active=True)
    server = Server(socket_path)
    server.lodes = [active]
    server.archived_lodes = [archived]
    conn = _mock_client(server)

    server._handle_mutation({"type": "lode_snapshot", "prefix": "same"}, conn)

    response = _decode_mock_response(conn)
    assert response["result"] == "found"
    assert response["lode"]["id"] == "same-id"
    assert response["lode"]["status"] == "archived twin"
    assert response["lode"]["active"] is True
    assert response["lode"]["archived"] is True
    assert "archived" not in active
    assert "archived" not in archived


@pytest.mark.parametrize("prefix", [None, 1, [], {}], ids=["missing", "integer", "list", "dict"])
def test_server_lode_snapshot_rejects_invalid_prefix(socket_path, prefix):
    server = Server(socket_path)
    conn = _mock_client(server)
    message = {"type": "lode_snapshot"}
    if prefix is not None:
        message["prefix"] = prefix

    server._handle_mutation(message, conn)

    assert _decode_mock_response(conn) == {
        "type": "error",
        "error": "lode_snapshot requires a string prefix",
        "ts": _decode_mock_response(conn)["ts"],
    }


def test_server_lode_snapshot_empty_prefix_is_valid(socket_path, make_lode):
    server = Server(socket_path)
    server.lodes = [make_lode(id="only-lode")]
    conn = _mock_client(server)

    server._handle_mutation({"type": "lode_snapshot", "prefix": ""}, conn)

    response = _decode_mock_response(conn)
    assert response["type"] == "lode_snapshot"
    assert response["result"] == "found"
    assert response["lode"]["id"] == "only-lode"
    assert response["lode"]["archived"] is False


def test_read_only_handlers_echo_thread_local_exchange_ids(socket_path):
    server = Server(socket_path)
    connections = [_mock_client(server), _mock_client(server)]
    barrier = threading.Barrier(2)

    def find_lode(_lode_id):
        barrier.wait(timeout=5)
        return None

    threads = []
    with patch.object(server, "_find_lode", side_effect=find_lode):
        for index, conn in enumerate(connections):
            message = {
                "type": "connect",
                "lode_id": f"lode-{index}",
                "exchange_id": f"exchange-{index}",
            }
            thread = threading.Thread(
                target=server._handle_read_only,
                args=(message, conn),
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

    assert [_decode_mock_response(conn)["exchange_id"] for conn in connections] == [
        "exchange-0",
        "exchange-1",
    ]


def test_mutation_responses_echo_exchange_id_and_clear_prior_value(socket_path):
    server = Server(socket_path)
    conn = _mock_client(server)

    server._handle_mutation(
        {"type": "lode_snapshot", "prefix": "missing", "exchange_id": "success-id"},
        conn,
    )
    assert _decode_mock_response(conn)["exchange_id"] == "success-id"

    server._handle_mutation(
        {"type": "lode_snapshot", "exchange_id": "error-id"},
        conn,
    )
    error_response = _decode_mock_response(conn)
    assert error_response["type"] == "error"
    assert error_response["exchange_id"] == "error-id"

    server._handle_mutation({"type": "lode_snapshot"}, conn)
    assert "exchange_id" not in _decode_mock_response(conn)


def _run_snapshot_burst(socket_path: Path, prefixes: list[str]) -> list[tuple[str, object]]:
    barrier = threading.Barrier(len(prefixes) + 1)
    results: list[tuple[str, object] | BaseException | None] = [None] * len(prefixes)

    def read_snapshot(index: int, prefix: str) -> None:
        try:
            barrier.wait(timeout=5)
            results[index] = read_lode_snapshot(socket_path, prefix, timeout=5)
        except BaseException as error:
            results[index] = error

    threads = [
        threading.Thread(target=read_snapshot, args=(index, prefix), daemon=True)
        for index, prefix in enumerate(prefixes)
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not [result for result in results if isinstance(result, BaseException)]
    assert all(result is not None for result in results)
    return results


def test_lode_snapshot_handles_twelve_simultaneous_clients(socket_path, server, make_lode):
    active = [make_lode(id=f"active-{index:02d}", active=True) for index in range(6)]
    archived = [make_lode(id=f"archived-{index:02d}", active=False) for index in range(6)]
    server.lodes = active
    server.archived_lodes = archived
    targets = [lode["id"] for pair in zip(active, archived) for lode in pair]

    results = _run_snapshot_burst(socket_path, targets)

    assert len(results) == 12
    for target, result in zip(targets, results):
        assert result[0] == "found"
        assert result[1]["id"] == target
        assert result[1]["archived"] is target.startswith("archived-")


def test_server_uses_configured_listen_backlog(socket_path, monkeypatch):
    assert LISTEN_BACKLOG == 64
    assert LISTEN_BACKLOG >= 32
    real_socket = socket.socket
    listen_calls = []

    class SocketProxy:
        def __init__(self, *args, **kwargs):
            self._socket = real_socket(*args, **kwargs)

        def listen(self, backlog):
            listen_calls.append(backlog)
            return self._socket.listen(backlog)

        def __getattr__(self, name):
            return getattr(self._socket, name)

    monkeypatch.setattr(hopper_server.socket, "socket", SocketProxy)
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5), "Server did not start"

    try:
        assert listen_calls == [LISTEN_BACKLOG]
    finally:
        server.stop()
        thread.join(timeout=2)


def test_parallel_lode_snapshots_do_not_write_state(socket_path, server, temp_config, make_lode):
    active = [
        make_lode(id="active-only", active=True),
        make_lode(id="shared-active", active=True),
    ]
    archived = [
        make_lode(id="archived-only", active=False),
        make_lode(id="shared-archived", active=False),
    ]
    server.lodes = active
    server.archived_lodes = archived
    save_lodes(active)
    save_archived_lodes(archived)
    active_path = temp_config / "active.jsonl"
    archived_path = temp_config / "archived.jsonl"
    active_before = active_path.read_bytes()
    archived_before = archived_path.read_bytes()
    cases = [
        ("active-o", "found", "active-only"),
        ("archived-o", "found", "archived-only"),
        ("missing", "absent", None),
        ("shared-", "ambiguous", ["shared-active", "shared-archived"]),
    ] * 6

    with patch.object(server, "broadcast") as broadcast:
        results = _run_snapshot_burst(socket_path, [case[0] for case in cases])

    for (_, expected_result, expected_payload), result in zip(cases, results):
        assert result[0] == expected_result
        if expected_result == "found":
            assert result[1]["id"] == expected_payload
        else:
            assert result[1] == expected_payload
    assert active_path.read_bytes() == active_before
    assert archived_path.read_bytes() == archived_before
    for path in (active_path, archived_path):
        assert all("archived" not in json.loads(line) for line in path.read_text().splitlines())
    broadcast.assert_not_called()


def _archived_page(server, make_lode, **request):
    """Ask the server for one archived page the way the client does."""
    server.archived_lodes = [
        make_lode(
            id=f"arch{index:04d}", project="proj-a" if index % 2 else "proj-b", updated_at=index
        )
        for index in range(50)
    ]
    return server._archived_list_page({"type": "archived_list", **request})


def test_archived_page_returns_the_newest_rows_first(server, make_lode):
    page = _archived_page(server, make_lode, limit=5)
    assert [lode["id"] for lode in page["lodes"]] == [
        "arch0049",
        "arch0048",
        "arch0047",
        "arch0046",
        "arch0045",
    ]
    assert page["total"] == 50
    assert page["offset"] == 0
    assert page["limit"] == 5


def test_archived_page_offset_walks_backwards_without_gaps_or_repeats(server, make_lode):
    first = _archived_page(server, make_lode, limit=5)
    second = _archived_page(server, make_lode, limit=5, offset=5)
    assert [lode["id"] for lode in second["lodes"]] == [
        "arch0044",
        "arch0043",
        "arch0042",
        "arch0041",
        "arch0040",
    ]
    assert not {lode["id"] for lode in first["lodes"]} & {lode["id"] for lode in second["lodes"]}


def test_archived_page_total_counts_the_filtered_archive_not_the_page(server, make_lode):
    page = _archived_page(server, make_lode, limit=3, project="proj-a")
    assert len(page["lodes"]) == 3
    assert page["total"] == 25
    assert all(lode["project"] == "proj-a" for lode in page["lodes"])


def test_archived_page_without_a_limit_still_returns_every_row(server, make_lode):
    # An older CLI names no bounds and must keep the answer it was written for.
    page = _archived_page(server, make_lode)
    assert len(page["lodes"]) == 50
    assert page["limit"] is None


@pytest.mark.parametrize("limit", [-1, "20", 20.0, True, None])
def test_archived_page_refuses_an_untrustworthy_bound(server, make_lode, limit):
    page = _archived_page(server, make_lode, limit=limit)
    assert len(page["lodes"]) == 50
    assert page["limit"] is None


def test_archived_page_past_the_end_is_empty_and_still_reports_the_total(server, make_lode):
    page = _archived_page(server, make_lode, limit=5, offset=500)
    assert page["lodes"] == []
    assert page["total"] == 50


def test_large_archive_snapshot_is_one_bounded_request(socket_path, server, make_lode, monkeypatch):
    target = make_lode(id="target-archive", stage="shipped", status="legacy archived")
    server.archived_lodes = [
        *(make_lode(id=f"archive-{index:04d}") for index in range(5_000)),
        target,
    ]
    read_archived = MagicMock(side_effect=AssertionError("bounded snapshot must not read archive"))
    monkeypatch.setattr(hopper_client, "read_archived_lodes", read_archived)

    with patch.object(
        hopper_client,
        "_exchange_message",
        wraps=hopper_client._exchange_message,
    ) as exchange:
        result = read_lode_snapshot(socket_path, "target", timeout=5)

    assert result == ("found", {**target, "archived": True})
    exchange.assert_called_once_with(
        socket_path,
        {"type": "lode_snapshot", "prefix": "target", "exchange_id": ANY},
        timeout=5,
        wait_for_response=True,
    )
    read_archived.assert_not_called()


def test_lode_snapshot_serializes_with_archive_transition(
    socket_path, server, make_lode, monkeypatch
):
    lode = make_lode(id="abcd2345", state="running", active=False)
    server.lodes = [lode]
    save_lodes(server.lodes)
    before = read_lode_snapshot(socket_path, "abcd")
    assert before[0] == "found"
    assert before[1]["id"] == "abcd2345"
    assert before[1]["archived"] is False

    real_archive_lode = hopper_server.archive_lode_for_action
    mid_transition = threading.Event()
    release_transition = threading.Event()

    def blocking_archive_lode(active, archived_lodes, lode_id, action_id):
        archived = real_archive_lode(active, archived_lodes, lode_id, action_id)
        mid_transition.set()
        assert release_transition.wait(5)
        return archived

    monkeypatch.setattr(hopper_server, "archive_lode_for_action", blocking_archive_lode)
    send_message(
        socket_path,
        {
            "type": "lode_action",
            "action_id": "d" * 32,
            "lode_id": "abcd2345",
            "expected_generation": None,
            "action_type": "archive",
            "target_disposition": "archived",
            "force_consent": False,
            "stage": "mill",
        },
    )
    assert mid_transition.wait(5), "archive did not reach the transition pause"
    result = []

    def read_during_transition():
        result.append(read_lode_snapshot(socket_path, "abcd", timeout=5))

    snapshot_thread = threading.Thread(target=read_during_transition, daemon=True)
    snapshot_thread.start()
    deadline = time.time() + 5
    snapshot_queued = False
    while time.time() < deadline:
        with server.event_queue.mutex:
            snapshot_queued = any(
                message.get("type") == "lode_snapshot"
                for message, _conn in server.event_queue.queue
            )
        if snapshot_queued:
            break
        time.sleep(0.01)

    try:
        assert snapshot_queued
        assert result == []
    finally:
        release_transition.set()

    snapshot_thread.join(timeout=10)
    assert not snapshot_thread.is_alive()
    assert result[0][0] == "found"
    assert result[0][1]["id"] == "abcd2345"
    assert result[0][1]["archived"] is True
    assert server.lodes == []
    assert [archived["id"] for archived in server.archived_lodes] == ["abcd2345"]


def test_lode_create_preserves_originating_extro_sid_over_socket(socket_path, server, temp_config):
    created = request_lode_creation(
        socket_path,
        "project-a",
        "scope-a",
        spawn=False,
        originating_extro_sid="extro-session-1",
        coder_provider="codex",
    )

    assert created["originating_extro_sid"] == "extro-session-1"
    assert server.lodes[0]["originating_extro_sid"] == "extro-session-1"
    persisted = json.loads((temp_config / "active.jsonl").read_text().strip())
    assert persisted["originating_extro_sid"] == "extro-session-1"


def test_lode_create_persists_selected_grok_provider(socket_path, server, temp_config):
    created = request_lode_creation(
        socket_path,
        "project-a",
        "scope-a",
        spawn=False,
        coder_provider="grok",
    )

    assert created["coder"] == {"provider": "grok", "session_id": None}
    persisted = json.loads((temp_config / "active.jsonl").read_text().strip())
    assert persisted["coder"] == {"provider": "grok", "session_id": None}


def test_create_refuses_before_mutation_when_server_lacks_provider_protocol(tmp_path, monkeypatch):
    calls = []

    def old_server_response(_socket_path, message, **_kwargs):
        calls.append(message)
        return None

    monkeypatch.setattr(hopper_client, "send_message", old_server_response)

    created = request_lode_creation(
        tmp_path / "old-server.sock",
        "project-a",
        "scope-a",
        spawn=False,
        coder_provider="codex",
    )

    assert created is None
    assert [message["type"] for message in calls] == ["coder_capabilities"]


@pytest.mark.parametrize("providers", [None, "codex", [1]])
def test_create_refuses_malformed_provider_capabilities(tmp_path, monkeypatch, providers):
    calls = []

    def malformed_response(_socket_path, message, **_kwargs):
        calls.append(message)
        return {"type": "coder_capabilities", "providers": providers}

    monkeypatch.setattr(hopper_client, "send_message", malformed_response)

    assert (
        request_lode_creation(
            tmp_path / "bad-server.sock",
            "project-a",
            "scope-a",
            spawn=False,
            coder_provider="codex",
        )
        is None
    )
    assert [message["type"] for message in calls] == ["coder_capabilities"]


@pytest.mark.parametrize(
    "diagnostic",
    [
        "codex command not found",
        "version check failed: permission denied",
        "version check failed: timed out after 5.0 seconds",
        "version check failed: exit 7",
    ],
)
def test_lode_create_refuses_codex_before_durable_creation_when_readiness_fails(
    socket_path, server, temp_config, diagnostic
):
    with patch(
        "hopper.server.coder_check",
        return_value={
            "provider": "codex",
            "ready": False,
            "version": "",
            "error": diagnostic,
        },
    ):
        created = request_lode_creation(
            socket_path,
            "project-a",
            "scope-a",
            spawn=False,
            coder_provider="codex",
        )

    assert created is None
    assert server.lodes == []
    assert (temp_config / "active.jsonl").read_text() == ""


def test_lode_create_requires_explicit_provider_before_durable_write(socket_path, temp_config):
    srv = Server(socket_path)
    conn = _mock_client(srv)
    active_path = temp_config / "active.jsonl"
    assert not active_path.exists()

    srv._handle_mutation(
        {"type": "lode_create", "project": "project-a", "scope": "scope-a"},
        conn,
    )

    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["error"] == "lode_create requires coder_provider"
    assert srv.lodes == []
    assert not active_path.exists()


def test_lode_create_unready_response_uses_shared_provider_diagnostic(socket_path, temp_config):
    srv = Server(socket_path)
    conn = _mock_client(srv)
    active_path = temp_config / "active.jsonl"
    assert not active_path.exists()
    readiness = {
        "provider": "codex",
        "ready": False,
        "version": "",
        "error": "codex command not found",
    }

    with patch("hopper.server.coder_check", return_value=readiness):
        srv._handle_mutation(
            {
                "type": "lode_create",
                "project": "project-a",
                "scope": "scope-a",
                "coder_provider": "codex",
            },
            conn,
        )

    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["error"] == "codex unavailable: codex command not found"
    assert srv.lodes == []
    assert not active_path.exists()


def test_sender_explicit_codex_crosses_prior_grok_default_receiver(
    socket_path, server, monkeypatch
):
    current_create = hopper_server.create_lode

    def prior_default_create(
        lodes,
        project,
        scope="",
        *,
        originating_extro_sid=None,
        coder_provider="grok",
    ):
        return current_create(
            lodes,
            project,
            scope,
            originating_extro_sid=originating_extro_sid,
            coder_provider=coder_provider,
        )

    monkeypatch.setattr(hopper_server, "create_lode", prior_default_create)

    created = request_lode_creation(
        socket_path,
        "project-a",
        "scope-a",
        spawn=False,
        coder_provider="codex",
    )

    assert created["codex_thread_id"] is None
    assert "coder" not in created
    assert "coder" not in server.lodes[0]


def test_concurrent_lode_create_responses_are_causally_bound(
    socket_path, server, temp_config, monkeypatch
):
    real_create_lode = hopper_server.create_lode
    real_enqueue_event = server._enqueue_event
    real_send_to_clients = server._send_to_clients
    a_create_started = threading.Event()
    b_enqueued = threading.Event()
    release_a = threading.Event()
    a_broadcast_delivered = threading.Event()
    results = {}

    def controlled_create_lode(
        lodes,
        project,
        scope="",
        *,
        originating_extro_sid=None,
        coder_provider,
    ):
        if scope == "scope-a":
            a_create_started.set()
            assert release_a.wait(5), "B was not connected and enqueued"
        elif scope == "scope-b":
            assert a_broadcast_delivered.wait(5), "A broadcast was not delivered"
        return real_create_lode(
            lodes,
            project,
            scope,
            originating_extro_sid=originating_extro_sid,
            coder_provider=coder_provider,
        )

    def observed_enqueue(message, conn=None):
        real_enqueue_event(message, conn)
        if message.get("type") == "lode_create" and message.get("scope") == "scope-b":
            b_enqueued.set()

    def observed_send_to_clients(message):
        real_send_to_clients(message)
        if message.get("type") == "lode_created" and message["lode"].get("scope") == "scope-a":
            a_broadcast_delivered.set()

    monkeypatch.setattr(hopper_server, "create_lode", controlled_create_lode)
    monkeypatch.setattr(server, "_enqueue_event", observed_enqueue)
    monkeypatch.setattr(server, "_send_to_clients", observed_send_to_clients)

    def create(name, project, scope):
        results[name] = request_lode_creation(
            socket_path,
            project,
            scope,
            spawn=False,
            timeout=5,
            coder_provider="codex",
        )

    a_thread = threading.Thread(
        target=create,
        args=("a", "project-a", "scope-a"),
        daemon=True,
    )
    a_thread.start()
    assert a_create_started.wait(5), "A did not reach create_lode"

    b_thread = threading.Thread(
        target=create,
        args=("b", "project-b", "scope-b"),
        daemon=True,
    )
    b_thread.start()
    assert b_enqueued.wait(5), "B was not enqueued"
    with server.lock:
        assert len(server.clients) == 2
    release_a.set()

    a_thread.join(timeout=10)
    b_thread.join(timeout=10)
    assert not a_thread.is_alive()
    assert not b_thread.is_alive()

    assert results["a"]["scope"] == "scope-a"
    assert results["b"]["scope"] == "scope-b"
    assert results["a"]["id"] != results["b"]["id"]
    assert results["b"]["id"] != results["a"]["id"]
    assert {(lode["project"], lode["scope"]) for lode in server.lodes} == {
        ("project-a", "scope-a"),
        ("project-b", "scope-b"),
    }
    assert len(server.lodes) == 2


def test_persistent_subscriber_and_one_shot_commands_are_isolated(socket_path, server, temp_config):
    subscriber_ready = threading.Event()
    broadcasts_received = threading.Event()
    broadcasts = []
    connection = HopperConnection(socket_path)

    def callback(message):
        if message.get("type") == "pong":
            subscriber_ready.set()
        elif message.get("type") == "lode_created":
            broadcasts.append(message["lode"]["scope"])
            if len(broadcasts) >= 2:
                broadcasts_received.set()

    connection.start(
        callback=callback,
        on_connect=lambda: connection.emit("ping"),
    )
    assert subscriber_ready.wait(5), "persistent subscriber did not connect"

    barrier = threading.Barrier(3)
    results = {}

    def create(name, scope):
        barrier.wait(timeout=5)
        results[name] = request_lode_creation(
            socket_path,
            f"project-{name}",
            scope,
            spawn=False,
            timeout=5,
            coder_provider="codex",
        )

    threads = [
        threading.Thread(target=create, args=("a", "subscriber-a"), daemon=True),
        threading.Thread(target=create, args=("b", "subscriber-b"), daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert broadcasts_received.wait(5), "subscriber missed a create broadcast"
    finally:
        connection.stop()

    assert results["a"]["scope"] == "subscriber-a"
    assert results["b"]["scope"] == "subscriber-b"
    assert results["a"]["id"] != results["b"]["id"]
    assert sorted(broadcasts) == ["subscriber-a", "subscriber-b"]


def test_lode_create_disabled_project_noop_without_conn(socket_path):
    """lode_create refuses disabled projects before creating or broadcasting."""
    srv = Server(socket_path)
    disabled = Project(path="/fake/repo", name="P", disabled=True, disabled_reason="wip")

    with (
        patch("hopper.server.find_project", return_value=disabled),
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        srv._handle_mutation(
            {
                "type": "lode_create",
                "project": "P",
                "scope": "scope",
                "coder_provider": "codex",
            },
            None,
        )

    assert srv.lodes == []
    assert not any(
        call.args[0].get("type") == "lode_created" for call in mock_broadcast.call_args_list
    )


def test_server_creates_socket(socket_path):
    """Server creates socket file on start."""
    srv = Server(socket_path)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()
    assert srv.ready.wait(5), "Server did not start"

    assert socket_path.exists()

    srv.stop()
    thread.join(timeout=2)

    # Socket cleaned up on stop
    assert not socket_path.exists()


def test_second_server_refuses_without_disturbing_original(socket_path):
    """A lock loser leaves the original socket and ping identity unchanged."""
    winner = Server(socket_path)
    thread = threading.Thread(target=winner.start, daemon=True)
    thread.start()
    assert winner.ready.wait(5), "Winner did not start"

    try:
        socket_inode = socket_path.stat().st_ino
        first_ping = send_message(socket_path, {"type": "ping"}, wait_for_response=True)

        loser = Server(socket_path)
        with pytest.raises(ServerLockHeld, match="a live hopper server"):
            loser.start()

        second_ping = send_message(socket_path, {"type": "ping"}, wait_for_response=True)
        assert socket_path.stat().st_ino == socket_inode
        assert second_ping["pid"] == first_ping["pid"] == os.getpid()
        assert second_ping["started_at"] == first_ping["started_at"] == winner.started_at
        assert socket_path.with_suffix(".pid").read_text() == str(os.getpid())
    finally:
        winner.stop()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("pidfile_contents", "expected_pid"),
    [("1234", "1234"), ("", "unavailable"), ("not-a-pid", "unavailable")],
)
def test_lock_refusal_happens_before_any_startup_mutation(
    socket_path, temp_config, make_lode, pidfile_contents, expected_pid
):
    """A lock loser cannot load, mutate, clean, spawn, or unlink startup state."""
    lode = make_lode(id="locked", active=True, tmux_pane="%9", pid=999)
    save_lodes([lode])
    active_path = temp_config / "active.jsonl"
    active_before = active_path.read_bytes()
    socket_path.write_text("foreign socket sentinel")
    socket_before = socket_path.read_bytes()
    worktree = temp_config / "lodes" / "locked" / "worktree"
    worktree.mkdir(parents=True)

    pidfile = socket_path.with_suffix(".pid")
    pidfile.write_text(pidfile_contents)
    held = open(pidfile, "a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = Server(socket_path)

    try:
        with (
            patch("hopper.server.load_lodes") as mock_load_lodes,
            patch("hopper.server.load_archived_lodes") as mock_load_archived,
            patch("hopper.server.load_backlog") as mock_load_backlog,
            patch("hopper.server.get_active_projects") as mock_projects,
            patch("hopper.server.save_lodes") as mock_save,
            patch("hopper.server.remove_worktree") as mock_remove,
            patch("hopper.server.delete_branch") as mock_delete,
            patch("hopper.server.spawn_lode_processor") as mock_spawn,
        ):
            with pytest.raises(ServerLockHeld) as exc_info:
                server.start()

        assert f"(pid {expected_pid})" in str(exc_info.value)
        mock_load_lodes.assert_not_called()
        mock_load_archived.assert_not_called()
        mock_load_backlog.assert_not_called()
        mock_projects.assert_not_called()
        mock_save.assert_not_called()
        mock_remove.assert_not_called()
        mock_delete.assert_not_called()
        mock_spawn.assert_not_called()
        assert active_path.read_bytes() == active_before
        assert socket_path.read_bytes() == socket_before
        assert worktree.is_dir()
        assert lode["active"] is True
        assert lode["tmux_pane"] == "%9"
        assert lode["pid"] == 999
    finally:
        held.close()


@pytest.mark.parametrize("old_pid", [999_999_999, os.getpid()])
def test_unlocked_stale_pidfile_does_not_block_start(socket_path, old_pid):
    """Pidfile contents are display-only when no process holds the lock."""
    socket_path.with_suffix(".pid").write_text(str(old_pid))
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5), "Server did not start"

    try:
        assert socket_path.with_suffix(".pid").read_text() == str(os.getpid())
    finally:
        server.stop()
        thread.join(timeout=2)


def test_stale_socket_without_listener_does_not_block_start(socket_path):
    """The lock holder replaces a stale socket path before binding."""
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    stale_inode = socket_path.stat().st_ino
    # Hold the stale inode allocated so the filesystem cannot recycle its number
    # for the replacement socket. ext4 hands a just-freed inode number straight
    # back out, so the comparison below passed only where the temp directory
    # happened to sit on tmpfs or btrfs.
    pinned_stale = socket_path.with_name(socket_path.name + ".pinned")
    os.link(socket_path, pinned_stale)

    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(5), "Server did not start"

    try:
        assert socket_path.stat().st_ino != stale_inode
        assert send_message(socket_path, {"type": "ping"}, wait_for_response=True)["type"] == "pong"
    finally:
        server.stop()
        thread.join(timeout=2)
        pinned_stale.unlink(missing_ok=True)


def test_server_that_never_bound_cannot_unlink_foreign_socket(socket_path):
    """Socket cleanup is gated by successful bind ownership."""
    socket_path.write_text("foreign")
    server = Server(socket_path)

    server.stop()

    assert socket_path.read_text() == "foreign"


def test_start_server_with_tui_reports_lock_refusal(socket_path, capsys):
    """A startup lock refusal exits before entering the TUI."""
    error = ServerLockHeld(
        "a live hopper server (pid 1234) holds the lock; "
        "attach to it or stop it before starting another"
    )
    with (
        patch.object(Server, "start", side_effect=error),
        patch("hopper.tui.run_tui") as mock_tui,
    ):
        assert start_server_with_tui(socket_path) == 1

    mock_tui.assert_not_called()
    assert str(error) in capsys.readouterr().out


def test_start_server_with_tui_reports_other_startup_error(socket_path, capsys):
    """A non-lock startup exception uses the generic failure path."""
    with (
        patch.object(Server, "start", side_effect=RuntimeError("bind exploded")),
        patch("hopper.tui.run_tui") as mock_tui,
    ):
        assert start_server_with_tui(socket_path) == 1

    mock_tui.assert_not_called()
    assert "Server failed to start: bind exploded" in capsys.readouterr().out


def test_two_process_server_start_race_has_one_stable_winner(tmp_path):
    """Two barrier-released processes yield one binder and one lock refusal."""
    child_code = r"""
import os
import socket
import sys
import threading

from hopper import config
from hopper.server import Server

host, port, label = sys.argv[1:]
control = socket.create_connection((host, int(port)), timeout=10)
control_file = control.makefile("rwb", buffering=0)
control_file.write(f"READY {label}\n".encode())
assert control_file.readline() == b"GO\n"
server = Server(config.server_socket_path())

def run_server():
    try:
        server.start()
    except Exception as error:
        server.startup_error = error
    finally:
        server.ready.set()

thread = threading.Thread(target=run_server, daemon=True)
thread.start()
assert server.ready.wait(5)
if server.startup_error is not None:
    message = str(server.startup_error)
    control_file.write(f"ERROR {label} {message}\n".encode())
    print(message)
    sys.exit(1)

control_file.write(
    f"BOUND {label} {os.getpid()} {server.started_at}\n".encode()
)
assert control_file.readline() == b"STOP\n"
server.stop()
thread.join(timeout=2)
"""
    xdg_home = tmp_path / "xdg"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    listener.settimeout(10)
    host, port = listener.getsockname()
    repo_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(xdg_home)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in [repo_root, env.get("PYTHONPATH", "")] if part
    )
    processes = {
        label: subprocess.Popen(
            [sys.executable, "-c", child_code, host, str(port), label],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for label in ("a", "b")
    }
    controls = {}
    connections = []
    try:
        for _ in processes:
            connection, _ = listener.accept()
            connection.settimeout(10)
            connections.append(connection)
            control_file = connection.makefile("rwb", buffering=0)
            ready = control_file.readline().decode().strip().split()
            assert ready[0] == "READY"
            controls[ready[1]] = control_file

        for control_file in controls.values():
            control_file.write(b"GO\n")
        reports = {
            label: control_file.readline().decode().strip()
            for label, control_file in controls.items()
        }
        bound = [label for label, report in reports.items() if report.startswith("BOUND ")]
        refused = [label for label, report in reports.items() if report.startswith("ERROR ")]
        assert len(bound) == 1, reports
        assert len(refused) == 1, reports
        winner_label = bound[0]
        loser_label = refused[0]
        assert "a live hopper server (pid " in reports[loser_label]
        assert "attach to it or stop it before starting another" in reports[loser_label]

        socket_path = xdg_home / "hopper" / "server.sock"
        first_ping = send_message(socket_path, {"type": "ping"}, wait_for_response=True)
        loser_output = processes[loser_label].communicate(timeout=10)
        assert processes[loser_label].returncode != 0
        assert "a live hopper server (pid " in loser_output[0]
        second_ping = send_message(socket_path, {"type": "ping"}, wait_for_response=True)
        assert second_ping["pid"] == first_ping["pid"]
        assert second_ping["started_at"] == first_ping["started_at"]

        bound_parts = reports[winner_label].split()
        assert first_ping["pid"] == int(bound_parts[2])
        assert first_ping["started_at"] == int(bound_parts[3])
        controls[winner_label].write(b"STOP\n")
        winner_output = processes[winner_label].communicate(timeout=10)
        assert processes[winner_label].returncode == 0, winner_output
    finally:
        for control_file in controls.values():
            control_file.close()
        for connection in connections:
            connection.close()
        listener.close()
        for process in processes.values():
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_server_lock_releases_after_sigkill(tmp_path):
    """A killed server needs no reaper before a replacement can bind."""
    child_code = r"""
import os
import socket
import sys
import threading

from hopper import config
from hopper.server import Server

host, port = sys.argv[1:]
control = socket.create_connection((host, int(port)), timeout=10)
control_file = control.makefile("rwb", buffering=0)
server = Server(config.server_socket_path())

def run_server():
    try:
        server.start()
    except Exception as error:
        server.startup_error = error
    finally:
        server.ready.set()

thread = threading.Thread(target=run_server, daemon=True)
thread.start()
assert server.ready.wait(5)
if server.startup_error is not None:
    control_file.write(f"ERROR {server.startup_error}\n".encode())
    sys.exit(1)
control_file.write(f"BOUND {os.getpid()} {server.started_at}\n".encode())
command = control_file.readline()
if command == b"CRASH\n":
    os._exit(137)
assert command == b"STOP\n"
server.stop()
thread.join(timeout=2)
"""
    xdg_home = tmp_path / "xdg"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    host, port = listener.getsockname()
    repo_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(xdg_home)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in [repo_root, env.get("PYTHONPATH", "")] if part
    )
    processes = []
    controls = []
    connections = []

    def start_child():
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, host, str(port)],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        connection, _ = listener.accept()
        connection.settimeout(10)
        connections.append(connection)
        control_file = connection.makefile("rwb", buffering=0)
        controls.append(control_file)
        report = control_file.readline().decode().strip().split()
        assert report[0] == "BOUND", report
        return process, control_file, int(report[1])

    try:
        first, first_control, first_pid = start_child()
        assert first_pid == first.pid
        first_control.write(b"CRASH\n")
        first_output = first.communicate(timeout=10)
        assert first.returncode == 137, first_output
        first_control.close()

        second, second_control, second_pid = start_child()
        assert second_pid == second.pid
        socket_path = xdg_home / "hopper" / "server.sock"
        response = send_message(socket_path, {"type": "ping"}, wait_for_response=True)
        assert response["pid"] == second_pid
        assert socket_path.with_suffix(".pid").read_text() == str(second_pid)

        second_control.write(b"STOP\n")
        second_output = second.communicate(timeout=10)
        assert second.returncode == 0, second_output
    finally:
        for control_file in controls:
            if not control_file.closed:
                control_file.close()
        for connection in connections:
            connection.close()
        listener.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_startup_reconciliation_alive_enters_reconnect_grace(socket_path, make_lode):
    lode = make_lode(
        id="alive-id",
        state="running",
        active=True,
        tmux_pane="%1",
        pid=1234,
        run_generation=TEST_RUN_GENERATION,
        status="spawn refused: tmux unreachable — verify tmux is running, then retry",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    assert lode["state"] == "reconnecting"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%1"
    assert lode["pid"] == 1234
    assert lode["reconnect_prior_state"] == "running"
    assert lode["reconnect_prior_status"].startswith("spawn refused:")
    assert lode["status"] == "Runner pane %1 survived server replacement; waiting for registration"
    assert lode["updated_at"] > 1000
    mock_save.assert_called_once_with(server.lodes)


def test_startup_reconciliation_gone_clears_identity_and_refusal(socket_path, make_lode):
    lode = make_lode(
        id="gone-id",
        state="running",
        active=True,
        tmux_pane="%2",
        pid=2345,
        run_generation=TEST_RUN_GENERATION,
        status="spawn refused: tmux unreachable — verify tmux is running, then retry",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    assert lode["active"] is False
    assert lode["state"] == "error"
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert "Recorded runner pane %2 is gone" in lode["status"]
    assert lode["updated_at"] > 1000
    mock_save.assert_called_once_with(server.lodes)


def test_startup_reconciliation_unknown_gates_without_restart(socket_path, make_lode):
    lode = make_lode(
        id="unknown-id",
        state="running",
        active=True,
        tmux_pane="%3",
        pid=3456,
        run_generation=TEST_RUN_GENERATION,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.UNKNOWN),
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    assert lode["state"] == "gated"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%3"
    assert lode["pid"] == 3456
    assert "Do not restart" in lode["status"]
    assert lode["spawn_disposition"] == "unknown"
    gate = lode_gate(lode)
    assert gate is not None
    assert gate["body"] == lode["status"]
    assert gate["kind"] == "explicit"
    assert lode["updated_at"] > 1000
    mock_save.assert_called_once_with(server.lodes)


def test_startup_reconciliation_without_pane_clears_unsupported_identity(socket_path, make_lode):
    lode = make_lode(
        id="no-pane",
        active=True,
        tmux_pane=None,
        pid=4567,
        status="spawn failed: tmux could not create a runner pane — verify tmux is running",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.pane_liveness") as mock_liveness,
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    mock_liveness.assert_not_called()
    assert lode["active"] is False
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert lode["state"] == "gated"
    assert "Do not restart" in lode["status"]
    assert lode["updated_at"] > 1000
    mock_save.assert_called_once_with(server.lodes)


def test_startup_reconciliation_mixed_lodes_saves_once(socket_path, make_lode):
    server = Server(socket_path)
    server.lodes = [
        make_lode(id="alive-id", active=True, tmux_pane="%1", pid=1),
        make_lode(id="gone-id", active=True, tmux_pane="%2", pid=2),
        make_lode(id="unknown-id", active=True, tmux_pane="%3", pid=3),
        make_lode(id="no-pane", active=True, pid=4),
    ]

    with (
        patch(
            "hopper.server.pane_liveness",
            side_effect=[Liveness.ALIVE, Liveness.GONE, Liveness.UNKNOWN],
        ),
        patch("hopper.server.save_lodes") as mock_save,
    ):
        server._reconcile_startup_lodes()

    mock_save.assert_called_once_with(server.lodes)


@pytest.mark.parametrize("state", ["new", "ready"])
def test_startup_reconciliation_alive_preserves_bounded_grace(socket_path, make_lode, state):
    lode = make_lode(
        id=f"{state}-id",
        state=state,
        status="Waiting for registration",
        active=True,
        tmux_pane="%8",
        pid=8080,
        run_generation=TEST_RUN_GENERATION,
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE):
        server._reconcile_startup_lodes()

    assert (lode["state"], lode["active"], lode["status"]) == (
        state,
        False,
        "Waiting for registration",
    )


def test_startup_reconciliation_intentionally_unspawned_new_retains_grace(socket_path, make_lode):
    lode = make_lode(id="new-id", state="new", active=True)
    server = Server(socket_path)
    server.lodes = [lode]

    with patch("hopper.server.pane_liveness") as liveness:
        server._reconcile_startup_lodes()

    liveness.assert_not_called()
    assert (lode["state"], lode["active"]) == ("new", False)


@pytest.mark.parametrize(
    ("state", "stage", "pending_action"),
    [
        ("error", "mill", None),
        ("gated", "mill", None),
        ("stuck", "mill", None),
        ("running", "shipped", None),
        ("teardown", "mill", {"phase": "spawning"}),
    ],
)
def test_startup_reconciliation_preserves_stronger_authority_inactive(
    socket_path, make_lode, state, stage, pending_action
):
    lode = make_lode(
        id="strong-id",
        state=state,
        stage=stage,
        status="Durable authority",
        active=True,
        tmux_pane="%9",
        run_generation=TEST_RUN_GENERATION,
        pending_action=pending_action,
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with patch("hopper.server.pane_liveness") as liveness:
        server._reconcile_startup_lodes()

    liveness.assert_not_called()
    assert (lode["state"], lode["active"], lode["status"]) == (
        state,
        False,
        "Durable authority",
    )


def test_gated_spawn_without_recorded_pane_spawns(socket_path, make_lode):
    server = Server(socket_path)
    lode = make_lode(id="fresh-id", active=False, tmux_pane=None, pid=None)
    server.lodes = [lode]

    with (
        patch("hopper.server.spawn_lode_processor", return_value=_spawned("%10")) as mock_spawn,
        patch.object(server, "broadcast") as mock_broadcast,
    ):
        outcome, pane = server._gated_spawn(lode, "/repo", foreground=False)

    assert outcome is SpawnOutcome.SPAWNED
    assert pane == "%10"
    assert lode["tmux_pane"] == "%10"
    assert lode["active"] is False
    assert lode["pid"] is None
    mock_spawn.assert_called_once_with("fresh-id", "/repo", foreground=False, env=ANY)
    mock_broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})


def test_gated_spawn_alive_refuses_even_when_active_is_false(socket_path, make_lode, caplog):
    lode = make_lode(
        id="incident-id",
        stage="ship",
        state="running",
        status="Ship ready",
        active=False,
        tmux_pane="%11",
        pid=1111,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    caplog.set_level(logging.WARNING)

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.spawn_lode_processor") as mock_spawn,
    ):
        outcome, pane = server._gated_spawn(
            lode,
            "/repo",
            spawn_updates={"stage": "refine", "state": "running"},
        )

    assert outcome is SpawnOutcome.ALREADY_LIVE
    assert pane is None
    mock_spawn.assert_not_called()
    assert lode["stage"] == "ship"
    assert lode["state"] == "gated"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%11"
    assert lode["pid"] == 1111
    assert lode["updated_at"] > 1000
    assert lode["status"] == (
        "Runner pane %11 is already live; attach with: hop lode peek incident-id. "
        "No new pane was started."
    )
    assert lode["spawn_disposition"] == "already_live"
    assert "attach instead of spawning" in caplog.text


def test_gated_spawn_gone_clears_stale_identity_then_spawns(socket_path, make_lode):
    lode = make_lode(id="gone-id", active=True, tmux_pane="%12", pid=1212)
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch("hopper.server.spawn_lode_processor", return_value=_spawned("%13")) as mock_spawn,
    ):
        outcome, pane = server._gated_spawn(lode, "/repo")

    assert outcome is SpawnOutcome.SPAWNED
    assert pane == "%13"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%13"
    assert lode["pid"] is None
    mock_spawn.assert_called_once()


def test_gated_spawn_unknown_preserves_identity_and_refuses(socket_path, make_lode, caplog):
    lode = make_lode(
        id="unknown-id",
        state="running",
        active=True,
        tmux_pane="%14",
        pid=1414,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    caplog.set_level(logging.WARNING)

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.UNKNOWN),
        patch("hopper.server.spawn_lode_processor") as mock_spawn,
    ):
        outcome, pane = server._gated_spawn(lode, "/repo")

    assert outcome is SpawnOutcome.UNKNOWN
    assert pane is None
    mock_spawn.assert_not_called()
    assert lode["state"] == "gated"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%14"
    assert lode["pid"] == 1414
    assert lode["updated_at"] > 1000
    assert "pane may be live" in lode["status"]
    assert "retry" not in lode["status"].lower()
    assert "restart" not in lode["status"].lower()
    assert "Do not launch another runner" in lode["status"]
    assert lode["spawn_disposition"] == "unknown"
    assert "unknown-id" in caplog.text


@pytest.mark.parametrize("force", [False, True])
def test_legacy_reset_wire_refuses_without_mutation(socket_path, make_lode, force):
    lode = make_lode(
        id="reset-id",
        stage="refine",
        state="running",
        active=True,
        tmux_pane="%live",
        pid=1415,
    )
    before = copy.deepcopy(lode)
    server = Server(socket_path)
    server.lodes = [lode]
    conn = _mock_client(server)

    server._handle_mutation(
        {
            "type": "lode_reset_claude_stage",
            "lode_id": "reset-id",
            "claude_stage": "refine",
            "spawn": True,
            "force": force,
            "ack_requested": True,
        },
        conn,
    )

    assert lode == before
    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == "protocol_upgrade_required"


def test_gated_spawn_missing_project_sets_visible_status(socket_path, make_lode):
    lode = make_lode(id="failed-id")
    server = Server(socket_path)
    server.lodes = [lode]
    with patch("hopper.server.spawn_lode_processor") as spawn:
        outcome, pane = server._gated_spawn(lode, None)

    assert outcome is SpawnOutcome.PROJECT_MISSING
    assert pane is None
    assert lode["state"] == "error"
    assert lode["active"] is False
    assert lode["spawn_disposition"] == "project_missing"
    assert "restore its registration/path" in lode["status"]
    assert "hop lode restart failed-id" in lode["status"]
    spawn.assert_not_called()


def test_gated_spawn_unknown_preserves_updates_and_forbids_restart(socket_path, make_lode):
    lode = make_lode(id="permission-id", stage="ship", state="ready", status="Ready")
    server = Server(socket_path)
    server.lodes = [lode]
    with patch(
        "hopper.server.spawn_lode_processor",
        return_value=(WindowSpawnOutcome.UNKNOWN, None),
    ):
        outcome, pane = server._gated_spawn(
            lode,
            "/repo",
            pending_state="ready",
            spawn_updates={"stage": "refine"},
        )

    assert outcome is SpawnOutcome.UNKNOWN
    assert pane is None
    assert lode["stage"] == "refine"
    assert lode["state"] == "gated"
    assert lode["active"] is False
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert lode["spawn_disposition"] == "unknown"
    assert "retry" not in lode["status"].lower()
    assert "restart" not in lode["status"].lower()
    assert "Do not launch another runner" in lode["status"]


@pytest.mark.parametrize(
    (
        "window_outcome",
        "project_path",
        "pending_state",
        "recorded_pane",
        "expected_outcome",
        "expected_state",
        "expected_disposition",
        "status_fragment",
    ),
    [
        (
            WindowSpawnOutcome.SPAWNED,
            "/repo",
            "new",
            None,
            SpawnOutcome.SPAWNED,
            "new",
            "spawned",
            "waiting for startup registration",
        ),
        (
            WindowSpawnOutcome.SPAWNED,
            "/repo",
            "ready",
            None,
            SpawnOutcome.SPAWNED,
            "ready",
            "spawned",
            "waiting for handoff registration",
        ),
        (
            WindowSpawnOutcome.PROVEN_NO_PANE,
            "/repo",
            "new",
            None,
            SpawnOutcome.PROVEN_NO_PANE,
            "error",
            "proven_no_pane",
            "repair or start tmux, then run: hop lode restart mapping-id",
        ),
        (
            WindowSpawnOutcome.UNKNOWN,
            "/repo",
            "new",
            None,
            SpawnOutcome.UNKNOWN,
            "gated",
            "unknown",
            "pane may be live",
        ),
        (
            None,
            None,
            "new",
            None,
            SpawnOutcome.PROJECT_MISSING,
            "error",
            "project_missing",
            "restore its registration/path, then run: hop lode restart mapping-id",
        ),
        (
            None,
            "/repo",
            "ready",
            "%70",
            SpawnOutcome.ALREADY_LIVE,
            "gated",
            "already_live",
            "attach with: hop lode peek mapping-id",
        ),
    ],
    ids=[
        "spawned-startup",
        "spawned-handoff",
        "proven-no-pane",
        "unknown",
        "project-missing",
        "already-live",
    ],
)
def test_gated_spawn_persists_truthful_disposition_table(
    socket_path,
    make_lode,
    window_outcome,
    project_path,
    pending_state,
    recorded_pane,
    expected_outcome,
    expected_state,
    expected_disposition,
    status_fragment,
):
    lode = make_lode(
        id="mapping-id",
        project="proj",
        state="new" if pending_state == "new" else "paused",
        tmux_pane=recorded_pane,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    spawn_result = (
        (window_outcome, "%71" if window_outcome is WindowSpawnOutcome.SPAWNED else None)
        if window_outcome is not None
        else None
    )

    with (
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.spawn_lode_processor", return_value=spawn_result) as spawn,
    ):
        outcome, pane_id = server._gated_spawn(
            lode,
            project_path,
            pending_state=pending_state,
        )

    assert outcome is expected_outcome
    assert pane_id == ("%71" if expected_outcome is SpawnOutcome.SPAWNED else None)
    assert (lode["state"], lode["active"], lode["spawn_disposition"]) == (
        expected_state,
        False,
        expected_disposition,
    )
    assert status_fragment in lode["status"]
    if expected_state == "gated":
        gate = lode_gate(lode)
        assert gate is not None
        assert gate["body"] == lode["status"]
        assert gate["kind"] == "explicit"
    if expected_outcome is SpawnOutcome.UNKNOWN:
        assert "retry" not in lode["status"].lower()
        assert "restart" not in lode["status"].lower()
        assert "Do not launch another runner" in lode["status"]
    if expected_outcome in {SpawnOutcome.PROJECT_MISSING, SpawnOutcome.ALREADY_LIVE}:
        spawn.assert_not_called()


def test_two_queued_spawn_requests_create_one_runner(socket_path, make_lode):
    lode = make_lode(id="queued-id", project="proj")
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.spawn_lode_processor", return_value=_spawned("%20")) as mock_spawn,
    ):
        event_thread = threading.Thread(target=server._event_loop, daemon=True)
        event_thread.start()
        server.enqueue({"type": "lode_spawn", "lode_id": "queued-id"})
        server.enqueue({"type": "lode_spawn", "lode_id": "queued-id"})

        deadline = time.monotonic() + 2
        while lode["spawn_disposition"] != "already_live" and time.monotonic() < deadline:
            time.sleep(0.01)
        server.stop_event.set()
        event_thread.join(timeout=1)

    mock_spawn.assert_called_once_with("queued-id", "/repo", foreground=False, env=ANY)
    assert lode["tmux_pane"] == "%20"


def test_lode_spawn_action_passes_foreground_to_gate(socket_path, make_lode):
    lode = make_lode(id="action-id", project="proj")
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch.object(server, "_gated_spawn") as mock_gate,
    ):
        server._handle_mutation(
            {"type": "lode_spawn", "lode_id": "action-id", "foreground": True},
            None,
        )

    mock_gate.assert_called_once_with(
        lode,
        "/repo",
        pending_state="new",
        foreground=True,
    )


def test_unarchive_and_spawn_is_one_server_action(socket_path, make_lode):
    lode = make_lode(id="restore-id", project="proj")
    lode["archived_at"] = 2000
    server = Server(socket_path)
    server.archived_lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch.object(server, "_gated_spawn") as mock_gate,
    ):
        server._handle_mutation(
            {
                "type": "lode_unarchive",
                "lode_id": "restore-id",
                "spawn": True,
                "foreground": False,
            },
            None,
        )

    assert server.archived_lodes == []
    assert server.lodes == [lode]
    assert "archived_at" not in lode
    mock_gate.assert_called_once_with(
        lode,
        "/repo",
        pending_state="new",
        foreground=False,
    )


def test_unarchive_spawn_fence_precedes_archive_list_mutation(socket_path, make_lode):
    record = _pending_completion_record()
    lode = make_lode(
        id=record["lode_id"],
        stage=record["stage"],
        state="teardown",
        run_generation="f" * 32,
    )
    lode["archived_at"] = 2_000
    server = Server(socket_path)
    server.archived_lodes = [lode]
    before_active = copy.deepcopy(server.lodes)
    before_archived = copy.deepcopy(server.archived_lodes)
    conn = _mock_client(server)

    with patch.object(server, "_gated_spawn") as spawn:
        server._handle_mutation(
            {
                "type": "lode_unarchive",
                "lode_id": lode["id"],
                "spawn": True,
            },
            conn,
        )

    assert server.lodes == before_active
    assert server.archived_lodes == before_archived
    assert _decode_mock_response(conn)["type"] == "error"
    spawn.assert_not_called()


def test_resume_refine_applies_updates_before_allowed_spawn(socket_path, make_lode):
    lode = make_lode(id="refine-id", stage="ship", state="ready", project="proj")
    server = Server(socket_path)
    server.lodes = [lode]

    def assert_updated_before_spawn(*args, **kwargs):
        assert lode["stage"] == "refine"
        assert lode["state"] == "ready"
        assert lode["active"] is False
        return _spawned("%21")

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.spawn_lode_processor", side_effect=assert_updated_before_spawn),
    ):
        server._handle_mutation({"type": "lode_resume_refine", "lode_id": "refine-id"}, None)

    assert lode["stage"] == "refine"
    assert lode["state"] == "ready"
    assert lode["active"] is False
    assert "waiting for handoff registration" in lode["status"]
    assert lode["tmux_pane"] == "%21"
    assert "refine" not in lode["runs"]


def test_resume_refine_proven_failure_keeps_refine_and_clears_gone_identity(socket_path, make_lode):
    lode = make_lode(
        id="refine-id",
        stage="ship",
        state="ready",
        status="Ready to refine",
        project="proj",
        active=True,
        tmux_pane="%22",
        pid=2222,
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch(
            "hopper.server.spawn_lode_processor",
            return_value=(WindowSpawnOutcome.PROVEN_NO_PANE, None),
        ),
        patch("hopper.server.save_lodes") as mock_save,
        patch.object(server, "broadcast") as mock_broadcast,
    ):
        server._handle_mutation({"type": "lode_resume_refine", "lode_id": "refine-id"}, None)

    assert lode["stage"] == "refine"
    assert lode["state"] == "error"
    assert lode["active"] is False
    assert lode["tmux_pane"] is None
    assert lode["pid"] is None
    assert "repair or start tmux" in lode["status"]
    assert "hop lode restart refine-id" in lode["status"]
    assert lode["spawn_disposition"] == "proven_no_pane"
    assert mock_save.call_count == 2
    mock_save.assert_called_with(server.lodes)
    mock_broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})


def test_resume_refine_live_pane_gates_without_changing_stage(socket_path, make_lode):
    lode = make_lode(
        id="refine-id",
        stage="ship",
        state="ready",
        project="proj",
        active=False,
        tmux_pane="%22",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch("hopper.server.find_project", return_value=None),
        patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE),
        patch("hopper.server.spawn_lode_processor") as mock_spawn,
    ):
        server._handle_mutation({"type": "lode_resume_refine", "lode_id": "refine-id"}, None)

    mock_spawn.assert_not_called()
    assert lode["stage"] == "ship"
    assert lode["state"] == "gated"
    assert lode["tmux_pane"] == "%22"
    assert lode["spawn_disposition"] == "already_live"


def test_resume_uses_gate_without_signaling_recorded_pid(socket_path, make_lode):
    lode = make_lode(
        id="resume-id",
        stage="refine",
        state="paused",
        project="proj",
        active=False,
        tmux_pane="%25",
        pid=4242,
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.pane_liveness", return_value=Liveness.GONE),
        patch("hopper.server.spawn_lode_processor", return_value=_spawned("%26")),
        patch("hopper.server.os.kill") as mock_kill,
    ):
        server._handle_mutation(
            {"type": "lode_resume", "lode_id": "resume-id"},
            None,
        )

    mock_kill.assert_not_called()
    assert lode["state"] == "ready"
    assert lode["active"] is False
    assert lode["tmux_pane"] == "%26"
    assert lode["pid"] is None


def test_fresh_backlog_promotion_spawns_through_gate(socket_path):
    item = BacklogItem(
        id="backlog1",
        project="proj",
        description="Fresh work",
        created_at=1000,
    )
    server = Server(socket_path)
    server.backlog = [item]

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.spawn_lode_processor", return_value=_spawned("%23")) as mock_spawn,
    ):
        lode = server._promote_backlog_item(item, coder_provider="codex")

    assert lode["tmux_pane"] == "%23"
    mock_spawn.assert_called_once_with(lode["id"], "/repo", foreground=False, env=ANY)


def test_fresh_lode_create_spawns_through_gate(socket_path):
    server = Server(socket_path)

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/repo", name="proj"),
        ),
        patch("hopper.server.spawn_lode_processor", return_value=_spawned("%24")) as mock_spawn,
    ):
        server._handle_mutation(
            {
                "type": "lode_create",
                "project": "proj",
                "scope": "work",
                "spawn": True,
                "coder_provider": "codex",
            },
            None,
        )

    lode = server.lodes[0]
    assert lode["tmux_pane"] == "%24"
    mock_spawn.assert_called_once_with(lode["id"], "/repo", foreground=False, env=ANY)


@pytest.mark.parametrize(
    ("status", "spawn_disposition"),
    [
        ("Runner pane creation is unverified", "unknown"),
        ("tmux did not create a runner pane", "proven_no_pane"),
    ],
)
def test_runner_registration_clears_spawn_status(socket_path, make_lode, status, spawn_disposition):
    lode = make_lode(
        id="register-id",
        status=status,
        spawn_disposition=spawn_disposition,
        run_generation=TEST_RUN_GENERATION,
    )
    server = Server(socket_path)
    server.lodes = [lode]

    server._register_lode_client(
        "register-id",
        MagicMock(),
        tmux_pane="%25",
        pid=2525,
        run_generation=TEST_RUN_GENERATION,
        proof_mode="other-bounded-no-birth",
    )

    assert lode["state"] == "running"
    assert lode["active"] is True
    assert lode["status"] == "Starting mill"
    assert lode["spawn_disposition"] is None
    assert lode["tmux_pane"] == "%25"
    assert lode["pid"] == 2525


@pytest.mark.parametrize("state", ["new", "ready", "reconnecting"])
def test_registration_atomically_leaves_only_eligible_graces(socket_path, make_lode, state):
    generation = "9" * 32
    lode = make_lode(
        id="register-id",
        state=state,
        status="Waiting for registration",
        active=False,
        run_generation=generation,
        spawn_disposition="spawned",
        reconnect_prior_state="running",
        reconnect_prior_status="Connected",
    )
    server = Server(socket_path)
    server.lodes = [lode]
    persisted = []
    broadcasts = []

    def observe_save(lodes):
        persisted.append(
            (
                lodes[0]["state"],
                lodes[0]["active"],
                lodes[0]["status"],
                lodes[0]["spawn_disposition"],
            )
        )

    with (
        patch("hopper.server.save_lodes", side_effect=observe_save),
        patch.object(server, "broadcast", side_effect=lambda message: broadcasts.append(message)),
    ):
        assert server._register_lode_client(
            "register-id",
            MagicMock(),
            tmux_pane="%72",
            pid=7272,
            run_generation=generation,
            proof_mode="other-bounded-no-birth",
        )

    assert persisted == [("running", True, "Starting mill", None)]
    assert broadcasts[0]["lode"]["state"] == "running"
    assert broadcasts[0]["lode"]["active"] is True
    assert not any(saved[:2] == ("new", True) for saved in persisted)
    assert "reconnect_prior_state" not in lode
    assert "reconnect_prior_status" not in lode


@pytest.mark.parametrize("state", ["new", "ready", "reconnecting"])
@pytest.mark.parametrize(
    "msg_type",
    sorted(hopper_client.RUNNER_MUTATION_TYPES - {"lode_register", "lode_supervisor_register"}),
)
def test_lifecycle_grace_denies_every_nonregistration_runner_mutation_without_changes(
    socket_path, make_lode, state, msg_type
):
    generation = "7" * 32
    lode = make_lode(
        id="grace777",
        state=state,
        status="Waiting for registration",
        active=False,
        run_generation=generation,
    )
    server = Server(socket_path)
    server.lodes = [lode]
    conn = _mock_client(server)
    before = copy.deepcopy(lode)
    message = {
        "type": msg_type,
        "lode_id": lode["id"],
        "run_generation": generation,
        "ack_requested": True,
    }
    if msg_type == "lode_action":
        message.update(
            expected_generation=generation,
            action_id="7" * 32,
            action_type="completion",
            target_disposition="advance_refine",
            force_consent=False,
            stage="mill",
        )
    elif msg_type == "lode_set_state":
        message.update(state="running", status="Connected")

    with (
        patch("hopper.server.save_lodes") as save,
        patch.object(server, "broadcast") as broadcast,
    ):
        server._handle_mutation(message, conn)

    response = _decode_mock_response(conn)
    if msg_type == "lode_action":
        assert response["outcome"] == "refused"
    else:
        assert response["accepted"] is False
    assert response["reason"] == "lifecycle_grace_pending"
    assert lode == before
    save.assert_not_called()
    broadcast.assert_not_called()
    assert server.action_acceptances == {}


def test_supervisor_registration_is_exempt_but_preserves_reconnect_grace(socket_path, make_lode):
    generation = TEST_RUN_GENERATION
    lode = make_lode(
        id="supervisor-id",
        state="reconnecting",
        status="Waiting for registration",
        active=False,
        run_generation=generation,
        reconnect_prior_state="running",
        reconnect_prior_status="Connected",
    )
    server = Server(socket_path)
    server.lodes = [lode]
    conn = _mock_client(server)
    before = copy.deepcopy(lode)
    record = _strict_completion_run_ownership(lode["id"], generation)

    with (
        patch("hopper.server.actions.write_run_ownership"),
        patch.object(server, "_record_action_spawn_adoption") as adoption,
    ):
        server._handle_registration_capture_result(
            {
                "key": f"supervisor:{lode['id']}:{generation}",
                "kind": "supervisor",
                "lode_id": lode["id"],
                "run_generation": generation,
                "request": {},
                "result": {"ok": True, "record": record},
            },
            conn,
        )

    assert lode == before
    adoption.assert_called_once_with(lode["id"], generation, "supervisor")
    response = _decode_mock_response(conn)
    assert response["type"] == "lode_supervisor_registered"
    assert response["accepted"] is True


@pytest.mark.parametrize(
    ("state", "pending_action"),
    [
        ("gated", None),
        ("stuck", None),
        ("teardown", {"phase": "spawning"}),
    ],
)
def test_registration_preserves_stronger_state_authority(
    socket_path, make_lode, state, pending_action
):
    generation = "8" * 32
    lode = make_lode(
        id="register-id",
        state=state,
        status="Durable authority",
        active=False,
        pending_action=pending_action,
        run_generation=generation,
        spawn_disposition="unknown",
    )
    server = Server(socket_path)
    server.lodes = [lode]

    with patch("hopper.server.save_lodes"):
        assert server._register_lode_client(
            "register-id",
            MagicMock(),
            tmux_pane="%73",
            pid=7373,
            run_generation=generation,
            proof_mode="other-bounded-no-birth",
        )

    assert (lode["state"], lode["status"], lode["active"]) == (
        state,
        "Durable authority",
        True,
    )
    assert lode["spawn_disposition"] is None


@pytest.mark.parametrize("proof_mode", [None, "unknown"])
def test_runner_registration_refuses_unknown_or_missing_proof_mode(
    socket_path, make_lode, proof_mode
):
    lode = make_lode(
        id="register-id",
        active=False,
        oom_scope="hopper-test.scope",
        run_generation=TEST_RUN_GENERATION,
    )
    server = Server(socket_path)
    server.lodes = [lode]

    assert not server._register_lode_client(
        "register-id",
        MagicMock(),
        run_generation=TEST_RUN_GENERATION,
        proof_mode=proof_mode,
        actual_unit=None,
    )
    assert "register-id" not in server.lode_clients
    assert lode["active"] is False
    assert lode["oom_scope"] == "hopper-test.scope"


def test_spawn_lode_processor_has_gated_and_action_successor_callers():
    hopper_dir = Path(__file__).resolve().parents[1] / "hopper"
    source = (hopper_dir / "server.py").read_text()
    assert source.count("spawn_lode_processor(") == 2
    assert "def _gated_spawn(" in source
    assert "def _spawn_action_successor(" in source
    assert "def _spawn_completion_pane(" not in source
    caller_count = sum(
        path.read_text().count("spawn_lode_processor(") for path in hopper_dir.glob("*.py")
    )
    assert caller_count == 3


def test_startup_archives_shipped_lodes(socket_path, temp_config, make_lode):
    """Server startup migrates shipped lodes from active to archived."""
    shipped_lode = make_lode(
        id="test-id",
        stage="shipped",
        active=True,
        tmux_pane="%1",
        pid=1234,
    )
    save_lodes([shipped_lode])

    srv = Server(socket_path)
    with patch("hopper.server.pane_liveness", return_value=Liveness.ALIVE):
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            assert srv.lodes == []
            assert len(srv.archived_lodes) == 1
            assert srv.archived_lodes[0]["id"] == "test-id"
            assert "archived_at" in srv.archived_lodes[0]

            archived_file = temp_config / "archived.jsonl"
            assert archived_file.exists()
            archived_entries = [
                json.loads(line) for line in archived_file.read_text().splitlines() if line.strip()
            ]
            assert len(archived_entries) == 1
            assert archived_entries[0]["id"] == "test-id"
            assert "archived_at" in archived_entries[0]
        finally:
            srv.stop()
            thread.join(timeout=2)


def test_startup_archives_shipped_lode_without_reconnect_conversion(
    socket_path, temp_config, make_lode
):
    """UNKNOWN runner evidence does not block lock-held shipped auto-archive."""
    shipped_lode = make_lode(
        id="unknown-shipped",
        stage="shipped",
        active=True,
        tmux_pane="%9",
        pid=9999,
    )
    save_lodes([shipped_lode])
    server = Server(socket_path)
    with patch("hopper.server.pane_liveness", return_value=Liveness.UNKNOWN):
        thread = threading.Thread(target=server.start, daemon=True)
        thread.start()
        try:
            assert server.ready.wait(5), "Server did not start"
            assert server.lodes == []
            assert server.archived_lodes[0]["id"] == "unknown-shipped"
            assert server.archived_lodes[0]["state"] != "reconnecting"
            assert server.archived_lodes[0]["active"] is False
        finally:
            server.stop()
            thread.join(timeout=2)


def test_cleanup_worktree_on_startup_archive(socket_path, temp_config, make_lode):
    """Startup archive triggers worktree and branch cleanup."""
    shipped_lode = make_lode(
        id="test-id",
        stage="shipped",
        project="myproject",
        branch="hopper-test-id",
    )
    save_lodes([shipped_lode])
    worktree_dir = temp_config / "lodes" / shipped_lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree", return_value=True) as mock_remove_worktree,
        patch("hopper.server.branch_exists", return_value=True),
        patch("hopper.server.delete_branch", return_value=True) as mock_delete_branch,
    ):
        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            for _ in range(50):
                if mock_remove_worktree.called and mock_delete_branch.called:
                    break
                time.sleep(0.1)

            mock_remove_worktree.assert_called_once_with("/fake/repo", str(worktree_dir))
            mock_delete_branch.assert_called_once_with("/fake/repo", shipped_lode["branch"])
        finally:
            srv.stop()
            thread.join(timeout=2)


def test_cleanup_dirty_worktree_skips_remove_and_branch(
    socket_path, temp_config, make_lode, caplog
):
    """Dirty worktree cleanup retains path and skips branch deletion."""
    lode = make_lode(
        id="test-id",
        stage="shipped",
        project="myproject",
        branch="hopper-test-id",
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)
    caplog.set_level("WARNING")

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=True) as mock_dirty,
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        srv._cleanup_worktree(lode)

    mock_dirty.assert_called_once_with(str(worktree_dir))
    mock_remove_worktree.assert_not_called()
    mock_delete_branch.assert_not_called()
    assert worktree_dir.exists()
    assert any(
        "worktree is dirty or cleanliness could not be proven" in record.getMessage()
        for record in caplog.records
    )


def test_cleanup_clean_worktree_removes_and_deletes_branch(socket_path, temp_config, make_lode):
    """Clean worktree cleanup removes the worktree and deletes the branch."""
    lode = make_lode(
        id="test-id",
        stage="shipped",
        project="myproject",
        branch="hopper-test-id",
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False) as mock_dirty,
        patch("hopper.server.remove_worktree", return_value=True) as mock_remove_worktree,
        patch("hopper.server.branch_exists", return_value=True),
        patch("hopper.server.delete_branch", return_value=True) as mock_delete_branch,
    ):
        srv._cleanup_worktree(lode)

    mock_dirty.assert_called_once_with(str(worktree_dir))
    mock_remove_worktree.assert_called_once_with("/fake/repo", str(worktree_dir))
    mock_delete_branch.assert_called_once_with("/fake/repo", lode["branch"])


def test_reap_cleanup_persists_partial_worktree_progress_before_retry(
    socket_path, temp_config, make_lode
):
    lode = make_lode(id="test-id", project="myproject", branch="hopper-test-id")
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False) as dirty,
        patch("hopper.server.remove_worktree", return_value=True) as remove,
        patch("hopper.server.branch_exists", return_value=True),
        patch("hopper.server.delete_branch", return_value=False),
        patch("hopper.server.current_time_ms", return_value=10_000),
    ):
        assert srv._cleanup_worktree(lode) is False

    assert lode["worktree_reap"] == {
        "trigger": "shipped",
        "path": str(worktree_dir),
        "worktree_removed_at": 10_000,
        "reaped_at": None,
    }
    dirty.assert_called_once_with(str(worktree_dir))
    remove.assert_called_once_with("/fake/repo", str(worktree_dir))

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty") as dirty,
        patch("hopper.server.remove_worktree") as remove,
        patch("hopper.server.branch_exists", return_value=False),
        patch("hopper.server.current_time_ms", return_value=20_000),
    ):
        assert srv._cleanup_worktree(lode) is True

    dirty.assert_not_called()
    remove.assert_not_called()
    assert lode["worktree_reap"]["reaped_at"] == 20_000


def test_reap_sweep_reaps_shipped_lode_at_grace_boundary(socket_path, temp_config, make_lode):
    now = 1_000_000_000
    lode = make_lode(
        id="test-id",
        stage="shipped",
        shipped_at=now - hopper_server.SHIPPED_WORKTREE_REAP_GRACE_MS,
        project="myproject",
        branch="hopper-test-id",
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)
    srv.lodes = [lode]

    with (
        patch("hopper.server.current_time_ms", return_value=now),
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree", return_value=True) as remove,
        patch("hopper.server.branch_exists", return_value=False),
        patch.object(srv, "broadcast") as broadcast,
    ):
        srv._reap_eligible_worktrees()

    remove.assert_called_once_with("/fake/repo", str(worktree_dir))
    assert lode["worktree_reap"]["trigger"] == "shipped"
    assert lode["worktree_reap"]["worktree_removed_at"] == now
    assert lode["worktree_reap"]["reaped_at"] == now
    broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})


def test_reap_sweep_waits_for_error_grace_and_excludes_terminal_shipped_lodes(
    socket_path, make_lode
):
    now = 1_000_000_000
    shipped_terminal = make_lode(
        id="shipterm",
        stage="shipped",
        shipped_at=now - hopper_server.SHIPPED_WORKTREE_REAP_GRACE_MS,
        failure_kind="oom",
    )
    terminal_error = make_lode(
        id="errdelay",
        state="error",
        failure_kind="oom",
        errored_at=now - hopper_server.ERROR_WORKTREE_REAP_GRACE_MS + 1,
    )
    srv = Server(socket_path)
    srv.lodes = [shipped_terminal, terminal_error]

    with (
        patch("hopper.server.current_time_ms", return_value=now),
        patch.object(srv, "_prepare_worktree_reap") as prepare,
    ):
        srv._reap_eligible_worktrees()

    prepare.assert_not_called()


def test_reap_sweep_excludes_oom_terminal_error_after_grace(socket_path, make_lode):
    now = 1_000_000_000
    lode = make_lode(
        id="oomerror",
        state="error",
        failure_kind="oom",
        errored_at=now - hopper_server.ERROR_WORKTREE_REAP_GRACE_MS - 1,
    )
    srv = Server(socket_path)
    srv.lodes = [lode]

    with (
        patch("hopper.server.current_time_ms", return_value=now),
        patch.object(srv, "_prepare_worktree_reap") as prepare,
    ):
        srv._reap_eligible_worktrees()

    prepare.assert_not_called()
    assert lode["worktree_reap"] is None


def test_reap_sweep_excludes_unverified_terminal_error_after_grace(socket_path, make_lode):
    now = 1_000_000_000
    lode = make_lode(
        id="unverif1",
        state="error",
        failure_kind="runner_exit_unverified",
        errored_at=now - hopper_server.ERROR_WORKTREE_REAP_GRACE_MS - 1,
    )
    srv = Server(socket_path)
    srv.lodes = [lode]

    with (
        patch("hopper.server.current_time_ms", return_value=now),
        patch.object(srv, "_prepare_worktree_reap") as prepare,
    ):
        srv._reap_eligible_worktrees()

    prepare.assert_not_called()
    assert lode["worktree_reap"] is None


def test_reap_sweep_reaps_generic_error_after_grace(socket_path, temp_config, make_lode):
    now = 1_000_000_000
    lode = make_lode(
        id="generror",
        state="error",
        errored_at=now - hopper_server.ERROR_WORKTREE_REAP_GRACE_MS - 1,
        project="myproject",
        branch="hopper-generror",
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)
    srv.lodes = [lode]

    with (
        patch("hopper.server.current_time_ms", return_value=now),
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree", return_value=True) as remove,
        patch("hopper.server.branch_exists", return_value=False),
        patch.object(srv, "broadcast"),
    ):
        srv._reap_eligible_worktrees()

    remove.assert_called_once_with("/fake/repo", str(worktree_dir))
    assert lode["worktree_reap"]["trigger"] == "error"
    assert type(lode["worktree_reap"]["reaped_at"]) is int


def test_reap_sweep_shipped_grace_requires_elapsed_time(socket_path, temp_config, make_lode):
    now = 1_000_000_000
    lode = make_lode(
        id="shipgrce",
        stage="shipped",
        shipped_at=now - hopper_server.SHIPPED_WORKTREE_REAP_GRACE_MS + 1,
        project="myproject",
        branch="hopper-shipgrce",
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)
    srv.lodes = [lode]

    with patch("hopper.server.current_time_ms", return_value=now):
        srv._reap_eligible_worktrees()

    assert lode["worktree_reap"] is None

    with (
        patch("hopper.server.current_time_ms", return_value=now + 2),
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree", return_value=True) as remove,
        patch("hopper.server.branch_exists", return_value=False),
        patch.object(srv, "broadcast"),
    ):
        srv._reap_eligible_worktrees()

    remove.assert_called_once_with("/fake/repo", str(worktree_dir))
    assert type(lode["worktree_reap"]["reaped_at"]) is int


def test_reap_sweep_generic_error_grace_requires_elapsed_time(socket_path, temp_config, make_lode):
    now = 1_000_000_000
    lode = make_lode(
        id="errgrace",
        state="error",
        errored_at=now - hopper_server.ERROR_WORKTREE_REAP_GRACE_MS + 1,
        project="myproject",
        branch="hopper-errgrace",
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)
    srv.lodes = [lode]

    with patch("hopper.server.current_time_ms", return_value=now):
        srv._reap_eligible_worktrees()

    assert lode["worktree_reap"] is None

    with (
        patch("hopper.server.current_time_ms", return_value=now + 2),
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree", return_value=True) as remove,
        patch("hopper.server.branch_exists", return_value=False),
        patch.object(srv, "broadcast"),
    ):
        srv._reap_eligible_worktrees()

    remove.assert_called_once_with("/fake/repo", str(worktree_dir))
    assert type(lode["worktree_reap"]["reaped_at"]) is int


def test_reap_sweep_continues_after_one_cleanup_failure(socket_path, temp_config, make_lode):
    now = 1_000_000_000
    failed = make_lode(
        id="failed11",
        stage="shipped",
        shipped_at=now - hopper_server.SHIPPED_WORKTREE_REAP_GRACE_MS - 1,
        project="myproject",
        branch="hopper-failed11",
    )
    succeeded = make_lode(
        id="success1",
        stage="shipped",
        shipped_at=now - hopper_server.SHIPPED_WORKTREE_REAP_GRACE_MS - 1,
        project="myproject",
        branch="hopper-success1",
    )
    failed_path = temp_config / "lodes" / failed["id"] / "worktree"
    succeeded_path = temp_config / "lodes" / succeeded["id"] / "worktree"
    failed_path.mkdir(parents=True)
    succeeded_path.mkdir(parents=True)
    srv = Server(socket_path)
    srv.lodes = [failed, succeeded]

    with (
        patch("hopper.server.current_time_ms", return_value=now),
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree", side_effect=(False, True)) as remove,
        patch("hopper.server.branch_exists", return_value=False),
        patch.object(srv, "broadcast"),
    ):
        srv._reap_eligible_worktrees()

    assert remove.call_args_list == [
        call("/fake/repo", str(failed_path)),
        call("/fake/repo", str(succeeded_path)),
    ]
    assert failed["worktree_reap"]["reaped_at"] is None
    assert type(succeeded["worktree_reap"]["reaped_at"]) is int


def test_reap_sweep_kill_requires_a_zero_unpushed_count(socket_path, temp_config, make_lode):
    lode = make_lode(
        id="test-id",
        project="myproject",
        branch="hopper-test-id",
        state="error",
        action_results=[{"action_type": "kill"}],
    )
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)
    srv = Server(socket_path)
    srv.archived_lodes = [lode]

    with (
        patch("hopper.server.git.unpushed_commits", return_value=(None, None)),
        patch("hopper.server.remove_worktree") as remove,
    ):
        srv._reap_eligible_worktrees()

    assert lode["worktree_reap"]["trigger"] == "killed"
    assert lode["worktree_reap"]["reaped_at"] is None
    remove.assert_not_called()

    with (
        patch("hopper.server.git.unpushed_commits", return_value=(0, "origin/main")),
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree", return_value=True) as remove,
        patch("hopper.server.branch_exists", return_value=False),
        patch.object(srv, "broadcast"),
    ):
        srv._reap_eligible_worktrees()

    remove.assert_called_once_with("/fake/repo", str(worktree_dir))
    assert type(lode["worktree_reap"]["reaped_at"]) is int


def test_reap_sweep_throttle_uses_monotonic_time(socket_path):
    srv = Server(socket_path)
    with (
        patch("hopper.server.time.monotonic", side_effect=(100.0, 159.9, 160.0)),
        patch.object(srv, "_reap_eligible_worktrees") as sweep,
    ):
        srv._maybe_reap_worktrees()
        srv._maybe_reap_worktrees()
        srv._maybe_reap_worktrees()

    assert sweep.call_count == 2


def test_reap_timestamps_are_written_at_terminal_transitions(socket_path, temp_config, make_lode):
    record = _pending_completion_record(stage="ship")
    srv = Server(socket_path)
    shipped = make_lode(
        id=record["lode_id"],
        stage="ship",
        state="teardown",
        run_generation=record["expected_generation"],
    )
    srv.lodes = [shipped]

    with patch("hopper.server.current_time_ms", return_value=10_000):
        assert srv._apply_completion_stage(record) is True

    assert shipped["stage"] == "shipped"
    assert shipped["shipped_at"] == 10_000
    assert shipped["errored_at"] is None

    failed = make_lode(id="failure1", active=True, tmux_pane="%1", pid=1)
    srv.lodes = [failed]
    with patch("hopper.server.current_time_ms", return_value=20_000):
        assert srv._set_terminal_failure(failed, "oom", None) is True

    assert failed["errored_at"] == 20_000
    with patch("hopper.server.current_time_ms", return_value=30_000):
        assert srv._set_terminal_failure(failed, "oom", None) is False
    assert failed["errored_at"] == 20_000


def test_lode_state_error_sets_errored_at(socket_path, temp_config, make_lode):
    lode = make_lode(id="generic1", state="running")
    srv = Server(socket_path)
    srv.lodes = [lode]

    with patch("hopper.lodes.current_time_ms", return_value=10_000):
        assert update_lode_state(srv.lodes, lode["id"], "error", "Runner failed") is lode

    assert lode["state"] == "error"
    assert lode["errored_at"] == 10_000


def test_lode_state_error_notification_preserves_errored_at(socket_path, temp_config, make_lode):
    lode = make_lode(id="generic2", state="error", errored_at=10_000)
    srv = Server(socket_path)
    srv.lodes = [lode]

    with patch("hopper.lodes.current_time_ms", return_value=20_000):
        assert update_lode_state(srv.lodes, lode["id"], "error", "Runner still failed") is lode

    assert lode["errored_at"] == 10_000


def test_lode_state_error_new_episode_refreshes_timestamp_and_reap_grace(
    socket_path, temp_config, make_lode
):
    first_error_at = 1_000_000
    second_error_at = first_error_at + hopper_server.ERROR_WORKTREE_REAP_GRACE_MS + 10
    lode = make_lode(id="generic3", state="running")
    srv = Server(socket_path)
    srv.lodes = [lode]

    with patch("hopper.lodes.current_time_ms", return_value=first_error_at):
        assert update_lode_state(srv.lodes, lode["id"], "error", "First failure") is lode
    with patch("hopper.lodes.current_time_ms", return_value=first_error_at + 1):
        assert update_lode_state(srv.lodes, lode["id"], "running", "Recovered") is lode
    assert lode["errored_at"] is None

    with patch("hopper.lodes.current_time_ms", return_value=second_error_at):
        assert update_lode_state(srv.lodes, lode["id"], "error", "Second failure") is lode

    assert lode["errored_at"] == second_error_at
    with (
        patch("hopper.server.current_time_ms", return_value=second_error_at + 1),
        patch.object(srv, "_prepare_worktree_reap") as prepare,
    ):
        srv._reap_eligible_worktrees()

    prepare.assert_not_called()
    assert lode["worktree_reap"] is None


def test_reap_resume_refuses_before_spawning(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            stage="refine",
            worktree_reap={
                "trigger": "shipped",
                "path": "/worktree",
                "worktree_removed_at": 1,
                "reaped_at": 2,
            },
        )
    ]
    conn = _mock_client(srv)

    with patch.object(srv, "_gated_spawn") as spawn:
        srv._handle_mutation({"type": "lode_resume", "lode_id": "test-id"}, conn)

    spawn.assert_not_called()
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert "Worktree auto-reaped" in response["error"]


def test_cleanup_skipped_without_worktree_dir(socket_path, temp_config, make_lode):
    """Cleanup is skipped when archived lode has no worktree directory."""
    shipped_lode = make_lode(id="test-id", stage="shipped", project="myproject")
    save_lodes([shipped_lode])

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            for _ in range(50):
                if not srv.lodes:
                    break
                time.sleep(0.1)

            mock_remove_worktree.assert_not_called()
            mock_delete_branch.assert_not_called()
        finally:
            srv.stop()
            thread.join(timeout=2)


def test_cleanup_skipped_when_project_not_found(socket_path, temp_config, make_lode):
    """Cleanup is skipped when archived lode project cannot be found."""
    shipped_lode = make_lode(id="test-id", stage="shipped", project="myproject")
    save_lodes([shipped_lode])
    worktree_dir = temp_config / "lodes" / shipped_lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)

    with (
        patch("hopper.server.find_project", return_value=None),
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            for _ in range(50):
                if not srv.lodes:
                    break
                time.sleep(0.1)

            mock_remove_worktree.assert_not_called()
            mock_delete_branch.assert_not_called()
        finally:
            srv.stop()
            thread.join(timeout=2)


def test_server_broadcast_requires_type():
    """Broadcast rejects messages without type field."""
    srv = Server(socket_path="/tmp/unused.sock")

    result = srv.broadcast({"data": "test"})

    assert result is False
    assert srv.broadcast_queue.qsize() == 0


def test_server_broadcast_queues_valid_message():
    """Broadcast queues messages with type field."""
    srv = Server(socket_path="/tmp/unused.sock")

    result = srv.broadcast({"type": "test", "data": "hello"})

    assert result is True
    assert srv.broadcast_queue.qsize() == 1
    msg = srv.broadcast_queue.get_nowait()
    assert msg["type"] == "test"
    assert msg["data"] == "hello"


def test_server_broadcast_removes_exchange_id_from_wire(socket_path):
    server = Server(socket_path)
    conn = _mock_client(server)
    message = {"type": "test", "exchange_id": "must-not-leak"}

    server._send_to_clients(message)

    payload = json.loads(conn.sendall.call_args.args[0].decode().strip())
    assert payload["type"] == "test"
    assert "exchange_id" not in payload
    assert "exchange_id" not in message


def test_server_write_locks_follow_client_lifecycle(socket_path):
    server = Server(socket_path)
    handler_conn = MagicMock()
    recv_entered = threading.Event()
    release_recv = threading.Event()

    def recv(_size):
        recv_entered.set()
        assert release_recv.wait(5)
        return b""

    handler_conn.recv.side_effect = recv
    handler_thread = threading.Thread(
        target=server._handle_client,
        args=(handler_conn,),
        daemon=True,
    )
    handler_thread.start()
    assert recv_entered.wait(5)
    with server.lock:
        assert server.clients == [handler_conn]
        assert set(server.clients) == set(server.write_locks)
    release_recv.set()
    handler_thread.join(timeout=5)
    assert not handler_thread.is_alive()
    with server.lock:
        assert server.clients == []
        assert server.write_locks == {}

    dead_conn = _mock_client(server)
    dead_conn.sendall.side_effect = OSError("dead client")
    server._send_to_clients({"type": "test"})
    with server.lock:
        assert dead_conn not in server.clients
        assert dead_conn not in server.write_locks
    dead_conn.close.assert_called_once()

    stop_conn = _mock_client(server)
    server.stop()
    assert server.clients == []
    assert server.write_locks == {}
    stop_conn.close.assert_called_once()


def test_server_write_lock_preserves_jsonl_framing_at_sub_line_granularity(socket_path):
    class InstrumentedLock:
        def __init__(self):
            self._lock = threading.Lock()
            self._counter_lock = threading.Lock()
            self.attempts = 0
            self.second_attempted = threading.Event()

        def __enter__(self):
            with self._counter_lock:
                self.attempts += 1
                if self.attempts == 2:
                    self.second_attempted.set()
            self._lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback):
            self._lock.release()

    class ChunkedSocket:
        def __init__(self, sock):
            self.sock = sock
            self._counter_lock = threading.Lock()
            self.send_calls = 0
            self.first_chunk_written = threading.Event()
            self.release_first_write = threading.Event()
            self.second_send_entered = threading.Event()

        def settimeout(self, timeout):
            self.sock.settimeout(timeout)

        def sendall(self, data):
            with self._counter_lock:
                self.send_calls += 1
                call_number = self.send_calls
            if call_number == 1:
                split_at = max(1, len(data) // 2)
                self.sock.sendall(data[:split_at])
                self.first_chunk_written.set()
                assert self.release_first_write.wait(5)
                self.sock.sendall(data[split_at:])
                return
            self.second_send_entered.set()
            self.sock.sendall(data)

    server = Server(socket_path)
    sender, receiver = socket.socketpair()
    conn = ChunkedSocket(sender)
    write_lock = InstrumentedLock()
    with server.lock:
        server.clients.append(conn)
        server.write_locks[conn] = write_lock

    response_thread = threading.Thread(
        target=server._send_response,
        args=(conn, {"type": "direct", "value": "response"}),
        daemon=True,
    )
    response_thread.start()
    assert conn.first_chunk_written.wait(5)

    broadcast_thread = threading.Thread(
        target=server._send_to_clients,
        args=({"type": "broadcast", "value": "update"},),
        daemon=True,
    )
    broadcast_thread.start()
    assert write_lock.second_attempted.wait(5)
    assert not conn.second_send_entered.is_set()
    conn.release_first_write.set()

    response_thread.join(timeout=5)
    broadcast_thread.join(timeout=5)
    assert not response_thread.is_alive()
    assert not broadcast_thread.is_alive()
    assert conn.second_send_entered.is_set()

    receiver.settimeout(2)
    wire = b""
    while wire.count(b"\n") < 2:
        wire += receiver.recv(4096)
    lines = wire.splitlines()
    messages = [json.loads(line) for line in lines]
    assert len(messages) == 2
    assert {message["type"] for message in messages} == {"direct", "broadcast"}
    assert [message["value"] for message in messages] == ["response", "update"]

    sender.close()
    receiver.close()


def test_server_sends_shutdown_to_clients(socket_path):
    """Server sends shutdown message to connected clients on stop."""
    srv = Server(socket_path)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()

    assert srv.ready.wait(5), "Server did not start"

    # Connect a client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered by server
    for _ in range(50):
        if len(srv.clients) > 0:
            break
        time.sleep(0.1)

    # Stop server (should send shutdown message)
    srv.stop()

    # Client should receive shutdown message (may get connection reset after)
    try:
        data = client.recv(4096).decode("utf-8")
        messages = [json.loads(line) for line in data.strip().split("\n") if line]
        assert any(msg.get("type") == "shutdown" for msg in messages)
    except ConnectionResetError:
        # If we get reset, the shutdown was sent but connection closed quickly
        # This is acceptable - the important thing is stop() completes cleanly
        pass

    client.close()
    thread.join(timeout=2)


def test_server_handles_connect(socket_path, server):
    """Server handles connect message and returns connected response."""
    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Send connect message
    msg = {"type": "connect"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive connected response
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "connected"
    assert "tmux" in response
    assert response["tmux"] is None  # No tmux location set

    client.close()


def test_server_handles_connect_with_tmux_location(socket_path, temp_config):
    """Server includes tmux location in connect response."""
    tmux_location = {"lode": "main", "pane": "%0"}
    srv = Server(socket_path, tmux_location=tmux_location)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()

    assert srv.ready.wait(5), "Server did not start"

    try:
        # Connect client
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)

        # Send connect message
        msg = {"type": "connect"}
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        # Should receive connected response with tmux location
        data = client.recv(4096).decode("utf-8")
        response = json.loads(data.strip().split("\n")[0])

        assert response["type"] == "connected"
        assert response["tmux"] == {"lode": "main", "pane": "%0"}

        client.close()
    finally:
        srv.stop()
        thread.join(timeout=2)


def test_server_handles_connect_with_lode_id(socket_path, server, temp_config, make_lode):
    """Server returns lode data when lode_id is provided."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Send connect message with lode_id
    msg = {"type": "connect", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive connected response with lode data
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "connected"
    assert response["lode_found"] is True
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["state"] == "new"

    client.close()


def test_server_handles_connect_with_missing_lode_id(socket_path, server):
    """Server returns lode_found=False for unknown lode."""
    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Send connect message with unknown lode_id
    msg = {"type": "connect", "lode_id": "nonexistent"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive connected response with lode not found
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "connected"
    assert response["lode_found"] is False
    assert response["lode"] is None

    client.close()


def test_server_handles_lode_set_state(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_state message."""
    lode = make_lode(id="test-id", state="running", active=True)
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send lode_set_state message
    msg = _runner_message(server, "lode_set_state", "test-id", state="running")
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["state"] == "running"

    # Server's lode should be updated
    assert server.lodes[0]["state"] == "running"

    client.close()


def test_raw_teardown_state_requires_canonical_pending_record(socket_path, make_lode):
    server = Server(socket_path)
    lode = make_lode(
        id="abcd2345",
        state="running",
        active=True,
        run_generation=TEST_RUN_GENERATION,
    )
    server.lodes = [lode]
    conn = _mock_client(server)

    server._handle_mutation(
        _runner_message(
            server,
            "lode_set_state",
            lode["id"],
            state="teardown",
            status="synthetic teardown",
            ack_requested=True,
        ),
        conn,
    )

    assert lode["state"] == "running"
    assert _decode_mock_response(conn) == {
        "type": "mutation_ack",
        "ts": ANY,
        "mutation_type": "lode_set_state",
        "lode_id": lode["id"],
        "accepted": False,
        "reason": "teardown_requires_pending_completion",
    }


def test_server_handles_lode_set_progress(socket_path, server, temp_config, make_lode):
    """Server stores truncated progress heartbeats and broadcasts lode_updated."""
    lode = make_lode(id="test-id", state="running")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    summary = "x" * 200
    msg = _runner_message(server, "lode_set_progress", "test-id", summary=summary)
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["last_progress_summary"] == "x" * 120
    assert response["lode"]["last_progress_at"] is not None
    assert server.lodes[0]["last_progress_summary"] == "x" * 120
    assert server.lodes[0]["last_progress_at"] is not None

    client.close()


@pytest.mark.parametrize("state", ["running", "stuck", "audit"])
def test_server_accepts_progress_for_live_states(socket_path, make_lode, state):
    """Running, stuck, and freeform task states accept progress heartbeats."""
    srv = Server(socket_path)
    lode = make_lode(id="test-id", state=state)
    srv.lodes = [lode]

    with (
        patch("hopper.server.save_lodes") as mock_save,
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        srv._handle_mutation(
            _runner_message(srv, "lode_set_progress", "test-id", summary="working"),
            None,
        )

    assert lode["last_progress_at"] is not None
    assert lode["last_progress_summary"] == "working"
    mock_save.assert_called_once_with(srv.lodes)
    mock_broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})


@pytest.mark.parametrize("state", ["gated", "error"])
def test_server_rejects_progress_for_terminal_or_inactive_states(
    socket_path, make_lode, state, caplog
):
    """Zombie heartbeats cannot mutate, persist, or broadcast inactive lodes."""
    srv = Server(socket_path)
    lode = make_lode(
        id="test-id",
        state=state,
        updated_at=234,
        last_progress_at=123,
        last_progress_summary="existing",
    )
    srv.lodes = [lode]

    with (
        caplog.at_level(logging.DEBUG, logger="hopper.server"),
        patch("hopper.server.save_lodes") as mock_save,
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        srv._handle_mutation(
            _runner_message(srv, "lode_set_progress", "test-id", summary="zombie"),
            None,
        )

    assert lode["last_progress_at"] == 123
    assert lode["last_progress_summary"] == "existing"
    assert lode["updated_at"] == 234
    mock_save.assert_not_called()
    mock_broadcast.assert_not_called()
    assert f"Ignoring progress heartbeat for lode test-id in state={state}" in caplog.messages


def test_server_handles_lode_set_title(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_title message."""
    lode = make_lode(id="test-id", state="running", active=True)
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send lode_set_title message
    msg = _runner_message(server, "lode_set_title", "test-id", title="Auth Flow")
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["title"] == "Auth Flow"

    # Server's lode should be updated
    assert server.lodes[0]["title"] == "Auth Flow"

    client.close()


def test_server_handles_lode_set_branch(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_branch message."""
    lode = make_lode(id="test-id", state="running", active=True)
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    msg = _runner_message(server, "lode_set_branch", "test-id", branch="hopper-test-id-auth-flow")
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["branch"] == "hopper-test-id-auth-flow"
    assert server.lodes[0]["branch"] == "hopper-test-id-auth-flow"

    client.close()


@pytest.mark.parametrize("msg_type", ["lode_pause", "lode_kill", "lode_archive"])
def test_legacy_manual_action_wire_refuses_without_mutation(socket_path, make_lode, msg_type):
    lode = make_lode(
        id="test-id",
        state="running",
        active=True,
        tmux_pane="%1",
        pid=12345,
    )
    before = copy.deepcopy(lode)
    server = Server(socket_path)
    server.lodes = [lode]
    conn = _mock_client(server)

    server._handle_mutation({"type": msg_type, "lode_id": "test-id"}, conn)

    assert lode == before
    assert server.archived_lodes == []
    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == "protocol_upgrade_required"
    assert "retired mixed-version control message" in response["status"]
    assert "Action unbound did not acquire generation none" in response["status"]
    assert "Preserved: worktree, branch, stage session" in response["status"]
    assert "Inspect with: hop lode status test-id" in response["status"]


def test_in_band_action_refusal_projects_only_without_an_action_owner(socket_path, make_lode):
    server = Server(socket_path)
    lode = make_lode(id="test-id", status="stale progress")
    server.lodes = [lode]

    server._send_action_ack(
        None,
        outcome="refused",
        reason="started_stage_requires_force",
        detail="restart requires explicit force consent",
        request={
            "lode_id": "test-id",
            "action_id": "a" * 32,
            "expected_generation": "b" * 32,
            "action_type": "restart",
            "target_disposition": "replacement_spawned",
            "force_consent": False,
        },
    )

    assert lode["status"].startswith("action refused: Restart refused")
    assert "restart requires explicit force consent" in lode["status"]
    assert "Preserved: worktree, branch" in lode["status"]
    assert "hop lode restart test-id" in lode["status"]
    lode["pending_action"] = {"action_id": "a" * 32}
    before = copy.deepcopy(lode)
    server._set_action_refusal("test-id", "new refusal")
    assert lode == before


def test_in_band_action_refusal_reports_divergent_owner_without_persisting(socket_path, make_lode):
    action_id = "a" * 32
    owner_action_id = "b" * 32
    owner_generation = "c" * 32
    request_generation = "d" * 32
    server = Server(socket_path)
    lode = make_lode(
        id="abcd2345",
        status="stale progress",
        run_generation=request_generation,
        pending_action={
            "action_id": owner_action_id,
            "action_type": "completion",
            "expected_generation": owner_generation,
            "preserved": {"worktree": True, "branch": True, "stage_session": True},
        },
    )
    server.lodes = [lode]
    server.action_waiters[action_id] = [(object(), None)]

    with patch.object(server, "_send_response") as send_response:
        server._send_action_ack(
            None,
            outcome="refused",
            reason="ownership_unavailable",
            action_id=action_id,
            action_type="restart",
            detail="generation ownership is absent",
            request={
                "lode_id": lode["id"],
                "action_id": action_id,
                "expected_generation": request_generation,
                "action_type": "restart",
                "target_disposition": "replacement_spawned",
                "force_consent": False,
            },
        )

    response = send_response.call_args.args[1]
    assert request_generation in response["detail"]
    assert owner_generation not in response["detail"]
    assert owner_generation in response["status"]
    assert lode["status"] == "stale progress"
    assert server.broadcast_queue.empty()


def test_in_band_action_refusal_broadcasts_only_the_first_identical_status(socket_path, make_lode):
    action_id = "a" * 32
    request_generation = "d" * 32
    server = Server(socket_path)
    lode = make_lode(id="abcd2345", pending_action=None)
    server.lodes = [lode]
    request = {
        "lode_id": lode["id"],
        "action_id": action_id,
        "expected_generation": request_generation,
        "action_type": "restart",
        "target_disposition": "replacement_spawned",
        "force_consent": False,
    }

    server._send_action_ack(
        None,
        outcome="refused",
        reason="ownership_unavailable",
        action_id=action_id,
        action_type="restart",
        detail="generation ownership is absent",
        request=request,
    )

    first_status = lode["status"]
    assert first_status
    assert first_status.startswith("action refused: ")
    assert server.broadcast_queue.qsize() == 1

    server._send_action_ack(
        None,
        outcome="refused",
        reason="ownership_unavailable",
        action_id=action_id,
        action_type="restart",
        detail="generation ownership is absent",
        request=request,
    )

    assert lode["status"] == first_status
    assert server.broadcast_queue.qsize() == 1


def test_server_resumes_paused_lode_with_existing_stage(socket_path, temp_config, make_lode):
    """Resume spawns the preserved stage session without resetting it."""
    srv = Server(socket_path)
    lode = make_lode(id="test-id", stage="refine", state="paused", active=False, project="proj")
    srv.lodes = [lode]
    conn = _mock_client(srv)

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/fake/repo", name="proj"),
        ),
        patch("hopper.server.spawn_lode_processor", return_value=_spawned("%2")) as mock_spawn,
        patch.object(srv, "broadcast"),
    ):
        srv._handle_mutation({"type": "lode_resume", "lode_id": "test-id"}, conn)

    mock_spawn.assert_called_once_with("test-id", "/fake/repo", foreground=False, env=ANY)
    assert srv.lodes[0]["state"] == "ready"
    assert srv.lodes[0]["active"] is False
    assert lode_driver(srv.lodes[0]) == "claude"
    assert "waiting for handoff registration" in srv.lodes[0]["status"]
    response = _decode_mock_response(conn)
    assert response["type"] == "lode_resumed"
    assert response["tmux_pane"] == "%2"


@pytest.mark.parametrize(
    ("outcome", "status", "guidance"),
    [
        (SpawnOutcome.ALREADY_LIVE, "attach to pane %1", "attach to pane %1"),
        (SpawnOutcome.UNKNOWN, "inspect tmux; do not restart", "do not restart"),
        (SpawnOutcome.PROVEN_NO_PANE, "repair tmux, then restart", "then restart"),
        (SpawnOutcome.PROJECT_MISSING, "restore the project, then restart", "then restart"),
    ],
)
def test_server_resume_failure_response_is_prescriptive(
    socket_path, make_lode, outcome, status, guidance
):
    server = Server(socket_path)
    server.lodes = [
        make_lode(id="test-id", stage="refine", state="paused", project="proj", status=status)
    ]
    conn = _mock_client(server)

    with (
        patch(
            "hopper.server.find_project",
            return_value=Project(path="/fake/repo", name="proj"),
        ),
        patch.object(server, "_gated_spawn", return_value=(outcome, None)),
    ):
        server._handle_mutation({"type": "lode_resume", "lode_id": "test-id"}, conn)

    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert guidance in response["error"]


def test_server_handles_backlog_set_queued(socket_path, server):
    """Server handles backlog_set_queued and broadcasts backlog_updated."""
    item = BacklogItem(
        id="bl111111",
        project="myproj",
        description="Queued item",
        created_at=1000,
    )
    server.backlog = [item]

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    msg = {"type": "backlog_set_queued", "item_id": "bl111111", "queued": "lode1234"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "backlog_updated"
    assert response["item"]["id"] == "bl111111"
    assert response["item"]["queued"] == "lode1234"
    assert server.backlog[0].queued == "lode1234"

    client.close()


def test_auto_promote_backlog_on_ship_stage(socket_path, server, temp_config, make_lode):
    """A raw shipped-stage mutation cannot publish a completion backlog action."""
    lode = make_lode(id="lode1234", project="myproj", stage="ship", state="running", active=True)
    server.lodes = [lode]
    save_lodes(server.lodes)
    server.backlog = [
        BacklogItem(
            id="bl111111",
            project="myproj",
            description="Promote me",
            created_at=1000,
            queued="lode1234",
        )
    ]

    with patch("hopper.server.spawn_lode_processor", return_value=_spawned("%30")) as mock_spawn:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)

        for _ in range(50):
            if len(server.clients) > 0:
                break
            time.sleep(0.1)

        msg = _runner_message(server, "lode_set_stage", "lode1234", stage="shipped")
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        updated = json.loads(client.recv(4096).decode("utf-8").strip().split("\n")[0])
        assert updated["lode"]["id"] == "lode1234"
        assert updated["lode"]["stage"] == "shipped"
        assert server.lodes[0]["stage"] == "shipped"
        assert [item.id for item in server.backlog] == ["bl111111"]
        assert server.backlog[0].queued == "lode1234"
        mock_spawn.assert_not_called()

        client.close()


def test_promote_backlog_item_disabled_project_returns_none(socket_path):
    """Disabled project backlog items are not promoted or removed."""
    srv = Server(socket_path)
    item = BacklogItem(
        id="bl111111",
        project="P",
        description="Promote me",
        created_at=1000,
    )
    srv.backlog = [item]
    disabled = Project(path="/fake/repo", name="P", disabled=True, disabled_reason="wip")

    with (
        patch("hopper.server.find_project", return_value=disabled),
        patch("hopper.server.spawn_lode_processor") as mock_spawn,
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        result = srv._promote_backlog_item(item, coder_provider="codex")

    assert result is None
    assert srv.lodes == []
    assert srv.backlog == [item]
    mock_spawn.assert_not_called()
    mock_broadcast.assert_not_called()


def test_auto_promote_on_ship_disabled_project_does_not_promote(socket_path, make_lode):
    """Auto-promote leaves a disabled project's queued item in place."""
    srv = Server(socket_path)
    lode = make_lode(id="lode1234", project="P", stage="ship")
    item = BacklogItem(
        id="bl111111",
        project="P",
        description="Promote me",
        created_at=1000,
        queued="lode1234",
    )
    srv.lodes = [lode]
    srv.backlog = [item]
    disabled = Project(path="/fake/repo", name="P", disabled=True, disabled_reason="wip")

    with (
        patch("hopper.server.find_project", return_value=disabled),
        patch("hopper.server.spawn_lode_processor") as mock_spawn,
        patch.object(srv, "broadcast") as mock_broadcast,
    ):
        srv._handle_mutation(
            _runner_message(srv, "lode_set_stage", "lode1234", stage="shipped"),
            None,
        )

    assert len(srv.lodes) == 1
    assert srv.backlog == [item]
    assert item.queued == "lode1234"
    assert not any(
        call.args[0].get("type") == "lode_created" for call in mock_broadcast.call_args_list
    )
    mock_spawn.assert_not_called()


def test_lode_promote_backlog_disabled_sends_promote_error(socket_path):
    """Manual promote reports disabled projects with promote_error."""
    srv = Server(socket_path)
    item = BacklogItem(
        id="bl111111",
        project="P",
        description="Promote me",
        created_at=1000,
    )
    srv.backlog = [item]
    conn = _mock_client(srv)
    disabled = Project(path="/fake/repo", name="P", disabled=True, disabled_reason="wip")

    with (
        patch("hopper.server.find_project", return_value=disabled),
        patch("hopper.server.spawn_lode_processor") as mock_spawn,
    ):
        srv._handle_mutation(
            {
                "type": "lode_promote_backlog",
                "item_id": "bl111111",
                "scope": "",
                "coder_provider": "codex",
            },
            conn,
        )

    response = _decode_mock_response(conn)
    assert response["type"] == "promote_error"
    assert "error: project 'P' is disabled" in response["error"]
    assert "  reason: wip" in response["error"]
    assert srv.lodes == []
    assert srv.backlog == [item]
    mock_spawn.assert_not_called()


def test_lode_promote_backlog_requires_explicit_provider_before_durable_write(
    socket_path, temp_config
):
    srv = Server(socket_path)
    item = BacklogItem(
        id="bl111111",
        project="P",
        description="Promote me",
        created_at=1000,
    )
    srv.backlog = [item]
    conn = _mock_client(srv)
    active_path = temp_config / "active.jsonl"
    assert not active_path.exists()

    srv._handle_mutation(
        {"type": "lode_promote_backlog", "item_id": "bl111111"},
        conn,
    )

    response = _decode_mock_response(conn)
    assert response["type"] == "promote_error"
    assert response["error"] == "lode_promote_backlog requires coder_provider"
    assert srv.lodes == []
    assert srv.backlog == [item]
    assert not active_path.exists()


def test_lode_promote_backlog_refuses_unready_default_before_durable_write(
    socket_path, temp_config
):
    srv = Server(socket_path)
    item = BacklogItem(
        id="bl111111",
        project="P",
        description="Promote me",
        created_at=1000,
    )
    srv.backlog = [item]
    conn = _mock_client(srv)
    active_path = temp_config / "active.jsonl"
    assert not active_path.exists()
    readiness = {
        "provider": "codex",
        "ready": False,
        "version": "",
        "error": "codex command not found",
    }

    with patch("hopper.server.coder_check", return_value=readiness):
        srv._handle_mutation(
            {
                "type": "lode_promote_backlog",
                "item_id": "bl111111",
                "coder_provider": "codex",
            },
            conn,
        )

    response = _decode_mock_response(conn)
    assert response["type"] == "promote_error"
    assert response["error"] == "codex unavailable: codex command not found"
    assert srv.lodes == []
    assert srv.backlog == [item]
    assert not active_path.exists()


def test_auto_promote_backlog_on_ship_stage_uses_oldest(
    socket_path, server, temp_config, make_lode
):
    """Raw stage mutation leaves every queued item for the durable action."""
    lode = make_lode(id="lode1234", project="myproj", stage="ship", state="running", active=True)
    server.lodes = [lode]
    save_lodes(server.lodes)
    older = BacklogItem(
        id="bl111111",
        project="myproj",
        description="Older",
        created_at=1000,
        queued="lode1234",
    )
    newer = BacklogItem(
        id="bl222222",
        project="myproj",
        description="Newer",
        created_at=2000,
        queued="lode1234",
    )
    server.backlog = [newer, older]

    with patch("hopper.server.spawn_lode_processor", return_value=_spawned("%31")) as mock_spawn:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)

        for _ in range(50):
            if len(server.clients) > 0:
                break
            time.sleep(0.1)

        msg = _runner_message(server, "lode_set_stage", "lode1234", stage="shipped")
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        updated = json.loads(client.recv(4096).decode("utf-8").strip().split("\n")[0])
        assert updated["lode"]["stage"] == "shipped"
        assert {item.id for item in server.backlog} == {"bl111111", "bl222222"}
        assert all(item.queued == "lode1234" for item in server.backlog)
        mock_spawn.assert_not_called()

        client.close()


def test_auto_promote_chains_multiple_queued_items(socket_path, server, temp_config, make_lode):
    """Raw shipped-stage mutation cannot start a queued-item chain."""
    lode = make_lode(id="lode1234", project="myproj", stage="ship", state="running", active=True)
    server.lodes = [lode]
    save_lodes(server.lodes)
    item_a = BacklogItem(
        id="bl_aaaaaa",
        project="myproj",
        description="A oldest",
        created_at=1000,
        queued="lode1234",
    )
    item_b = BacklogItem(
        id="bl_bbbbbb",
        project="myproj",
        description="B middle",
        created_at=2000,
        queued="lode1234",
    )
    item_c = BacklogItem(
        id="bl_cccccc",
        project="myproj",
        description="C newest",
        created_at=3000,
        queued="lode1234",
    )
    server.backlog = [item_c, item_b, item_a]  # intentionally out of order

    with patch("hopper.server.spawn_lode_processor", return_value=_spawned("%32")) as mock_spawn:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)

        for _ in range(50):
            if len(server.clients) > 0:
                break
            time.sleep(0.1)

        msg = _runner_message(server, "lode_set_stage", "lode1234", stage="shipped")
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        updated = json.loads(client.recv(4096).decode("utf-8").strip().split("\n")[0])
        assert updated["lode"]["stage"] == "shipped"
        assert {item.id for item in server.backlog} == {
            "bl_aaaaaa",
            "bl_bbbbbb",
            "bl_cccccc",
        }
        assert all(item.queued == "lode1234" for item in server.backlog)
        mock_spawn.assert_not_called()
        client.close()


def test_server_connect_does_not_register_ownership(socket_path, server, temp_config, make_lode):
    """Connect message returns lode data but does not register ownership."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "connect", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
    client.recv(4096)

    # Give server time to process
    time.sleep(0.2)

    # Connect should NOT register ownership or set active
    assert "test-id" not in server.lode_clients
    assert server.lodes[0]["active"] is False

    client.close()


def test_server_registers_on_lode_register(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """lode_register message claims ownership and sets active=True."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = _runner_message(server, "lode_register", "test-id", pid=12345)
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    assert "test-id" in server.lode_clients
    assert server.lodes[0]["active"] is True
    assert server.lodes[0]["pid"] == 12345

    client.close()


def test_server_sets_active_false_on_disconnect(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Server sets active=False and clears tmux_pane on client disconnect."""
    lode = make_lode(id="test-id", state="running", tmux_pane="%1")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client and register ownership
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = _runner_message(server, "lode_register", "test-id")
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    assert server.lodes[0]["active"] is True

    # Disconnect client
    client.close()

    # Wait for disconnect handling
    for _ in range(50):
        if not server.lodes[0]["active"]:
            break
        time.sleep(0.1)

    # active=False, tmux_pane cleared, but state/status untouched
    assert server.lodes[0]["active"] is False
    assert server.lodes[0]["tmux_pane"] is None
    assert server.lodes[0]["state"] == "running"
    assert "test-id" not in server.lode_clients


def test_server_clears_pid_on_disconnect(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Server clears pid on client disconnect."""
    lode = make_lode(id="test-id", state="running", pid=54321)
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = _runner_message(server, "lode_register", "test-id", pid=12345)
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    for _ in range(50):
        if server.lodes[0]["pid"] == 12345:
            break
        time.sleep(0.1)

    assert server.lodes[0]["pid"] == 12345

    client.close()

    for _ in range(50):
        if server.lodes[0]["pid"] is None:
            break
        time.sleep(0.1)

    assert server.lodes[0]["active"] is False
    assert server.lodes[0]["pid"] is None


def test_server_preserves_state_on_disconnect(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Server preserves state and status on client disconnect (only toggles active)."""
    lode = make_lode(id="test-id", state="error", status="Something failed", tmux_pane="%1")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client and register ownership
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = _runner_message(server, "lode_register", "test-id")
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    # Disconnect client
    client.close()

    # Wait for disconnect handling
    for _ in range(50):
        if server.lodes[0]["tmux_pane"] is None:
            break
        time.sleep(0.1)

    # State and status preserved, active set to False
    assert server.lodes[0]["state"] == "error"
    assert server.lodes[0]["status"] == "Something failed"
    assert server.lodes[0]["active"] is False
    assert server.lodes[0]["tmux_pane"] is None


def test_server_handles_ready_state(socket_path, server, temp_config, make_lode):
    """Server accepts 'ready' as a valid state."""
    lode = make_lode(id="test-id", stage="refine", state="completed")
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    msg = _runner_message(
        server,
        "lode_set_state",
        "test-id",
        state="ready",
        status="Mill output saved",
    )
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["state"] == "ready"
    assert response["lode"]["status"] == "Mill output saved"

    client.close()


def test_auto_spawn_on_disconnect(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Disconnect only clears ownership; durable completion owns spawning."""
    lode = make_lode(
        id="test-id",
        state="ready",
        stage="ship",
        status="Refine complete",
        project="my-project",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    with (
        patch("hopper.server.find_project") as mock_find,
        patch("hopper.server.spawn_lode_processor", return_value=_spawned("%33")) as mock_spawn,
    ):
        mock_find.return_value = MagicMock(path="/some/path")

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        msg = _runner_message(server, "lode_register", "test-id")
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        for _ in range(50):
            if "test-id" in server.lode_clients:
                break
            time.sleep(0.1)

        client.close()

        for _ in range(20):
            time.sleep(0.1)
            if not server.lodes[0]["active"]:
                break

        mock_spawn.assert_not_called()
        assert server.lodes[0]["stage"] == "ship"
        assert server.lodes[0]["state"] == "running"


def test_auto_archive_shipped_on_disconnect(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Disconnect cannot archive without a durable completion action."""
    lode = make_lode(
        id="test-id",
        stage="shipped",
        state="ready",
        status="Ship complete",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)
    msg = _runner_message(server, "lode_register", "test-id")
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    client.close()

    for _ in range(20):
        time.sleep(0.1)
        if not server.lodes[0]["active"]:
            break

    assert len(server.lodes) == 1
    assert server.lodes[0]["id"] == "test-id"
    assert server.lodes[0]["active"] is False
    assert server.archived_lodes == []

    archived_file = temp_config / "archived.jsonl"
    assert not archived_file.read_text().strip()


def test_lode_unarchive(socket_path, server, temp_config, make_lode):
    """Unarchive moves lode from archived to active and broadcasts."""
    lode = make_lode(id="test-id", stage="mill", state="new")
    lode["archived_at"] = 5000
    server.archived_lodes = [lode]
    save_archived_lodes(server.archived_lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    msg = {"type": "lode_unarchive", "lode_id": "test-id"}
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    for _ in range(50):
        if server.lodes:
            break
        time.sleep(0.1)

    assert len(server.lodes) == 1
    assert server.lodes[0]["id"] == "test-id"
    assert "archived_at" not in server.lodes[0]
    assert server.archived_lodes == []

    client.close()


def test_cleanup_worktree_on_disconnect_archive(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Disconnect never invokes completion-owned destructive cleanup."""
    lode = make_lode(
        id="test-id",
        stage="shipped",
        state="ready",
        status="Ship complete",
        project="myproject",
        branch="hopper-test-id",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)
    worktree_dir = temp_config / "lodes" / lode["id"] / "worktree"
    worktree_dir.mkdir(parents=True)

    with (
        patch(
            "hopper.server.find_project", return_value=Project(path="/fake/repo", name="myproject")
        ),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree") as mock_remove_worktree,
        patch("hopper.server.delete_branch") as mock_delete_branch,
    ):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        msg = _runner_message(server, "lode_register", "test-id")
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        for _ in range(50):
            if "test-id" in server.lode_clients:
                break
            time.sleep(0.1)

        client.close()

        for _ in range(20):
            time.sleep(0.1)
            if not server.lodes[0]["active"]:
                break

        mock_remove_worktree.assert_not_called()
        mock_delete_branch.assert_not_called()
        assert worktree_dir.exists()
        assert server.lodes == [lode]


def test_no_auto_archive_non_shipped_on_disconnect(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Non-shipped lodes are not auto-archived on disconnect."""
    lode = make_lode(
        id="test-id",
        stage="ship",
        state="ready",
        status="Ship complete",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)
    msg = _runner_message(server, "lode_register", "test-id")
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    client.close()

    for _ in range(20):
        time.sleep(0.1)
        if not server.lodes[0]["active"]:
            break

    assert len(server.lodes) == 1
    assert server.lodes[0]["id"] == "test-id"
    assert server.archived_lodes == []


def test_auto_spawn_skipped_when_stage_done(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Auto-advance does not spawn when current stage is already complete."""
    lode = make_lode(
        id="test-id",
        state="ready",
        stage="ship",
        status="Ship complete",
        project="my-project",
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    with (
        patch("hopper.server.find_project") as mock_find,
        patch("hopper.server.spawn_lode_processor") as mock_spawn,
    ):
        mock_find.return_value = MagicMock(path="/some/path")

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        msg = _runner_message(server, "lode_register", "test-id")
        client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        for _ in range(50):
            if "test-id" in server.lode_clients:
                break
            time.sleep(0.1)

        client.close()

        for _ in range(20):
            time.sleep(0.1)
            if not server.lodes[0]["active"]:
                break

        mock_spawn.assert_not_called()


def test_server_disconnects_stale_client_on_reconnect(
    socket_path, server, temp_config, make_lode, registered_generation_capture
):
    """Server disconnects old client when new client registers for same lode."""
    lode = make_lode(id="test-id")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # First client registers
    client1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client1.connect(str(socket_path))
    client1.settimeout(2.0)

    msg = _runner_message(server, "lode_register", "test-id")
    client1.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for registration
    for _ in range(50):
        if "test-id" in server.lode_clients:
            break
        time.sleep(0.1)

    old_socket = server.lode_clients["test-id"]

    # Second client registers for same lode
    client2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client2.connect(str(socket_path))
    client2.settimeout(2.0)

    msg = _runner_message(server, "lode_register", "test-id")
    client2.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Wait for re-registration
    for _ in range(50):
        if server.lode_clients.get("test-id") != old_socket:
            break
        time.sleep(0.1)

    # Second client should now own the lode
    assert "test-id" in server.lode_clients
    assert server.lode_clients["test-id"] != old_socket

    client1.close()
    client2.close()


def test_server_handles_legacy_lode_set_codex_thread(socket_path, server, temp_config, make_lode):
    """Server preserves the existing Codex runner mutation contract."""
    lode = make_lode(id="test-id", stage="refine", state="running")
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send the pre-Grok Codex mutation shape.
    msg = _runner_message(
        server,
        "lode_set_codex_thread",
        "test-id",
        codex_thread_id="codex-uuid-1234",
    )
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert response["lode"]["codex_thread_id"] == "codex-uuid-1234"
    assert "coder" not in response["lode"]

    # Server's lode should be updated
    assert server.lodes[0]["codex_thread_id"] == "codex-uuid-1234"

    client.close()


def test_server_refuses_codex_thread_mutation_for_grok_lode(server, make_lode, caplog):
    lode = make_lode(
        id="test-id",
        stage="refine",
        state="running",
        coder={"provider": "grok", "session_id": None},
    )
    server.lodes = [lode]
    before = copy.deepcopy(lode)

    with caplog.at_level(logging.WARNING):
        server._handle_mutation(
            _runner_message(
                server,
                "lode_set_codex_thread",
                "test-id",
                codex_thread_id="grok-session",
            ),
            None,
        )

    assert lode == {**before, "run_generation": TEST_RUN_GENERATION}
    assert "Refusing invalid Codex thread mutation" in caplog.text


def test_server_handles_lode_set_coder_session(socket_path, server, temp_config, make_lode):
    """Server stores the additive Grok session mutation."""
    lode = make_lode(
        id="test-id",
        stage="refine",
        state="running",
        coder={"provider": "grok", "session_id": None},
    )
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    msg = _runner_message(
        server,
        "lode_set_coder_session",
        "test-id",
        provider="grok",
        session_id="grok-uuid-1234",
    )
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["lode"]["coder"]["session_id"] == "grok-uuid-1234"
    assert server.lodes[0]["coder"]["session_id"] == "grok-uuid-1234"

    client.close()


def test_server_refuses_invalid_coder_session_provider(server, make_lode, caplog):
    lode = make_lode(
        id="test-id",
        stage="refine",
        state="running",
        coder={"provider": "grok", "session_id": None},
    )
    server.lodes = [lode]
    before = copy.deepcopy(lode)

    with caplog.at_level(logging.WARNING):
        server._handle_mutation(
            _runner_message(
                server,
                "lode_set_coder_session",
                "test-id",
                provider="invalid",
                session_id="session-123",
            ),
            None,
        )

    assert lode == {**before, "run_generation": TEST_RUN_GENERATION}
    assert "Refusing invalid coder session mutation" in caplog.text


def test_server_handles_lode_set_claude_started(socket_path, server, temp_config, make_lode):
    """Server handles lode_set_claude_started message."""
    lode = make_lode(id="test-id", stage="mill", state="running")
    assert lode_stage_session(lode, "mill")["started"] is False
    server.lodes = [lode]
    save_lodes(server.lodes)

    # Connect client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    # Wait for client to be registered
    for _ in range(50):
        if len(server.clients) > 0:
            break
        time.sleep(0.1)

    # Send lode_set_claude_started message
    msg = _runner_message(server, "lode_set_claude_started", "test-id", claude_stage="mill")
    client.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Should receive broadcast
    data = client.recv(4096).decode("utf-8")
    response = json.loads(data.strip().split("\n")[0])

    assert response["type"] == "lode_updated"
    assert response["lode"]["id"] == "test-id"
    assert lode_stage_session(response["lode"], "mill")["started"] is True

    # Server's lode should be updated
    assert lode_stage_session(server.lodes[0], "mill")["started"] is True
    # Other stages unchanged
    assert lode_stage_session(server.lodes[0], "refine")["started"] is False

    client.close()


def test_server_refuses_legacy_lode_reset_claude_stage(socket_path, server, temp_config, make_lode):
    lode = make_lode(id="test-id", stage="mill", state="running")
    _mark_stage_started(lode, "mill")
    before = copy.deepcopy(lode)
    server.lodes = [lode]
    save_lodes(server.lodes)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)
    client.sendall(
        (
            json.dumps(
                {
                    "type": "lode_reset_claude_stage",
                    "lode_id": "test-id",
                    "claude_stage": "mill",
                }
            )
            + "\n"
        ).encode("utf-8")
    )
    response = json.loads(client.recv(4096).decode("utf-8").strip().split("\n")[0])

    assert response["type"] == "lode_action_ack"
    assert response["accepted"] is False
    assert response["reason"] == "protocol_upgrade_required"
    assert server.lodes[0] == before
    client.close()


# Constructed from the prep-verified U+2500 rules, U+276F prompt, and U+00A0 spacing.
_IDLE_EMPTY_CAPTURE = "─────\n❯ \n─────\n"
_IDLE_STAGED_CAPTURE = "─────\n❯ Please revise\n─────\n"
# Constructed from the real processing capture's observed content shape.
_PROCESSING_CAPTURE = "Please revise\n● Working…\n"
# A Claude numbered selector whose third option is free-text. Free-text paste still
# cannot drive this surface; numbered answers navigate the highlight explicitly.
_IDLE_QUESTION_CAPTURE = (
    "Which cutover should I take?\n"
    "❯ 1. Delete the Python wire now\n"
    "  2. Keep both behind a flag\n"
    "  3. Type something.\n"
    "↑/↓ to navigate · Enter to select · Esc to cancel\n"
)
_IDLE_QUESTION_THIRD_CAPTURE = (
    "Which cutover should I take?\n"
    "  1. Delete the Python wire now\n"
    "  2. Keep both behind a flag\n"
    "❯ 3. Ship the Rust wire\n"
    "  4. Type something.\n"
    "↑/↓ to navigate · Enter to select · Esc to cancel\n"
)
# Constructed from the existing selector fixture with the same legacy tuple shape but new content.
_FOLLOW_ON_QUESTION_CAPTURE = (
    "Which release should ship?\n"
    "❯ 1. Ship the patch release\n"
    "  2. Ship the minor release\n"
    "  3. Type something.\n"
    "↑/↓ to navigate · Enter to select · Esc to cancel\n"
)
# Paired fixtures constructed from the existing selector fixture with a changing
# unnumbered ancillary control.
_QUESTION_ANCILLARY_FIRST_CAPTURE = (
    "Which cutover should I take?\n"
    "❯ 1. Delete the Python wire now\n"
    "  2. Keep both behind a flag\n"
    "  3. Type something.\n"
    "  n to add notes\n"
    "↑/↓ to navigate · Enter to select · Esc to cancel\n"
)
_QUESTION_ANCILLARY_SECOND_CAPTURE = _QUESTION_ANCILLARY_FIRST_CAPTURE.replace(
    "n to add notes", "n notes unavailable"
)
_EDITED_FREE_TEXT_QUESTION_CAPTURE = (
    "  1. Widen scope to include it\n"
    "  2. Leave it; relax AC1\n"
    "  3. deny.toml-only stopgap for it\n"
    "❯ 4. 3\n"
    "────────────────────────────────────────────────────────────────\n"
    "  5. Chat about this\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
)
# Constructed from the verified staged-composer shape. No agent was killed during prep; the
# surviving Claude frame and shell prompt written beneath it are inferred from the specified
# killed-agent pane behavior.
_DEAD_AGENT_STAGED_CAPTURE = (
    "Claude's last rendered response\n─────\n❯ Please revise\n─────\nextro@host:~/project$ \n"
)
# Constructed from the verified composer shape with its closing U+2500 rule removed.
_TORN_COMPOSER_CAPTURE = "─────\n❯ \n"
# Constructed from the shell prompt portion of the inferred killed-agent shape above.
_SHELL_PROMPT_CAPTURE = "extro@host:~/project$ \n"


def _handle_delivery_with_tmux(
    srv,
    conn,
    *,
    captures,
    titles,
    paste=True,
    submit=True,
    text="Please revise",
    message_type="lode_send_feedback",
    input_paste=True,
):
    """Run the real delivery state machine with only tmux boundaries patched."""
    message = {"type": message_type, "lode_id": "test-id", "text": text}
    if message_type == "lode_send_pane_input":
        message["paste"] = input_paste
    with (
        patch("hopper.server.capture_pane", side_effect=captures) as mock_capture,
        patch("hopper.server.pane_title", side_effect=titles) as mock_title,
        patch("hopper.server.paste_buffer", return_value=paste) as mock_paste,
        patch("hopper.server.send_keys", return_value=submit) as mock_send,
        patch("hopper.server.time.sleep") as mock_sleep,
    ):
        srv._handle_mutation(message, conn)
    return mock_capture, mock_title, mock_paste, mock_send, mock_sleep


def _observe_acceptance_with_tmux(captures, titles, evidence):
    """Run acceptance polling with only its existing tmux boundaries patched."""
    with (
        patch("hopper.server.capture_pane", side_effect=captures) as mock_capture,
        patch("hopper.server.pane_title", side_effect=titles) as mock_title,
        patch("hopper.server.time.sleep") as mock_sleep,
    ):
        result = hopper_server._observe_pane_acceptance(
            "%1",
            _IDLE_STAGED_CAPTURE,
            "✳ Ready",
            evidence,
        )
    return result, mock_capture, mock_title, mock_sleep


def test_lode_send_feedback_alive_pane_sends_keys(socket_path, make_lode):
    """Idle staged feedback resumes only after the title becomes processing."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", stage="refine", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[
            _IDLE_EMPTY_CAPTURE,
            _IDLE_EMPTY_CAPTURE,
            _IDLE_STAGED_CAPTURE,
            _PROCESSING_CAPTURE,
        ],
        titles=["✳ Ready", "✳ Ready", "⠐ Working"],
        text="Looks good",
    )

    mock_paste.assert_called_once_with("%1", "Looks good")
    mock_send.assert_called_once_with("%1", "Enter")
    assert srv.lodes[0]["state"] == "running"
    assert srv.lodes[0]["status"] == "Feedback accepted"
    assert srv.lodes[0]["gate_epoch"] == 0
    assert srv.lodes[0]["gate_body"] is None
    broadcast = srv.broadcast_queue.get_nowait()
    assert broadcast["type"] == "lode_updated"
    response = _decode_mock_response(conn)
    assert response["type"] == "feedback_sent"
    assert response["lode_id"] == "test-id"
    assert response["tmux_pane"] == "%1"
    assert "submitted" not in response
    assert "tail" not in response


def test_feedback_state_change_is_logged(socket_path, make_lode, caplog):
    """A state write with no log line is unreconstructable afterwards.

    `lode_send_feedback` wrote `running` over a park and left no record, so the
    eight hours in which that lode looked alive had nothing explaining why. Every
    writer logs, and `via` names which one.
    """
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", stage="refine", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    with caplog.at_level(logging.INFO, logger="hopper.server"):
        _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[
                _IDLE_EMPTY_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _IDLE_STAGED_CAPTURE,
                _PROCESSING_CAPTURE,
            ],
            titles=["✳ Ready", "✳ Ready", "⠐ Working"],
            text="Looks good",
        )

    assert srv.lodes[0]["state"] == "running"
    lines = [r.getMessage() for r in caplog.records]
    assert any("Lode test-id state=running" in line and "via=feedback" in line for line in lines), (
        lines
    )


def test_lode_send_feedback_dead_pane_fails_closed(socket_path, make_lode):
    """Dead pane feedback stays gated and requires an explicit resume."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="gated", tmux_pane="%dead")]
    conn = _mock_client(srv)

    with patch("hopper.server.spawn_lode_processor") as mock_spawn:
        _capture, mock_title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[None],
            titles=[],
            text="y",
        )

    mock_spawn.assert_not_called()
    mock_title.assert_not_called()
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback blocked: pane unavailable"
    assert srv.lodes[0]["gate_epoch"] == 2
    assert srv.lodes[0]["gate_body"] == "Gate"
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["outcome"] == "pane_unavailable"
    assert "hop lode resume test-id" in response["error"]
    assert "tail" not in response


def test_lode_send_feedback_paste_failure_remains_gated(socket_path, make_lode):
    """A failed paste is proven not sent and remains gated."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE] * 3,
        titles=["✳ Ready"],
        paste=False,
    )

    mock_paste.assert_called_once_with("%1", "Please revise")
    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback not sent; gate remains blocked"
    assert srv.lodes[0]["gate_epoch"] == 1
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["outcome"] == "not_sent"
    assert "Retry the same feedback" in response["error"]
    assert response["tail"] == _IDLE_EMPTY_CAPTURE.rstrip()


def test_lode_send_feedback_changed_input_after_paste_failure_is_unverified(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE, _IDLE_EMPTY_CAPTURE, _IDLE_STAGED_CAPTURE],
        titles=["✳ Ready"],
        paste=False,
    )

    mock_paste.assert_called_once_with("%1", "Please revise")
    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback outcome unknown; inspect pane"
    assert srv.lodes[0]["gate_epoch"] == 1
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["outcome"] == "unverified"
    assert "some feedback text may have reached the pane" in response["error"]
    assert "hop lode peek test-id" in response["error"]
    assert "do not paste the feedback again" in response["error"]
    assert response["tail"] == _IDLE_STAGED_CAPTURE.rstrip()


def test_lode_send_feedback_pane_disappears_after_paste(socket_path, make_lode):
    """Pane death after paste has an unknown outcome and forbids blind replay."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE, _IDLE_EMPTY_CAPTURE, None],
        titles=["✳ Ready"],
    )

    mock_paste.assert_called_once()
    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback outcome unknown; inspect pane"
    assert srv.lodes[0]["gate_epoch"] == 1
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["outcome"] == "unverified"
    assert "delivery outcome is unknown" in response["error"]
    assert "do not paste the feedback again" in response["error"]


def test_lode_send_feedback_missing_lode(socket_path):
    """Missing lode feedback request returns an unknown-lode response."""
    srv = Server(socket_path)
    conn = _mock_client(srv)

    srv._handle_mutation(
        {"type": "lode_send_feedback", "lode_id": "missing", "text": "feedback"},
        conn,
    )

    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["outcome"] == "unknown_lode"
    assert response["error"] == (
        "Lode missing was not found on this server. No pane was touched. Check the lode ID, "
        "then retry."
    )


def test_gate_feedback_waits_for_idle_before_touching_pane(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)
    actions = []

    def title(*_args):
        actions.append("title")
        return ["⠐ Working", "", "✳ Ready", "⠐ Working"][actions.count("title") - 1]

    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[
                _IDLE_EMPTY_CAPTURE,
                _PROCESSING_CAPTURE,
                _PROCESSING_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _PROCESSING_CAPTURE,
            ],
        ),
        patch("hopper.server.pane_title", side_effect=title),
        patch(
            "hopper.server.paste_buffer",
            side_effect=lambda *_args: actions.append("paste") or True,
        ),
        patch("hopper.server.send_keys", return_value=True) as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        srv._handle_mutation(
            {"type": "lode_send_feedback", "lode_id": "test-id", "text": "feedback"},
            conn,
        )

    assert actions == ["title", "title", "title", "paste", "title"]
    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "running"


def test_gate_feedback_pane_lost_while_waiting_for_idle_is_unavailable(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, mock_title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE, None],
        titles=[],
    )

    mock_title.assert_not_called()
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback blocked: pane unavailable"
    assert srv.lodes[0]["gate_epoch"] == 1
    assert srv.lodes[0]["gate_body"] == srv.lodes[0]["status"]
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["outcome"] == "pane_unavailable"
    assert "hop lode resume test-id" in response["error"]
    assert response["tail"] == _PROCESSING_CAPTURE.rstrip()


def test_gate_feedback_busy_for_entire_idle_wait_never_touches_pane(socket_path, make_lode, caplog):
    """A continuously processing pane exhausts the idle wait as busy."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    with caplog.at_level(logging.WARNING, logger="hopper.server"):
        _capture, mock_title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[_PROCESSING_CAPTURE] * 13,
            titles=["⠐ Working"] * 12,
        )

    assert mock_title.call_count == 12
    assert mock_sleep.call_count == 12
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert srv.lodes[0]["status"] == "Feedback blocked: pane busy"
    assert srv.lodes[0]["gate_epoch"] == 1
    assert srv.lodes[0]["gate_body"] == srv.lodes[0]["status"]
    assert _decode_mock_response(conn)["outcome"] == "busy"
    assert "reason=idle_timeout outcome=busy" in caplog.text


def test_gate_feedback_circle_processing_title_reports_busy(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, mock_title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE] * 13,
        titles=["◐ Working"] * 12,
    )

    assert mock_title.call_count == 12
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert _decode_mock_response(conn)["outcome"] == "busy"


def test_gate_feedback_identical_old_processing_title_is_frozen_after_idle_wait(
    socket_path, make_lode
):
    idle_poll_window_ms = int(hopper_server._FEEDBACK_IDLE_WAIT_SECONDS * 1000)
    assert hopper_server.FROZEN_PANE_THRESHOLD_MS > idle_poll_window_ms * 100
    observed_at = 1_000
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="running",
            tmux_pane="%1",
            pane_title_observation={"title": "⠐ Working", "observed_at": observed_at},
        )
    ]
    conn = _mock_client(srv)

    with patch(
        "hopper.server.current_time_ms",
        return_value=observed_at + hopper_server.FROZEN_PANE_THRESHOLD_MS,
    ):
        _capture, mock_title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[_PROCESSING_CAPTURE] * 13,
            titles=["⠐ Working"] * 12,
        )

    assert mock_title.call_count == 12
    assert mock_sleep.call_count == 12
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    response = _decode_mock_response(conn)
    assert response["outcome"] == "pane_frozen"
    assert f"at least {hopper_server.FROZEN_PANE_THRESHOLD_MS // 60_000} min" in response["error"]


def test_gate_feedback_circle_processing_title_past_threshold_is_frozen(socket_path, make_lode):
    observed_at = 1_000
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="running",
            tmux_pane="%1",
            pane_title_observation={"title": "◐ Working", "observed_at": observed_at},
        )
    ]
    conn = _mock_client(srv)

    with patch(
        "hopper.server.current_time_ms",
        return_value=observed_at + hopper_server.FROZEN_PANE_THRESHOLD_MS,
    ):
        _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[_PROCESSING_CAPTURE] * 13,
            titles=["◐ Working"] * 12,
        )

    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert _decode_mock_response(conn)["outcome"] == "pane_frozen"


def test_gate_feedback_old_idle_title_still_delivers(socket_path, make_lode):
    observed_at = 1_000
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="running",
            tmux_pane="%1",
            pane_title_observation={"title": "✳ Ready", "observed_at": observed_at},
        )
    ]
    conn = _mock_client(srv)

    with patch(
        "hopper.server.current_time_ms",
        return_value=observed_at + hopper_server.FROZEN_PANE_THRESHOLD_MS,
    ):
        _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[
                _IDLE_EMPTY_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _IDLE_STAGED_CAPTURE,
                _PROCESSING_CAPTURE,
            ],
            titles=["✳ Ready", "✳ Ready", "⠐ Working"],
        )

    mock_paste.assert_called_once_with("%1", "Please revise")
    mock_send.assert_called_once_with("%1", "Enter")
    assert _decode_mock_response(conn)["type"] == "feedback_sent"
    assert srv.lodes[0]["pane_title_observation"] is None


def test_gate_feedback_old_unknown_title_remains_unknown(socket_path, make_lode):
    observed_at = 1_000
    unknown_title = "_ Waiting for an external state"
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="running",
            tmux_pane="%1",
            pane_title_observation={"title": unknown_title, "observed_at": observed_at},
        )
    ]
    conn = _mock_client(srv)

    with patch(
        "hopper.server.current_time_ms",
        return_value=observed_at + hopper_server.FROZEN_PANE_THRESHOLD_MS,
    ):
        _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[_PROCESSING_CAPTURE] * 13,
            titles=[unknown_title] * 12,
        )

    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert _decode_mock_response(conn)["outcome"] == "pane_state_unknown"
    assert srv.lodes[0]["pane_title_observation"] is None


def test_gate_feedback_changed_title_resets_observation_and_keeps_polling(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="running",
            tmux_pane="%1",
            pane_title_observation={"title": "⠂ Working", "observed_at": 1_000},
        )
    ]
    conn = _mock_client(srv)

    with patch("hopper.server.current_time_ms", return_value=700_000):
        _capture, mock_title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[_PROCESSING_CAPTURE] * 13,
            titles=["⠐ Working"] * 12,
        )

    assert mock_title.call_count == 12
    assert mock_sleep.call_count == 12
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert _decode_mock_response(conn)["outcome"] == "busy"
    assert srv.lodes[0]["pane_title_observation"] == {
        "title": "⠐ Working",
        "observed_at": 700_000,
    }


def test_gate_feedback_processing_then_unknown_remains_busy_and_never_touches_pane(
    socket_path, make_lode
):
    """PROCESSING followed by UNKNOWN remains idle_timeout because processing was seen."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, mock_title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE] * 13,
        titles=["⠐ Working"] * 6 + ["_ Land native skills port to main"] * 6,
    )

    assert mock_title.call_count == 12
    assert mock_sleep.call_count == 12
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert srv.lodes[0]["status"] == "Feedback blocked: pane busy"
    assert srv.lodes[0]["gate_epoch"] == 1
    assert srv.lodes[0]["gate_body"] == srv.lodes[0]["status"]
    assert _decode_mock_response(conn)["outcome"] == "busy"


def test_gate_feedback_auto_submit_accepts_without_enter(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE, _IDLE_EMPTY_CAPTURE, _PROCESSING_CAPTURE],
        titles=["✳ Ready", "⠐ Working"],
    )

    mock_paste.assert_called_once()
    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "running"
    assert _decode_mock_response(conn)["type"] == "feedback_sent"


def test_gate_feedback_placeholder_is_staged_not_accepted(socket_path, make_lode):
    # Constructed from prep-verified U+2500 rules, U+276F prompt, and U+00A0 spacing.
    placeholder = "───\n❯ [Pasted text #1 +40 lines]\n───\n"
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, _paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[
            _IDLE_EMPTY_CAPTURE,
            _IDLE_EMPTY_CAPTURE,
            placeholder,
            _PROCESSING_CAPTURE,
        ],
        titles=["✳ Ready", "✳ Ready", "⠐ Working"],
    )

    mock_send.assert_called_once_with("%1", "Enter")
    assert srv.lodes[0]["state"] == "running"


def test_pane_delivery_accepts_answer_when_prior_selector_disappears_without_working_title():
    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[
                _IDLE_QUESTION_CAPTURE,
                _IDLE_QUESTION_CAPTURE,
                _IDLE_QUESTION_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
            ],
        ),
        patch("hopper.server.pane_title", side_effect=["✳ Ready"] * 3),
        patch("hopper.server.send_keys", return_value=True) as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "1", paste=False)

    assert result["reason"] == "selector_changed"
    mock_send.assert_called_once_with("%1", "Enter")


def test_pane_delivery_accepts_paste_when_settled_input_clears_without_working_title():
    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[
                _IDLE_EMPTY_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _IDLE_STAGED_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
            ],
        ),
        patch("hopper.server.pane_title", side_effect=["✳ Ready"] * 4),
        patch("hopper.server.paste_buffer", return_value=True),
        patch("hopper.server.send_keys", return_value=True) as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "Please revise", paste=True)

    assert result["reason"] == "composer_cleared"
    mock_send.assert_called_once_with("%1", "Enter")


def test_pane_delivery_accepts_answer_followed_by_different_same_shape_selector():
    assert hopper_server.pane_answer_choices(
        _IDLE_QUESTION_CAPTURE
    ) == hopper_server.pane_answer_choices(_FOLLOW_ON_QUESTION_CAPTURE)
    assert hopper_server.pane_answer_identity(
        _IDLE_QUESTION_CAPTURE
    ) != hopper_server.pane_answer_identity(_FOLLOW_ON_QUESTION_CAPTURE)

    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[
                _IDLE_QUESTION_CAPTURE,
                _IDLE_QUESTION_CAPTURE,
                _IDLE_QUESTION_CAPTURE,
                _FOLLOW_ON_QUESTION_CAPTURE,
                _FOLLOW_ON_QUESTION_CAPTURE,
            ],
        ),
        patch("hopper.server.pane_title", side_effect=["✳ Ready"] * 3),
        patch("hopper.server.send_keys", return_value=True),
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "1", paste=False)

    assert result["reason"] == "selector_changed"


def test_pane_delivery_ancillary_selector_change_does_not_establish_acceptance():
    expected_identity = hopper_server.pane_answer_identity(_IDLE_QUESTION_CAPTURE)
    assert (
        hopper_server.pane_answer_identity(_QUESTION_ANCILLARY_FIRST_CAPTURE) == expected_identity
    )
    assert (
        hopper_server.pane_answer_identity(_QUESTION_ANCILLARY_SECOND_CAPTURE) == expected_identity
    )

    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[_IDLE_QUESTION_CAPTURE] * 3
            + [
                _QUESTION_ANCILLARY_FIRST_CAPTURE,
                _QUESTION_ANCILLARY_SECOND_CAPTURE,
            ]
            * 6,
        ),
        patch("hopper.server.pane_title", side_effect=["✳ Ready"] * 13),
        patch("hopper.server.send_keys", return_value=True),
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "1", paste=False)

    assert result["reason"] == "acceptance_timeout"


def test_pane_delivery_accepts_circle_processing_title_fast_path():
    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[
                _IDLE_EMPTY_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _IDLE_STAGED_CAPTURE,
                _PROCESSING_CAPTURE,
            ],
        ),
        patch("hopper.server.pane_title", side_effect=["✳ Ready", "✳ Ready", "◐ Working"]),
        patch("hopper.server.paste_buffer", return_value=True),
        patch("hopper.server.send_keys", return_value=True),
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "Please revise", paste=True)

    assert result["reason"] == "enter_accepted"


def test_pane_delivery_auto_submits_with_circle_processing_title():
    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[_IDLE_EMPTY_CAPTURE, _IDLE_EMPTY_CAPTURE, _PROCESSING_CAPTURE],
        ),
        patch("hopper.server.pane_title", side_effect=["✳ Ready", "◑ Working"]),
        patch("hopper.server.paste_buffer", return_value=True),
        patch("hopper.server.send_keys") as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "Please revise", paste=True)

    assert result["reason"] == "auto_submitted"
    mock_send.assert_not_called()


@pytest.mark.parametrize(
    "capture",
    [
        pytest.param("", id="successful-empty-capture"),
        pytest.param("   \n\t\n", id="whitespace-only"),
        pytest.param(_SHELL_PROMPT_CAPTURE, id="shell-prompt"),
    ],
)
def test_pane_acceptance_unreadable_capture_times_out(capture):
    # Empty and whitespace shapes exercise successful captures; the shell shape is defined above.
    result, mock_capture, mock_title, _sleep = _observe_acceptance_with_tmux(
        [capture] * 12,
        ["✳ Ready"] * 12,
        ("composer", "Please revise"),
    )

    assert result["reason"] == "acceptance_timeout"
    assert mock_capture.call_count == 12
    assert mock_title.call_count == 12


def test_pane_acceptance_none_capture_reports_lost_pane():
    # None is the capture_pane failure sentinel, distinct from a successful empty capture.
    result, mock_capture, mock_title, _sleep = _observe_acceptance_with_tmux(
        [None],
        [],
        ("composer", "Please revise"),
    )

    assert result["reason"] == "pane_lost_after_submit"
    assert mock_capture.call_count == 1
    mock_title.assert_not_called()


def test_pane_acceptance_torn_composer_times_out():
    result, _capture, _title, _sleep = _observe_acceptance_with_tmux(
        [_TORN_COMPOSER_CAPTURE] * 12,
        ["✳ Ready"] * 12,
        ("composer", "Please revise"),
    )

    assert result["reason"] == "acceptance_timeout"


def test_pane_acceptance_dead_agent_frame_times_out():
    assert hopper_server.pane_surface_readable(_DEAD_AGENT_STAGED_CAPTURE) is True
    assert hopper_server.read_pane_input(_DEAD_AGENT_STAGED_CAPTURE) == "Please revise"

    result, _capture, _title, _sleep = _observe_acceptance_with_tmux(
        [_DEAD_AGENT_STAGED_CAPTURE] * 12,
        ["✳ Ready"] * 12,
        ("composer", "Please revise"),
    )

    assert result["reason"] == "acceptance_timeout"


def test_pane_acceptance_requires_two_consecutive_consumed_polls():
    result, _capture, _title, _sleep = _observe_acceptance_with_tmux(
        [_IDLE_EMPTY_CAPTURE, _IDLE_STAGED_CAPTURE] * 6,
        ["✳ Ready"] * 12,
        ("composer", "Please revise"),
    )

    assert result["reason"] == "acceptance_timeout"


def test_pane_acceptance_first_consumed_sighting_on_last_poll_times_out():
    result, _capture, _title, _sleep = _observe_acceptance_with_tmux(
        [_IDLE_STAGED_CAPTURE] * 11 + [_IDLE_EMPTY_CAPTURE],
        ["✳ Ready"] * 12,
        ("composer", "Please revise"),
    )

    assert result["reason"] == "acceptance_timeout"


def test_pane_acceptance_unchanged_composer_times_out():
    result, _capture, _title, _sleep = _observe_acceptance_with_tmux(
        [_IDLE_STAGED_CAPTURE] * 12,
        ["✳ Ready"] * 12,
        ("composer", "Please revise"),
    )

    assert result["reason"] == "acceptance_timeout"


def test_pane_acceptance_unchanged_selector_times_out():
    selector_identity = hopper_server.pane_answer_identity(_IDLE_QUESTION_CAPTURE)
    assert selector_identity is not None
    result, _capture, _title, _sleep = _observe_acceptance_with_tmux(
        [_IDLE_QUESTION_CAPTURE] * 12,
        ["✳ Ready"] * 12,
        ("selector", selector_identity),
    )

    assert result["reason"] == "acceptance_timeout"


def test_gate_feedback_empty_settle_is_unverified(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, _paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE] * 6,
        titles=["✳ Ready"] * 5,
    )

    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    response = _decode_mock_response(conn)
    assert response["outcome"] == "unverified"
    assert "do not paste the feedback again" in response["error"]


def test_gate_feedback_enter_failure_keeps_staged_text(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, _paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE, _IDLE_EMPTY_CAPTURE, _IDLE_STAGED_CAPTURE],
        titles=["✳ Ready", "✳ Ready"],
        submit=False,
    )

    mock_send.assert_called_once_with("%1", "Enter")
    response = _decode_mock_response(conn)
    assert response["outcome"] == "not_sent"
    assert "submit it once instead of pasting it again" in response["error"]


def test_gate_feedback_capture_change_with_same_staged_input_is_unverified(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)
    # Constructed from the staged fixture by appending a visible pane-change marker.
    changed_capture = _IDLE_STAGED_CAPTURE + "pane changed\n"

    _capture, _title, _paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE, _IDLE_EMPTY_CAPTURE, _IDLE_STAGED_CAPTURE]
        + [changed_capture] * 12,
        titles=["✳ Ready"] * 14,
    )

    mock_send.assert_called_once_with("%1", "Enter")
    assert srv.lodes[0]["state"] == "gated"
    assert _decode_mock_response(conn)["outcome"] == "unverified"


def test_gate_feedback_pane_lost_after_enter_is_unverified(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, _paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE, _IDLE_EMPTY_CAPTURE, _IDLE_STAGED_CAPTURE, None],
        titles=["✳ Ready", "✳ Ready"],
    )

    mock_send.assert_called_once()
    response = _decode_mock_response(conn)
    assert response["outcome"] == "unverified"
    assert "acceptance could be verified" in response["error"]


def test_delivery_taxonomy_tables_cover_shared_and_choice_only_failures():
    shared = {
        "pane_unavailable",
        "idle_timeout",
        "pane_state_unknown",
        "pane_frozen",
        "pane_awaiting_choice",
        "paste_failed",
        "paste_failed_unknown",
        "paste_not_staged",
        "pane_lost_after_paste",
        "submit_failed",
        "acceptance_timeout",
        "pane_lost_after_submit",
    }
    choice_only = {
        "pane_not_awaiting_choice",
        "choice_unavailable",
        "choice_requires_text",
        "choice_navigation_failed_unknown",
        "choice_navigation_unverified",
        "pane_lost_after_choice_navigation",
        "choice_submit_failed",
    }

    character_only = {
        "gated_body_refused",
        "character_failed",
        "character_failed_unknown",
    }
    provider_only = {"pane_blocked", "pane_character_unsupported"}
    assert set(hopper_server._DELIVERY_FAILURE_OUTCOMES) == (
        shared | choice_only | character_only | provider_only
    )
    assert set(hopper_server._GATE_FEEDBACK_MESSAGES) == shared | character_only | provider_only
    assert set(hopper_server._PANE_INPUT_MESSAGES) == shared | choice_only | provider_only
    assert hopper_server._ACCEPTED_DELIVERY_REASONS == {
        "auto_submitted",
        "character_sent",
        "composer_cleared",
        "enter_accepted",
        "selector_changed",
    }
    assert hopper_server._PRE_PASTE_REASONS == {
        "pane_unavailable",
        "idle_timeout",
        "pane_state_unknown",
        "pane_frozen",
        "pane_awaiting_choice",
        "gated_body_refused",
    }


def test_preexisting_gate_feedback_failure_contracts_stay_unchanged():
    # Pre-existing entries must not drift. A NEW outcome is allowed to appear: this
    # guard exists to catch silent rewording of contracts callers already depend on,
    # not to freeze the taxonomy against growth. The message half below is checked
    # the same way, by rendering only the reasons named here.
    preexisting_statuses = {
        "pane_unavailable": "Feedback blocked: pane unavailable",
        "busy": "Feedback blocked: pane busy",
        "not_sent": "Feedback not sent; gate remains blocked",
        "unverified": "Feedback outcome unknown; inspect pane",
        "pane_state_unknown": "Feedback blocked: pane state unrecognized",
        "pane_frozen": "Feedback blocked: pane appears frozen",
    }
    assert preexisting_statuses.items() <= hopper_server._GATE_FEEDBACK_STATUSES.items()
    expected = {
        "pane_unavailable": (
            "Feedback was not sent because pane %1 is unavailable. No feedback was pasted "
            "or submitted. Run `hop lode resume test-id`, wait for the prompt, then retry "
            "the same feedback."
        ),
        "idle_timeout": (
            "Feedback was not sent because pane %1 did not become idle within 3.0s. No "
            "feedback was pasted or submitted, and Hopper does not know when the pane will "
            "be ready. Wait for the current turn to finish, then retry the same feedback."
        ),
        "paste_failed": (
            "Feedback was not sent because Hopper could not paste it into pane %1. Nothing "
            "was submitted. Retry the same feedback."
        ),
        "paste_failed_unknown": (
            "Hopper could not complete the paste into pane %1, but some feedback text may "
            "have reached the pane. The delivery outcome is unknown. Inspect with `hop lode "
            "peek test-id` before deciding whether to retry; do not paste the feedback again "
            "unless the pane proves it was not accepted or staged."
        ),
        "paste_not_staged": (
            "Hopper pasted feedback into pane %1, but no new user turn was observed within "
            "1.0s. The delivery outcome is unknown. Inspect with `hop lode peek test-id` "
            "before deciding whether to retry; do not paste the feedback again unless the "
            "pane proves it was not accepted or staged."
        ),
        "pane_lost_after_paste": (
            "Hopper pasted feedback into pane %1, but the pane became unavailable before a "
            "new user turn was observed. The delivery outcome is unknown. Run `hop lode "
            "resume test-id`, then inspect with `hop lode peek test-id` before deciding "
            "whether to retry; do not paste the feedback again unless the pane proves it was "
            "not accepted or staged."
        ),
        "submit_failed": (
            "Feedback was not submitted because Hopper could not press Enter in pane %1. "
            "The feedback is still staged. Inspect with `hop lode peek test-id`, then submit "
            "it once instead of pasting it again."
        ),
        "acceptance_timeout": (
            "Hopper pressed Enter in pane %1, but acceptance could not be verified within "
            "3.0s. The delivery outcome is unknown. "
            "Inspect with `hop lode peek test-id` before deciding whether to retry; do not "
            "paste the feedback again unless the pane proves it was not accepted or staged."
        ),
        "pane_lost_after_submit": (
            "Hopper pressed Enter in pane %1, then the pane became unavailable before "
            "acceptance could be verified. The delivery outcome is unknown. Run `hop lode "
            "resume test-id`, then inspect with `hop lode peek test-id` before deciding "
            "whether to retry; do not paste the feedback again unless the pane proves it was "
            "not accepted or staged."
        ),
    }
    rendered = {
        reason: hopper_server._GATE_FEEDBACK_MESSAGES[reason].format(
            pane="%1",
            lode_id="test-id",
            title='"unused"',
        )
        for reason in expected
    }
    assert rendered == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("y", "y"),
        ("y\n", "y"),
        ("y\r\n", "y"),
        ("1", "1"),
        ("-", "-"),
        (" yes", None),
        ("y ", None),
        ("yes", None),
        ("\n", None),
        ("", None),
        (None, None),
        (1, None),
    ],
)
def test_single_character_payload_classifies_stdin_and_bodies(text, expected):
    assert hopper_server._single_character_payload(text) == expected


def test_gated_feedback_refuses_a_body_without_touching_the_pane(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="gated", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, mock_title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE] * 13,
        titles=["⠐ Working"] * 12,
        text="approved, ship it",
    )

    mock_title.assert_not_called()
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    mock_sleep.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == (
        "Feedback blocked: gated lode accepts only a single character"
    )
    assert srv.lodes[0]["gate_epoch"] == 1
    assert srv.lodes[0]["gate_body"] == "Gate"
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["outcome"] == "gated_character_only"
    assert "only sends a single character" in response["error"]
    assert "hop gate feedback test-id y" in response["error"]
    assert "Nothing was pasted or submitted" in response["error"]
    assert "tail" not in response


def test_gated_feedback_sends_a_character_to_a_busy_pane_without_idle_wait(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="gated", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, mock_title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE],
        titles=["⠐ Working"],
        text="y\n",
    )

    mock_paste.assert_not_called()
    mock_send.assert_called_once_with("%1", "y", literal=True)
    mock_sleep.assert_not_called()
    assert mock_title.call_count == 1
    assert srv.lodes[0]["state"] == "running"
    assert srv.lodes[0]["status"] == "Character sent"
    assert srv.lodes[0]["gate_epoch"] == 2
    assert srv.lodes[0]["gate_body"] is None
    assert srv.lodes[0]["gate_kind"] is None
    assert srv.lodes[0]["gate_delivery_epoch"] == 1
    response = _decode_mock_response(conn)
    assert response["type"] == "feedback_sent"
    assert response["character"] is True
    assert response["tmux_pane"] == "%1"


def test_idle_park_resume_clears_only_the_current_durable_gate(socket_path, make_lode):
    srv = Server(socket_path)
    lode = make_lode(id="test-id", state="gated")
    published, changed = publish_lode_gate(
        [lode],
        "test-id",
        body="Parked (idle): awaiting an operator turn",
        kind="idle_park",
        status="Parked (idle)",
    )
    assert published is lode
    assert changed
    srv.lodes = [lode]
    conn = _mock_client(srv)

    with patch.object(srv, "_gated_spawn", return_value=(SpawnOutcome.SPAWNED, "%1")) as spawn:
        srv._handle_mutation({"type": "lode_resume", "lode_id": "test-id"}, conn)

    assert lode["gate_body"] is None
    assert lode["gate_kind"] is None
    assert lode["state"] == "ready"
    spawn.assert_called_once()
    assert _decode_mock_response(conn)["type"] == "lode_resumed"


def test_gated_feedback_character_send_failure_stays_gated(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="gated", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE, _PROCESSING_CAPTURE],
        titles=["⠐ Working"],
        text="y",
        submit=False,
    )

    mock_paste.assert_not_called()
    mock_send.assert_called_once_with("%1", "y", literal=True)
    mock_sleep.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback not sent; gate remains blocked"
    response = _decode_mock_response(conn)
    assert response["outcome"] == "not_sent"
    assert "could not deliver it" in response["error"]
    assert "Retry the same single-character send" in response["error"]


def test_gated_feedback_unknown_pane_refuses_a_character_without_sending(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="gated", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE],
        titles=["_ Land native skills port to main"],
        text="y",
    )

    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    mock_sleep.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["gate_epoch"] == 2
    assert srv.lodes[0]["gate_body"] == "Gate"
    assert _decode_mock_response(conn)["outcome"] == "pane_state_unknown"


def test_gated_feedback_idle_character_submits_without_paste(socket_path, make_lode):
    staged = "─────\n❯ y\n─────\n"
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="gated", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE, staged, _PROCESSING_CAPTURE],
        titles=["✳ Ready", "✳ Ready", "⠐ Working"],
        text="y",
    )

    mock_paste.assert_not_called()
    mock_send.assert_any_call("%1", "y", literal=True)
    mock_send.assert_any_call("%1", "Enter")
    assert srv.lodes[0]["state"] == "running"
    response = _decode_mock_response(conn)
    assert response["type"] == "feedback_sent"
    assert "character" not in response


def test_ungated_feedback_still_pastes_a_body(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[
            _IDLE_EMPTY_CAPTURE,
            _IDLE_EMPTY_CAPTURE,
            _IDLE_STAGED_CAPTURE,
            _PROCESSING_CAPTURE,
        ],
        titles=["✳ Ready", "✳ Ready", "⠐ Working"],
        text="Looks good",
    )

    mock_paste.assert_called_once_with("%1", "Looks good")
    mock_send.assert_called_once_with("%1", "Enter")
    assert _decode_mock_response(conn)["type"] == "feedback_sent"


def test_gate_feedback_unknown_pane_state_never_touches_pane(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    _capture, mock_title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE] * 13,
        titles=["_ Land native skills port to main"] * 12,
    )

    assert mock_title.call_count == 12
    assert mock_sleep.call_count == 12
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert srv.lodes[0]["state"] == "gated"
    assert srv.lodes[0]["status"] == "Feedback blocked: pane state unrecognized"
    assert srv.lodes[0]["gate_epoch"] == 1
    assert srv.lodes[0]["gate_body"] == srv.lodes[0]["status"]
    response = _decode_mock_response(conn)
    assert response["outcome"] == "pane_state_unknown"
    assert '"_ Land native skills port to main"' in response["error"]
    assert "hop lode peek test-id" in response["error"]
    assert "safe to retry" in response["error"]
    assert "Wait for the current turn to finish" not in response["error"]


def test_gate_feedback_missing_title_reports_no_title_without_touching_pane(
    socket_path, make_lode, caplog
):
    """A None title remains UNKNOWN and is rendered distinctly in messages and logs."""
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)

    with caplog.at_level(logging.WARNING, logger="hopper.server"):
        _capture, mock_title, mock_paste, mock_send, mock_sleep = _handle_delivery_with_tmux(
            srv,
            conn,
            captures=[_PROCESSING_CAPTURE] * 13,
            titles=[None] * 12,
        )

    assert mock_title.call_count == 12
    assert mock_sleep.call_count == 12
    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert srv.lodes[0]["gate_epoch"] == 1
    assert srv.lodes[0]["gate_body"] == srv.lodes[0]["status"]
    response = _decode_mock_response(conn)
    assert response["outcome"] == "pane_state_unknown"
    assert "<no title reported>" in response["error"]
    assert "title=<no title reported>" in caplog.text
    assert 'title=""' not in caplog.text


def test_pane_delivery_unknown_then_idle_proceeds():
    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[
                _PROCESSING_CAPTURE,
                _PROCESSING_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _PROCESSING_CAPTURE,
            ],
        ),
        patch(
            "hopper.server.pane_title",
            side_effect=["_ Land native skills port to main", "✳ Ready", "⠐ Working"],
        ),
        patch("hopper.server.paste_buffer", return_value=True) as mock_paste,
        patch("hopper.server.send_keys") as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "continue", paste=True)

    assert result == {
        "reason": "auto_submitted",
        "capture": _PROCESSING_CAPTURE,
        "title": "⠐ Working",
    }
    mock_paste.assert_called_once_with("%1", "continue")
    mock_send.assert_not_called()


def test_malformed_terminal_frame_is_neither_input_eligible_nor_acceptance_evidence():
    malformed = "\x1b]0;unterminated─────\n❯\u00a0staged\n─────\n"
    with (
        patch("hopper.server.capture_pane", return_value=malformed),
        patch("hopper.server.pane_title", return_value="✳ Ready"),
        patch("hopper.server.paste_buffer") as paste,
        patch("hopper.server.time.sleep"),
    ):
        delivery = hopper_server._attempt_pane_delivery("%1", "continue", paste=True)

    assert delivery["reason"] == "pane_state_unknown"
    paste.assert_not_called()

    with (
        patch("hopper.server.capture_pane", return_value=malformed),
        patch("hopper.server.pane_title", return_value="not a known title"),
        patch("hopper.server.time.sleep"),
    ):
        observed = hopper_server._observe_pane_acceptance(
            "%1", malformed, "not a known title", ("composer", "staged")
        )

    assert observed["reason"] == "acceptance_timeout"


def test_lode_send_pane_input_answer_auto_submits_without_state_write(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="running",
            status="Waiting for choice",
            gate_epoch=7,
            tmux_pane="%1",
        )
    ]
    before = copy.deepcopy(srv.lodes[0])
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[
            _IDLE_QUESTION_CAPTURE,
            _IDLE_QUESTION_CAPTURE,
            _IDLE_QUESTION_CAPTURE,
            _PROCESSING_CAPTURE,
        ],
        titles=["✳ Ask user to choose", "⠂ Ask user to choose"],
        text="1",
        message_type="lode_send_pane_input",
        input_paste=False,
    )

    mock_paste.assert_not_called()
    mock_send.assert_called_once_with("%1", "Enter")
    assert srv.lodes[0] == before
    assert srv.broadcast_queue.empty()
    response = _decode_mock_response(conn)
    assert {key: response[key] for key in ("type", "lode_id", "tmux_pane")} == {
        "type": "pane_input_sent",
        "lode_id": "test-id",
        "tmux_pane": "%1",
    }


def test_lode_send_pane_input_nudge_uses_paste_without_state_write(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", status="Idle", tmux_pane="%1")]
    before = copy.deepcopy(srv.lodes[0])
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[
            _IDLE_EMPTY_CAPTURE,
            _IDLE_EMPTY_CAPTURE,
            _IDLE_STAGED_CAPTURE,
            _PROCESSING_CAPTURE,
        ],
        titles=["✳ Ready", "✳ Ready", "⠐ Working"],
        text="continue",
        message_type="lode_send_pane_input",
        input_paste=True,
    )

    mock_paste.assert_called_once_with("%1", "continue")
    mock_send.assert_called_once_with("%1", "Enter")
    assert srv.lodes[0] == before
    assert srv.broadcast_queue.empty()
    assert _decode_mock_response(conn)["type"] == "pane_input_sent"


def test_lode_send_pane_input_answer_without_processing_is_unverified(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    before = copy.deepcopy(srv.lodes[0])
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_QUESTION_CAPTURE] * 15,
        titles=["✳ Ask user to choose"] * 13,
        text="1",
        message_type="lode_send_pane_input",
        input_paste=False,
    )

    mock_paste.assert_not_called()
    mock_send.assert_called_once_with("%1", "Enter")
    assert srv.lodes[0] == before
    assert srv.broadcast_queue.empty()
    response = _decode_mock_response(conn)
    assert response["type"] == "error"
    assert response["outcome"] == "unverified"
    assert "acceptance could not be verified" in response["error"]


def test_lode_send_pane_input_unknown_state_does_not_mutate_or_broadcast(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="running",
            status="Working",
            gate_epoch=3,
            tmux_pane="%1",
        )
    ]
    before = copy.deepcopy(srv.lodes[0])
    conn = _mock_client(srv)

    _capture, _title, mock_paste, mock_send, _sleep = _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_PROCESSING_CAPTURE] * 13,
        titles=["_ Land native skills port to main"] * 12,
        text="continue",
        message_type="lode_send_pane_input",
    )

    mock_paste.assert_not_called()
    mock_send.assert_not_called()
    assert srv.lodes[0] == before
    assert srv.broadcast_queue.empty()
    response = _decode_mock_response(conn)
    assert response["outcome"] == "pane_state_unknown"
    assert '"_ Land native skills port to main"' in response["error"]


@pytest.mark.parametrize(
    ("reason", "outcome", "level"),
    [
        ("auto_submitted", "accepted", logging.INFO),
        ("composer_cleared", "accepted", logging.INFO),
        ("enter_accepted", "accepted", logging.INFO),
        ("selector_changed", "accepted", logging.INFO),
        ("pane_unavailable", "pane_unavailable", logging.WARNING),
        ("idle_timeout", "busy", logging.WARNING),
        ("pane_state_unknown", "pane_state_unknown", logging.WARNING),
        ("pane_frozen", "pane_frozen", logging.WARNING),
        ("pane_awaiting_choice", "awaiting_choice", logging.WARNING),
        ("paste_failed", "not_sent", logging.WARNING),
        ("paste_failed_unknown", "unverified", logging.WARNING),
        ("paste_not_staged", "unverified", logging.WARNING),
        ("pane_lost_after_paste", "unverified", logging.WARNING),
        ("submit_failed", "not_sent", logging.WARNING),
        ("acceptance_timeout", "unverified", logging.WARNING),
        ("pane_lost_after_submit", "unverified", logging.WARNING),
    ],
)
def test_pane_delivery_logs_exactly_once_for_every_reason(reason, outcome, level, caplog):
    attempt_result = {
        "reason": reason,
        "capture": _IDLE_EMPTY_CAPTURE,
        "title": "_ Land native skills port to main",
    }
    delivered_text = "DISTINCTIVE_BODY_MUST_NOT_BE_LOGGED"

    with (
        patch("hopper.server._attempt_pane_delivery", return_value=attempt_result),
        caplog.at_level(logging.INFO, logger="hopper.server"),
    ):
        result = hopper_server._deliver_pane_input("test-id", "%1", delivered_text, paste=True)

    records = [record for record in caplog.records if record.name == "hopper.server"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == level
    assert result == attempt_result
    message = record.getMessage()
    assert "lode=test-id" in message
    assert "pane=%1" in message
    assert f"reason={reason}" in message
    assert f"outcome={outcome}" in message
    assert 'title="_ Land native skills port to main"' in message
    assert delivered_text not in message


def test_pane_delivery_exception_logs_once_without_body_and_propagates(caplog):
    delivered_text = "DISTINCTIVE_EXCEPTION_BODY_MUST_NOT_BE_LOGGED"

    with (
        patch("hopper.server.capture_pane", side_effect=OSError("tmux failed")),
        caplog.at_level(logging.WARNING, logger="hopper.server"),
        pytest.raises(OSError, match="tmux failed"),
    ):
        hopper_server._deliver_pane_input("test-id", "%1", delivered_text, paste=True)

    records = [record for record in caplog.records if record.name == "hopper.server"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "lode=test-id" in message
    assert "pane=%1" in message
    assert "reason=delivery_exception" in message
    assert "outcome=unverified" in message
    assert "title=<no title reported>" in message
    assert delivered_text not in message


def test_render_observed_title_distinguishes_none_and_preserves_verbatim():
    assert hopper_server._render_observed_title(None) == "<no title reported>"
    assert (
        hopper_server._render_observed_title("_ Land native skills port to main")
        == '"_ Land native skills port to main"'
    )


@pytest.mark.parametrize(
    "reason",
    ["auto_submitted", "composer_cleared", "enter_accepted", "selector_changed"],
)
def test_send_pane_input_round_trips_over_real_server_socket(
    server, socket_path, make_lode, reason
):
    server.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    attempt_result = {
        "reason": reason,
        "capture": _PROCESSING_CAPTURE,
        "title": "⠂ Working",
    }

    with patch("hopper.server._attempt_pane_delivery", return_value=attempt_result):
        response = send_pane_input(
            socket_path,
            "test-id",
            "continue",
            paste=True,
        )

    assert response is not None
    assert {key: response[key] for key in ("type", "lode_id", "tmux_pane")} == {
        "type": "pane_input_sent",
        "lode_id": "test-id",
        "tmux_pane": "%1",
    }


@pytest.mark.parametrize("reason", ["composer_cleared", "selector_changed"])
def test_gate_feedback_handler_accepts_consumed_input_reasons(socket_path, make_lode, reason):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)
    attempt_result = {
        "reason": reason,
        "capture": _IDLE_EMPTY_CAPTURE,
        "title": "✳ Ready",
    }

    with patch("hopper.server._attempt_pane_delivery", return_value=attempt_result):
        srv._handle_mutation(
            {"type": "lode_send_feedback", "lode_id": "test-id", "text": "Please revise"},
            conn,
        )

    assert srv.lodes[0]["state"] == "running"
    assert _decode_mock_response(conn)["type"] == "feedback_sent"


def test_feedback_epoch_rejects_stale_resume(socket_path, make_lode, caplog):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running", tmux_pane="%1")]
    conn = _mock_client(srv)
    _handle_delivery_with_tmux(
        srv,
        conn,
        captures=[_IDLE_EMPTY_CAPTURE] * 3,
        titles=["✳ Ready"],
        paste=False,
    )

    with caplog.at_level(logging.INFO):
        srv._handle_mutation(
            _runner_message(
                srv,
                "lode_set_state",
                "test-id",
                state="running",
                status="Gate resumed",
                gate_epoch=0,
            ),
            None,
        )

    assert srv.lodes[0]["state"] == "gated"
    assert "Dropping stale state update lode=test-id" in caplog.text


def test_matching_idle_gate_epoch_allows_genuine_resume(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="gated",
            gate_body="Parked",
            gate_kind="idle_park",
            gate_epoch=4,
        )
    ]

    srv._handle_mutation(
        _runner_message(
            srv,
            "lode_set_state",
            "test-id",
            state="running",
            status="Gate resumed",
            gate_epoch=4,
            gate_kind="idle_park",
        ),
        None,
    )

    assert srv.lodes[0]["state"] == "running"


def test_gate_clear_without_epoch_is_refused_and_leaves_gate_intact(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [
        make_lode(
            id="test-id",
            state="gated",
            gate_body="Parked",
            gate_kind="idle_park",
            gate_epoch=4,
        )
    ]
    conn = _mock_client(srv)

    srv._handle_mutation(
        _runner_message(
            srv,
            "lode_set_state",
            "test-id",
            state="running",
            status="Gate resumed",
            gate_kind="idle_park",
            ack_requested=True,
        ),
        conn,
    )

    assert srv.lodes[0]["state"] == "gated"
    assert lode_gate(srv.lodes[0]) == {
        "body": "Parked",
        "kind": "idle_park",
        "epoch": 4,
        "delivery_epoch": 0,
    }
    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == "stale_gate_epoch"


@pytest.mark.parametrize("kind", ["native_question", "idle_park"])
def test_runner_gate_publication_requires_current_generation(socket_path, make_lode, kind):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", state="running")]
    conn = _mock_client(srv)

    srv._handle_mutation(
        {
            "type": "lode_publish_gate",
            "lode_id": "test-id",
            "body": "Awaiting a response",
            "kind": kind,
            "ack_requested": True,
            "ts": 1,
        },
        conn,
    )

    assert srv.lodes[0]["state"] == "running"
    assert lode_gate(srv.lodes[0]) is None
    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == "missing_run_generation"


def test_durable_gate_publication_round_trips_over_the_real_socket(server, socket_path, make_lode):
    server.lodes = [make_lode(id="test-id", stage="refine", state="running", tmux_pane="%1")]

    response = hopper_client.publish_lode_gate(
        socket_path, "test-id", "Review this change", kind="explicit"
    )

    assert response is not None
    assert response["type"] == "lode_gate_published"
    lode = server.lodes[0]
    assert lode["state"] == "gated"
    assert lode["gate_body"] == "Review this change"
    assert lode["gate_kind"] == "explicit"
    assert lode["gate_epoch"] == 1


def test_successful_durable_gate_publication_writes_derived_artifact(
    isolate_config, server, socket_path, make_lode
):
    server.lodes = [make_lode(id="test-id", stage="refine", state="running", tmux_pane="%1")]

    response = hopper_client.publish_lode_gate(
        socket_path, "test-id", "Review this change", kind="explicit"
    )

    assert response is not None
    assert response["artifact_written"] is True
    assert (isolate_config / "lodes" / "test-id" / "gate.md").read_text() == "Review this change"


def test_gate_artifact_failure_leaves_durable_gate_as_the_only_authority(socket_path, make_lode):
    srv = Server(socket_path)
    srv.lodes = [make_lode(id="test-id", stage="refine", state="running")]
    conn = _mock_client(srv)

    with patch("hopper.server.write_lode_gate_artifact", side_effect=OSError("disk unavailable")):
        srv._handle_mutation(
            {
                "type": "lode_publish_gate",
                "lode_id": "test-id",
                "body": "Durable review",
                "kind": "explicit",
            },
            conn,
        )

    assert srv.lodes[0]["gate_body"] == "Durable review"
    assert not (config.hopper_dir() / "lodes" / "test-id" / "gate.md").exists()
    assert _decode_mock_response(conn)["artifact_written"] is False


def test_failed_gate_replacement_preserves_one_prior_authority(socket_path, make_lode):
    srv = Server(socket_path)
    lode = make_lode(
        id="test-id",
        state="gated",
        gate_body="Prior gate",
        gate_kind="explicit",
        gate_epoch=4,
    )
    srv.lodes = [lode]
    save_lodes(srv.lodes)
    active_path = config.hopper_dir() / "active.jsonl"
    prior_bytes = active_path.read_bytes()
    publication_conn = _mock_client(srv)

    with patch("hopper.lodes._write_jsonl_atomic", side_effect=OSError("disk unavailable")):
        srv._handle_mutation(
            {
                "type": "lode_publish_gate",
                "lode_id": "test-id",
                "body": "Replacement gate",
                "kind": "explicit",
            },
            publication_conn,
        )

    assert lode_gate(lode) == {
        "body": "Prior gate",
        "kind": "explicit",
        "epoch": 4,
        "delivery_epoch": 0,
    }
    assert lode["state"] == "gated"
    assert _decode_mock_response(publication_conn)["type"] == "error"
    assert active_path.read_bytes() == prior_bytes

    feedback_conn = _mock_client(srv)
    srv._handle_mutation(
        {"type": "lode_send_feedback", "lode_id": "test-id", "text": "mixed authority"},
        feedback_conn,
    )

    assert _decode_mock_response(feedback_conn)["type"] == "error"
    assert lode_gate(lode) == {
        "body": "Prior gate",
        "kind": "explicit",
        "epoch": 4,
        "delivery_epoch": 0,
    }


def test_stale_native_consumption_cannot_clear_a_replaced_gate(socket_path, make_lode):
    srv = Server(socket_path)
    lode = make_lode(
        id="test-id",
        state="gated",
        run_generation="1" * 32,
        gate_body="First question",
        gate_kind="native_question",
        gate_epoch=1,
    )
    srv.lodes = [lode]
    publish_lode_gate(
        srv.lodes,
        "test-id",
        body="Second question",
        kind="native_question",
        status="Gate",
    )

    srv._handle_mutation(
        _runner_message(
            srv,
            "lode_set_state",
            "test-id",
            state="running",
            status="Gate resumed",
            gate_epoch=1,
            gate_kind="native_question",
        ),
        None,
    )

    assert lode["state"] == "gated"
    assert lode["gate_body"] == "Second question"
    assert lode["gate_epoch"] == 2


class TestActivityLog:
    def test_activity_log_created_on_start(self, isolate_config, server):
        """Server start creates activity.log with listening message."""
        log_path = isolate_config / "activity.log"
        assert log_path.exists()
        deadline = time.monotonic() + 2
        content = ""
        while "Server listening" not in content and time.monotonic() < deadline:
            content = log_path.read_text()
            time.sleep(0.01)
        assert "Server listening" in content

    def test_lode_mutation_logged(self, isolate_config, server, socket_path, make_lode):
        """Lode state change produces a log line with lode ID and new state."""
        server.lodes = [make_lode(id="test-log", state="running", active=True)]
        save_lodes(server.lodes)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        try:
            msg = _runner_message(
                server,
                "lode_set_state",
                "test-log",
                state="running",
                status="doing stuff",
            )
            client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            client.recv(4096)
        finally:
            client.close()

        time.sleep(0.1)
        log_path = isolate_config / "activity.log"
        content = log_path.read_text()
        assert "test-log" in content
        assert "state=running" in content

    def test_backlog_mutation_logged(self, isolate_config, server, socket_path):
        """Backlog add produces a log line with item ID."""
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        try:
            msg = {"type": "backlog_add", "project": "myproj", "description": "do thing"}
            client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            client.recv(4096)
        finally:
            client.close()

        time.sleep(0.1)
        log_path = isolate_config / "activity.log"
        content = log_path.read_text()
        assert "added project=myproj" in content

    def test_projects_reload(self, isolate_config, server, socket_path):
        """projects_reload reloads project list from disk."""
        # Server starts with empty projects
        assert server.projects == []

        # Send projects_reload message
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        try:
            msg = {"type": "projects_reload"}
            client.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        finally:
            client.close()

        time.sleep(0.1)
        # Projects reloaded (empty since no config, but handler ran)
        log_path = isolate_config / "activity.log"
        content = log_path.read_text()
        assert "Projects and lodes reloaded" in content

    def test_projects_reload_refreshes_project_order_after_touch(self, socket_path):
        """projects_reload updates in-memory project recency order after touch_project."""
        with config_transaction() as stored:
            stored.update(
                {
                    "projects": [
                        {"path": "/tmp/A", "name": "A", "disabled": False, "last_used_at": 200},
                        {"path": "/tmp/B", "name": "B", "disabled": False, "last_used_at": 100},
                    ]
                }
            )

        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        try:
            assert srv.ready.wait(5), "Server did not start"

            assert [project.name for project in srv.projects] == ["A", "B"]

            touch_project("B")
            srv.enqueue({"type": "projects_reload"})

            for _ in range(50):
                if [project.name for project in srv.projects] == ["B", "A"]:
                    break
                time.sleep(0.02)

            assert [project.name for project in srv.projects] == ["B", "A"]
        finally:
            srv.stop()
            thread.join(timeout=2)

    def test_server_stop_closes_handler(self, isolate_config, socket_path):
        """Server stop removes and closes the file handler."""
        srv = Server(socket_path)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()

        assert srv.ready.wait(5), "Server did not start"

        assert srv._log_handler is not None
        handler = srv._log_handler
        stream = handler.stream

        srv.stop()
        thread.join(timeout=2)

        assert srv._log_handler is None
        assert stream is None or stream.closed


class TestOomLifecycle:
    def test_spawn_persists_generation_and_scope_before_pane_creation(self, socket_path, make_lode):
        srv = Server(socket_path)
        lode = make_lode(id="test-id")
        srv.lodes = [lode]
        generation = "1" * 32
        expected_unit = hopper_server.oom.scope_unit_name("test-id", generation)
        persisted = []

        def observe_save(lodes):
            persisted.append((lodes[0]["run_generation"], lodes[0]["oom_scope"]))

        def observe_spawn(lode_id, project_path, *, foreground, env):
            assert persisted == [(generation, expected_unit)]
            assert env == {
                "HOPPER_RUN_GENERATION": generation,
                "HOPPER_OOM_SCOPE": expected_unit,
            }
            return _spawned("%1")

        with (
            patch("hopper.server.uuid.uuid4", return_value=MagicMock(hex=generation)),
            patch("hopper.server.oom.is_linux", return_value=True),
            patch("hopper.server.save_lodes", side_effect=observe_save),
            patch("hopper.server.spawn_lode_processor", side_effect=observe_spawn),
            patch.object(srv, "broadcast"),
        ):
            outcome, pane = srv._gated_spawn(lode, "/repo")

        assert outcome is SpawnOutcome.SPAWNED
        assert pane == "%1"
        assert persisted[-1] == (generation, expected_unit)

    @pytest.mark.parametrize(
        ("msg_type", "fields"),
        [
            ("lode_set_state", {"state": "running", "status": "stale"}),
            ("lode_set_status", {"status": "stale"}),
            ("lode_set_stage", {"stage": "ship"}),
            ("lode_set_progress", {"summary": "stale"}),
            ("lode_set_title", {"title": "stale"}),
            ("lode_set_branch", {"branch": "stale"}),
            (
                "lode_set_coder_session",
                {"provider": "codex", "session_id": "stale"},
            ),
            ("lode_set_claude_started", {"claude_stage": "mill"}),
        ],
    )
    def test_stale_runner_mutations_are_fenced(self, socket_path, make_lode, msg_type, fields):
        srv = Server(socket_path)
        lode = make_lode(
            id="test-id",
            state="error",
            status=format_terminal_failure_status("oom", "test-id"),
            failure_kind="oom",
            run_generation="2" * 32,
        )
        srv.lodes = [lode]
        before = json.loads(json.dumps(lode))

        with (
            patch("hopper.server.save_lodes") as save,
            patch.object(srv, "broadcast") as broadcast,
        ):
            srv._handle_mutation(
                {
                    "type": msg_type,
                    "lode_id": "test-id",
                    "run_generation": "1" * 32,
                    **fields,
                },
                None,
            )

        if msg_type == "lode_set_claude_started":
            assert lode_stage_session(lode, "mill") == lode_stage_session(before, "mill")
            assert lode["status"].startswith("protocol error: stale_run_generation")
            assert lode["protocol_error"] == "stale_run_generation"
            save.assert_called_once()
            broadcast.assert_called_once()
        else:
            assert lode == before
            save.assert_not_called()
            broadcast.assert_not_called()

    @pytest.mark.parametrize(
        ("lode", "run_generation", "reason"),
        [
            (None, "1" * 32, "lode_not_found"),
            ({"run_generation": "2" * 32, "failure_kind": None}, None, "missing_run_generation"),
            (
                {"run_generation": "2" * 32, "failure_kind": None},
                "1" * 32,
                "stale_run_generation",
            ),
            ({"run_generation": None, "failure_kind": None}, "1" * 32, "stale_run_generation"),
            (
                {"run_generation": "2" * 32, "failure_kind": "oom"},
                "2" * 32,
                "terminal_failure",
            ),
        ],
    )
    def test_acknowledged_runner_mutation_reports_each_fence_refusal(
        self, socket_path, make_lode, lode, run_generation, reason
    ):
        srv = Server(socket_path)
        if lode is not None:
            srv.lodes = [make_lode(id="test-id", **lode)]
        conn = _mock_client(srv)
        message = {
            "type": "lode_set_state",
            "lode_id": "test-id",
            "state": "completed",
            "status": "Mill complete",
            "ack_requested": True,
        }
        if run_generation is not None:
            message["run_generation"] = run_generation

        srv._handle_mutation(message, conn)

        response = _decode_mock_response(conn)
        assert response["type"] == "mutation_ack"
        assert response["accepted"] is False
        assert response["reason"] == reason

    def test_acknowledged_completed_state_is_refused_without_mutation(self, socket_path, make_lode):
        generation = "2" * 32
        srv = Server(socket_path)
        lode = make_lode(id="test-id", state="running", active=True, run_generation=generation)
        srv.lodes = [lode]
        conn = _mock_client(srv)
        before = copy.deepcopy(lode)

        srv._handle_mutation(
            {
                "type": "lode_set_state",
                "lode_id": "test-id",
                "state": "completed",
                "status": "Mill complete",
                "run_generation": generation,
                "ack_requested": True,
                "exchange_id": "exchange",
            },
            conn,
        )

        response = _decode_mock_response(conn)
        assert response["accepted"] is False
        assert response["reason"] == "unsupported_state"
        assert response["exchange_id"] == "exchange"
        assert lode == before

    def test_old_cli_mutation_without_ack_request_gets_no_direct_reply(
        self, socket_path, make_lode
    ):
        generation = "2" * 32
        srv = Server(socket_path)
        lode = make_lode(id="test-id", run_generation=generation)
        srv.lodes = [lode]
        conn = _mock_client(srv)
        before = copy.deepcopy(lode)

        srv._handle_mutation(
            {
                "type": "lode_set_state",
                "lode_id": "test-id",
                "state": "completed",
                "status": "Mill complete",
                "run_generation": generation,
            },
            conn,
        )

        conn.sendall.assert_not_called()
        assert lode == before

    def test_never_registered_one_shot_disconnect_is_inert(self, socket_path, make_lode):
        srv = Server(socket_path)
        lode = make_lode(id="test-id", active=True, run_generation="2" * 32)
        srv.lodes = [lode]
        conn = MagicMock()

        srv._on_client_disconnect(conn)

        assert lode["active"] is True
        assert srv.pending_disconnects == {}

    def test_pane_activity_mutation_updates_only_pane_activity_field(self, socket_path, make_lode):
        generation = "2" * 32
        srv = Server(socket_path)
        lode = make_lode(
            id="test-id",
            state="running",
            active=True,
            run_generation=generation,
            last_progress_at=12_000,
        )
        srv.lodes = [lode]

        srv._handle_mutation(
            {
                "type": "lode_set_pane_activity",
                "lode_id": "test-id",
                "run_generation": generation,
                "observed_at": 45_000,
            },
            None,
        )

        assert lode["last_pane_activity_at"] == 45_000
        assert lode["last_progress_at"] == 12_000

    def test_terminal_latch_survives_spawn_until_supported_registration(
        self, socket_path, make_lode
    ):
        srv = Server(socket_path)
        status = format_terminal_failure_status("oom", "test-id")
        lode = make_lode(id="test-id", state="error", status=status, failure_kind="oom")
        srv.lodes = [lode]

        with patch("hopper.server.spawn_lode_processor") as spawn:
            assert srv._gated_spawn(lode, "/repo") == (SpawnOutcome.UNKNOWN, None)
        spawn.assert_not_called()
        assert (lode["state"], lode["status"], lode["failure_kind"]) == (
            "error",
            status,
            "oom",
        )

        with (
            patch("hopper.server.spawn_lode_processor", return_value=_spawned("%2")),
            patch.object(srv, "broadcast"),
        ):
            outcome, _ = srv._gated_spawn(
                lode,
                "/repo",
                allow_terminal_recovery=True,
            )

        assert outcome is SpawnOutcome.SPAWNED
        assert (lode["state"], lode["status"], lode["failure_kind"]) == (
            "error",
            status,
            "oom",
        )
        assert srv._register_lode_client(
            "test-id",
            MagicMock(),
            tmux_pane="%2",
            pid=222,
            run_generation=lode["run_generation"],
            proof_mode="linux-strict",
            actual_unit=lode["oom_scope"],
        )
        assert lode["failure_kind"] is None
        assert lode["state"] == "running"
        assert lode["status"] == "Starting mill"

    @pytest.mark.parametrize("proof_mode", ["linux-degraded", "other-bounded-no-birth"])
    def test_terminal_resume_commits_recovery_in_degraded_mode(
        self, socket_path, make_lode, proof_mode
    ):
        srv = Server(socket_path)
        status = format_terminal_failure_status("oom", "test-id")
        lode = make_lode(
            id="test-id",
            project="proj",
            state="error",
            status=status,
            failure_kind="oom",
        )
        srv.lodes = [lode]

        with (
            patch(
                "hopper.server.find_project",
                return_value=Project(path="/repo", name="proj"),
            ),
            patch("hopper.server.oom.is_linux", return_value=True),
            patch("hopper.server.spawn_lode_processor", return_value=_spawned("%4")),
            patch("hopper.server.save_lodes"),
            patch.object(srv, "broadcast") as broadcast,
        ):
            srv._handle_mutation({"type": "lode_resume", "lode_id": "test-id"}, None)
            assert lode["failure_kind"] == "oom"
            assert lode["status"] == status
            broadcast.reset_mock()

            owner = MagicMock()
            assert srv._register_lode_client(
                "test-id",
                owner,
                tmux_pane="%4",
                pid=444,
                run_generation=lode["run_generation"],
                proof_mode=proof_mode,
                actual_unit=None,
            )

        assert lode["failure_kind"] is None
        assert lode["state"] == "running"
        assert lode["status"] == "Starting mill"
        assert lode["oom_scope"] is None
        assert srv.lode_clients["test-id"] is owner
        assert srv.client_lodes[owner] == "test-id"
        assert srv.client_generations[owner] == lode["run_generation"]
        broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})

    @pytest.mark.parametrize(
        ("proof_mode", "actual_unit", "run_generation"),
        [
            ("linux-strict", None, "4" * 32),
            ("linux-strict", "hopper-other.scope", "4" * 32),
            ("linux-strict", "hopper-terminal.scope", "3" * 32),
            ("linux-degraded", "hopper-terminal.scope", "4" * 32),
        ],
    )
    def test_terminal_registration_refuses_invalid_proof_claim(
        self,
        socket_path,
        make_lode,
        proof_mode,
        actual_unit,
        run_generation,
    ):
        srv = Server(socket_path)
        terminal = make_lode(
            id="terminal-id",
            state="error",
            status=format_terminal_failure_status("oom", "terminal-id"),
            failure_kind="oom",
            run_generation="4" * 32,
            oom_scope="hopper-terminal.scope",
            tmux_pane="%4",
        )
        srv.lodes = [terminal]
        before = json.loads(json.dumps(terminal))
        owner = MagicMock()

        with (
            patch("hopper.server.save_lodes") as save,
            patch.object(srv, "broadcast") as broadcast,
        ):
            assert not srv._register_lode_client(
                "terminal-id",
                owner,
                tmux_pane="%4",
                pid=444,
                run_generation=run_generation,
                proof_mode=proof_mode,
                actual_unit=actual_unit,
            )

        assert terminal == before
        assert "terminal-id" not in srv.lode_clients
        assert owner not in srv.client_lodes
        assert owner not in srv.client_generations
        save.assert_not_called()
        broadcast.assert_not_called()

    def test_terminal_legacy_restart_refuses_without_mutation(self, socket_path, make_lode):
        status = format_terminal_failure_status("oom", "test-id")
        lode = make_lode(
            id="test-id",
            project="proj",
            stage="refine",
            state="error",
            status=status,
            failure_kind="oom",
        )
        before = copy.deepcopy(lode)
        server = Server(socket_path)
        server.lodes = [lode]
        conn = _mock_client(server)

        server._handle_mutation(
            {
                "type": "lode_reset_claude_stage",
                "lode_id": "test-id",
                "claude_stage": "refine",
                "spawn": True,
            },
            conn,
        )

        assert lode == before
        response = _decode_mock_response(conn)
        assert response["accepted"] is False
        assert response["reason"] == "protocol_upgrade_required"

    def test_authoritative_oom_result_is_exact_and_idempotent(self, socket_path, make_lode):
        srv = Server(socket_path)
        lode = make_lode(
            id="test-id",
            state="running",
            active=True,
            tmux_pane="%1",
            pid=123,
            run_generation="5" * 32,
            oom_scope="hopper-test.scope",
        )
        srv.lodes = [lode]
        message = {
            "type": "lode_run_result",
            "lode_id": "test-id",
            "run_generation": "5" * 32,
            "unit_name": "hopper-test.scope",
            "unit_result": "oom-kill",
            "worker_returncode": 137,
        }

        with (
            patch("hopper.server.save_lodes") as save,
            patch.object(srv, "broadcast") as broadcast,
        ):
            srv._handle_lode_run_result(message, None)
            srv._handle_lode_run_result(message, None)

        assert lode["state"] == "error"
        assert lode["failure_kind"] == "oom"
        assert lode["status"] == format_terminal_failure_status("oom", "test-id")
        assert save.call_count == 2
        save.assert_called_with(srv.lodes)
        broadcast.assert_called_once_with({"type": "lode_updated", "lode": lode})

    def test_failed_terminal_persistence_is_retried_before_ack(self, socket_path, make_lode):
        srv = Server(socket_path)
        lode = make_lode(
            id="test-id",
            state="running",
            run_generation="a" * 32,
            oom_scope="hopper-test.scope",
        )
        srv.lodes = [lode]
        message = {
            "lode_id": "test-id",
            "run_generation": "a" * 32,
            "unit_name": "hopper-test.scope",
            "unit_result": "oom-kill",
            "worker_returncode": 137,
        }

        with patch("hopper.server.save_lodes", side_effect=[OSError("disk full"), None]) as save:
            with pytest.raises(OSError, match="disk full"):
                srv._handle_lode_run_result(message, None)
            srv._handle_lode_run_result(message, None)

        assert save.call_count == 2
        assert lode["failure_kind"] == "oom"

    @pytest.mark.parametrize("unit_result", [None, "success"])
    def test_connected_ordinary_result_ack_is_not_durable(
        self, socket_path, make_lode, unit_result
    ):
        srv = Server(socket_path)
        generation = "d" * 32
        lode = make_lode(
            id="test-id",
            run_generation=generation,
            oom_scope="hopper-test.scope",
        )
        srv.lodes = [lode]
        srv.lode_clients["test-id"] = MagicMock()
        conn = _mock_client(srv)

        srv._handle_lode_run_result(
            {
                "lode_id": "test-id",
                "run_generation": generation,
                "unit_name": "hopper-test.scope",
                "unit_result": unit_result,
                "worker_returncode": 0,
            },
            conn,
        )

        assert _decode_mock_response(conn) == {
            "type": "lode_run_result_ack",
            "ts": ANY,
            "accepted": True,
            "durable": False,
            "disposition": "success",
        }
        assert srv.runner_results[("test-id", generation)] == (unit_result, 0)

    def test_unknown_lode_result_ack_is_durable(self, socket_path):
        srv = Server(socket_path)
        conn = _mock_client(srv)

        srv._handle_lode_run_result(
            {
                "lode_id": "archived-id",
                "run_generation": "e" * 32,
                "unit_name": "hopper-archived.scope",
                "unit_result": "exit-code",
                "worker_returncode": 1,
            },
            conn,
        )

        assert _decode_mock_response(conn) == {
            "type": "lode_run_result_ack",
            "ts": ANY,
            "accepted": False,
            "durable": True,
            "disposition": "not-found",
        }

    @pytest.mark.parametrize("unit_result", [None, "success"], ids=["collected", "loaded"])
    @pytest.mark.parametrize(
        ("stage", "status"),
        [
            ("ship", "Refine complete"),
            ("shipped", "Ship complete"),
        ],
        ids=["ship", "shipped"],
    )
    def test_verified_ordinary_exit_orderings_finalize_identically(
        self,
        socket_path,
        make_lode,
        unit_result,
        stage,
        status,
    ):
        def run_order(report_first):
            srv = Server(socket_path)
            generation = "0" * 32
            lode = make_lode(
                id="test-id",
                project="proj",
                stage=stage,
                state="ready",
                status=status,
                active=True,
                tmux_pane="%1",
                pid=123,
                run_generation=generation,
                oom_scope="hopper-test.scope",
            )
            srv.lodes = [lode]
            owner = MagicMock()
            srv.lode_clients["test-id"] = owner
            srv.client_lodes[owner] = "test-id"
            srv.client_generations[owner] = generation
            message = {
                "lode_id": "test-id",
                "run_generation": generation,
                "unit_name": "hopper-test.scope",
                "unit_result": unit_result,
                "worker_returncode": 0,
            }
            broadcasts = []

            with (
                patch("hopper.lodes.current_time_ms", return_value=4242),
                patch("hopper.server.save_lodes"),
                patch("hopper.lodes.save_lodes"),
                patch(
                    "hopper.server.find_project",
                    return_value=Project(path="/repo", name="proj"),
                ),
                patch.object(srv, "broadcast", side_effect=broadcasts.append),
                patch.object(srv, "_gated_spawn") as spawn,
                patch.object(srv, "_cleanup_worktree") as cleanup,
            ):
                if report_first:
                    srv._handle_lode_run_result(message, None)
                    srv._on_client_disconnect(owner)
                else:
                    srv._on_client_disconnect(owner)
                    srv._handle_lode_run_result(message, None)
                for pending in srv.pending_disconnects.values():
                    pending["deadline"] = 0
                srv._drain_due_disconnects()

            assert not srv.pending_disconnects
            assert not srv.runner_results
            spawn.assert_not_called()
            cleanup.assert_not_called()
            assert srv.lodes == [lode]
            assert srv.archived_lodes == []
            final = lode
            assert final["failure_kind"] is None
            assert final["status"] == status
            assert final["active"] is False
            assert final["tmux_pane"] is None
            assert final["pid"] is None
            assert all(
                event["lode"].get("failure_kind") is None for event in broadcasts if "lode" in event
            )
            return json.loads(json.dumps(final))

        report_first_record = run_order(report_first=True)
        disconnect_first_record = run_order(report_first=False)
        assert report_first_record == disconnect_first_record

    @pytest.mark.parametrize("oom_first", [False, True])
    def test_disconnect_and_oom_order_never_broadcasts_inactive(
        self, socket_path, make_lode, oom_first
    ):
        srv = Server(socket_path)
        lode = make_lode(
            id="test-id",
            state="running",
            active=True,
            tmux_pane="%1",
            pid=123,
            run_generation="6" * 32,
            oom_scope="hopper-test.scope",
        )
        srv.lodes = [lode]
        owner = MagicMock()
        srv.lode_clients["test-id"] = owner
        srv.client_lodes[owner] = "test-id"
        srv.client_generations[owner] = "6" * 32
        message = {
            "lode_id": "test-id",
            "run_generation": "6" * 32,
            "unit_name": "hopper-test.scope",
            "unit_result": "oom-kill",
            "worker_returncode": 137,
        }
        broadcasts = []

        with (
            patch.object(srv, "broadcast", side_effect=broadcasts.append),
            patch.object(srv, "_gated_spawn") as spawn,
        ):
            if oom_first:
                srv._handle_lode_run_result(message, None)
                srv._on_client_disconnect(owner)
                for pending in srv.pending_disconnects.values():
                    pending["deadline"] = 0
                srv._drain_due_disconnects()
            else:
                srv._on_client_disconnect(owner)
                srv._handle_lode_run_result(message, None)

        assert lode["state"] == "error"
        assert lode["failure_kind"] == "oom"
        assert lode["status"] == format_terminal_failure_status("oom", "test-id")
        assert all(message["lode"]["state"] == "error" for message in broadcasts)
        spawn.assert_not_called()

    @pytest.mark.parametrize(
        "recovery_message",
        [
            {"type": "lode_resume", "lode_id": "test-id"},
        ],
        ids=["resume"],
    )
    def test_oom_first_disconnect_does_not_delay_explicit_recovery(
        self, socket_path, make_lode, recovery_message
    ):
        srv = Server(socket_path)
        generation = "6" * 32
        lode = make_lode(
            id="test-id",
            project="proj",
            state="running",
            active=True,
            tmux_pane="%1",
            pid=123,
            run_generation=generation,
            oom_scope="hopper-test.scope",
        )
        srv.lodes = [lode]
        owner = MagicMock()
        srv.lode_clients["test-id"] = owner
        srv.client_lodes[owner] = "test-id"
        srv.client_generations[owner] = generation

        with (
            patch("hopper.server.save_lodes"),
            patch.object(srv, "broadcast"),
        ):
            srv._handle_lode_run_result(
                {
                    "lode_id": "test-id",
                    "run_generation": generation,
                    "unit_name": "hopper-test.scope",
                    "unit_result": "oom-kill",
                    "worker_returncode": 137,
                },
                None,
            )
            srv._on_client_disconnect(owner)

        assert not srv.pending_disconnects

        with (
            patch(
                "hopper.server.find_project",
                return_value=Project(path="/repo", name="proj"),
            ),
            patch("hopper.server.oom.is_linux", return_value=False),
            patch("hopper.server.spawn_lode_processor", return_value=_spawned("%2")) as spawn,
            patch("hopper.server.save_lodes"),
            patch.object(srv, "broadcast"),
        ):
            srv._handle_mutation(recovery_message, None)

        spawn.assert_called_once()
        assert lode["run_generation"] != generation

    def test_guarded_disconnect_times_out_unverified_and_allows_same_generation_upgrade(
        self, socket_path, make_lode
    ):
        srv = Server(socket_path)
        lode = make_lode(
            id="test-id",
            state="running",
            active=True,
            tmux_pane="%1",
            pid=123,
            run_generation="7" * 32,
            oom_scope="hopper-test.scope",
        )
        srv.lodes = [lode]
        owner = MagicMock()
        srv.lode_clients["test-id"] = owner
        srv.client_lodes[owner] = "test-id"
        srv.client_generations[owner] = "7" * 32

        srv._handle_lode_run_result(
            {
                "lode_id": "test-id",
                "run_generation": "7" * 32,
                "unit_name": "hopper-test.scope",
                "unit_result": None,
                "worker_returncode": 137,
            },
            None,
        )
        with patch("hopper.server.time.monotonic", return_value=100):
            srv._on_client_disconnect(owner)
        assert lode["active"] is True
        assert srv.pending_disconnects[("test-id", "7" * 32)]["deadline"] == 160
        for pending in srv.pending_disconnects.values():
            pending["deadline"] = 0
        srv._drain_due_disconnects()

        assert lode["failure_kind"] == "runner_exit_unverified"
        assert lode["status"] == format_terminal_failure_status("runner_exit_unverified", "test-id")

        srv._handle_lode_run_result(
            {
                "lode_id": "test-id",
                "run_generation": "7" * 32,
                "unit_name": "hopper-test.scope",
                "unit_result": "oom-kill",
                "worker_returncode": 137,
            },
            None,
        )
        assert lode["failure_kind"] == "oom"
        assert lode["status"] == format_terminal_failure_status("oom", "test-id")

    def test_guarded_result_after_old_two_second_window_is_not_lost(self, socket_path, make_lode):
        generation = "7" * 32
        srv = Server(socket_path)
        lode = make_lode(
            id="test-id",
            state="running",
            active=True,
            tmux_pane="%1",
            pid=123,
            run_generation=generation,
            oom_scope="hopper-test.scope",
        )
        srv.lodes = [lode]
        owner = MagicMock()
        srv.lode_clients["test-id"] = owner
        srv.client_lodes[owner] = "test-id"
        srv.client_generations[owner] = generation

        with patch("hopper.server.time.monotonic", return_value=100):
            srv._on_client_disconnect(owner)
        assert srv.pending_disconnects[("test-id", generation)]["deadline"] == 160

        with patch("hopper.server.time.monotonic", return_value=103):
            srv._handle_lode_run_result(
                {
                    "lode_id": "test-id",
                    "run_generation": generation,
                    "unit_name": "hopper-test.scope",
                    "unit_result": "success",
                    "worker_returncode": 0,
                },
                None,
            )

        assert ("test-id", generation) not in srv.pending_disconnects
        assert lode["failure_kind"] is None

    def test_exit_137_without_authoritative_oom_is_not_oom(self, socket_path, make_lode):
        srv = Server(socket_path)
        lode = make_lode(
            id="test-id",
            state="running",
            run_generation="8" * 32,
            oom_scope="hopper-test.scope",
        )
        srv.lodes = [lode]

        srv._handle_lode_run_result(
            {
                "lode_id": "test-id",
                "run_generation": "8" * 32,
                "unit_name": "hopper-test.scope",
                "unit_result": "success",
                "worker_returncode": 137,
            },
            None,
        )

        assert lode["failure_kind"] == "runner_exit_unverified"
        assert lode["status"] != format_terminal_failure_status("oom", "test-id")

    def test_startup_consumes_oom_before_reconcile_and_preserves_terminal_latch(
        self, socket_path, temp_config, make_lode
    ):
        lode = make_lode(
            id="test-id",
            stage="shipped",
            state="running",
            active=True,
            tmux_pane="%1",
            pid=123,
            run_generation="9" * 32,
            oom_scope="hopper-test.scope",
        )
        save_lodes([lode])
        srv = Server(socket_path)
        release = MagicMock(return_value=True)
        spawn = MagicMock()
        with (
            patch(
                "hopper.server.oom.find_systemctl",
                return_value="systemctl",
            ),
            patch("hopper.server.oom.find_scope_tools", side_effect=AssertionError),
            patch("hopper.server.oom.read_scope_result", return_value="oom-kill"),
            patch("hopper.server.oom.release_scope", release),
            patch("hopper.server.pane_liveness", side_effect=AssertionError("reconciled pane")),
            patch("hopper.server.spawn_lode_processor", spawn),
        ):
            thread = threading.Thread(target=srv.start, daemon=True)
            thread.start()
            assert srv.ready.wait(5)
            srv.stop()
            thread.join(timeout=2)

        assert len(srv.lodes) == 1
        assert srv.lodes[0]["failure_kind"] == "oom"
        assert srv.lodes[0]["status"] == format_terminal_failure_status("oom", "test-id")
        release.assert_called_once_with("systemctl", "hopper-test.scope")
        spawn.assert_not_called()

    def test_unavailable_startup_result_preserves_evidence_until_deferred_failure(
        self, socket_path, make_lode
    ):
        lode = make_lode(
            id="test-id",
            stage="shipped",
            state="running",
            active=True,
            tmux_pane="%1",
            pid=123,
            run_generation="b" * 32,
            oom_scope="hopper-test.scope",
        )
        srv = Server(socket_path)
        srv.lodes = [lode]

        with (
            patch(
                "hopper.server.oom.find_systemctl",
                return_value="systemctl",
            ),
            patch("hopper.server.oom.read_scope_result", return_value=None),
            patch("hopper.server.oom.release_scope") as release,
            patch("hopper.server.pane_liveness") as pane_liveness,
            patch("hopper.server.spawn_lode_processor") as spawn,
        ):
            srv._consume_failed_oom_units()
            srv._reconcile_startup_lodes()
            assert srv._gated_spawn(lode, "/repo") == (SpawnOutcome.UNKNOWN, None)

        pane_liveness.assert_not_called()
        spawn.assert_not_called()
        release.assert_not_called()
        assert lode["oom_scope"] == "hopper-test.scope"
        assert lode["active"] is False
        for pending in srv.pending_disconnects.values():
            pending["deadline"] = 0
        srv._drain_due_disconnects()
        assert lode["failure_kind"] == "runner_exit_unverified"

    def test_startup_without_systemctl_fails_guarded_lode_closed(self, socket_path, make_lode):
        lode = make_lode(
            id="test-id",
            state="running",
            active=True,
            tmux_pane="%1",
            pid=123,
            run_generation="c" * 32,
            oom_scope="hopper-test.scope",
        )
        srv = Server(socket_path)
        srv.lodes = [lode]

        with (
            patch("hopper.server.oom.find_systemctl", return_value=None),
            patch("hopper.server.oom.read_scope_result") as read_result,
            patch("hopper.server.oom.release_scope") as release,
            patch("hopper.server.pane_liveness") as pane_liveness,
        ):
            srv._consume_failed_oom_units()
            srv._reconcile_startup_lodes()

        read_result.assert_not_called()
        release.assert_not_called()
        pane_liveness.assert_not_called()
        assert lode["failure_kind"] == "runner_exit_unverified"
        assert lode["failure_kind"] != "oom"
        assert classify(lode) == ("error", 1, None)

    def test_startup_terminal_lode_skips_unavailable_hold_but_allows_oom_upgrade(
        self, socket_path, make_lode
    ):
        lode = make_lode(
            id="test-id",
            state="error",
            status=format_terminal_failure_status("runner_exit_unverified", "test-id"),
            failure_kind="runner_exit_unverified",
            run_generation="c" * 32,
            oom_scope="hopper-test.scope",
        )
        srv = Server(socket_path)
        srv.lodes = [lode]

        with (
            patch(
                "hopper.server.oom.find_systemctl",
                return_value="systemctl",
            ),
            patch(
                "hopper.server.oom.read_scope_result",
                side_effect=[None, "oom-kill"],
            ),
            patch("hopper.server.oom.release_scope") as release,
        ):
            srv._consume_failed_oom_units()
            assert not srv.pending_disconnects
            assert lode["failure_kind"] == "runner_exit_unverified"

            srv._consume_failed_oom_units()

        assert lode["failure_kind"] == "oom"
        assert lode["status"] == format_terminal_failure_status("oom", "test-id")
        release.assert_called_once_with("systemctl", "hopper-test.scope")


def test_pane_delivery_refuses_to_paste_into_a_numbered_selector():
    """A selector is not a text box: refuse before touching it.

    Measured 2026-08-09: a lode asked a question whose third option was
    "Type something." Nudge pasted into it 42 times over 15 minutes; the text
    stayed staged with nothing able to submit it, and each retry appended to
    what was already there.
    """
    with (
        patch("hopper.server.capture_pane", return_value=_IDLE_QUESTION_CAPTURE),
        patch("hopper.server.pane_title", return_value="✳ Ready"),
        patch("hopper.server.paste_buffer", return_value=True) as mock_paste,
        patch("hopper.server.send_keys") as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "use option 3", paste=True)

    assert result["reason"] == "pane_awaiting_choice"
    # The whole point: nothing reached the pane, so nothing can accumulate.
    mock_paste.assert_not_called()
    mock_send.assert_not_called()


def test_pane_delivery_still_answers_a_numbered_selector():
    """The requested row is selected even when another row owns the cursor."""
    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[
                _EDITED_FREE_TEXT_QUESTION_CAPTURE,
                _EDITED_FREE_TEXT_QUESTION_CAPTURE,
                _IDLE_QUESTION_THIRD_CAPTURE,
                _PROCESSING_CAPTURE,
            ],
        ),
        patch("hopper.server.pane_title", side_effect=["✳ Ready", "⠐ Working"]),
        patch("hopper.server.send_keys", return_value=True) as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "3", paste=False)

    assert result["reason"] == "enter_accepted"
    assert mock_send.call_args_list == [call("%1", "Up"), call("%1", "Enter")]


def test_pane_delivery_refuses_free_text_choice_without_submitting_staged_text():
    with (
        patch("hopper.server.capture_pane", return_value=_EDITED_FREE_TEXT_QUESTION_CAPTURE),
        patch("hopper.server.pane_title", return_value="✳ Ready"),
        patch("hopper.server.send_keys") as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "4", paste=False)

    assert result["reason"] == "choice_requires_text"
    mock_send.assert_not_called()


def test_pane_delivery_refuses_number_key_in_an_ordinary_composer():
    with (
        patch("hopper.server.capture_pane", return_value=_IDLE_EMPTY_CAPTURE),
        patch("hopper.server.pane_title", return_value="✳ Ready"),
        patch("hopper.server.send_keys") as mock_send,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "3", paste=False)

    assert result["reason"] == "pane_not_awaiting_choice"
    mock_send.assert_not_called()


def test_pane_delivery_still_pastes_into_an_ordinary_composer():
    """The refusal must not fire on a normal pane -- the other direction."""
    with (
        patch(
            "hopper.server.capture_pane",
            side_effect=[
                _IDLE_EMPTY_CAPTURE,
                _IDLE_EMPTY_CAPTURE,
                _PROCESSING_CAPTURE,
            ],
        ),
        patch("hopper.server.pane_title", side_effect=["✳ Ready", "⠐ Working"]),
        patch("hopper.server.paste_buffer", return_value=True) as mock_paste,
        patch("hopper.server.time.sleep"),
    ):
        result = hopper_server._attempt_pane_delivery("%1", "continue", paste=True)

    assert result["reason"] == "auto_submitted"
    mock_paste.assert_called_once_with("%1", "continue")


def test_pane_awaiting_choice_message_names_the_verb_that_works():
    """The refusal has to hand the operator the next action, not just a diagnosis."""
    message = hopper_server._PANE_INPUT_MESSAGES["pane_awaiting_choice"].format(
        pane="%1", lode_id="abc12345", title="✳ Ready"
    )
    assert "hop lode answer abc12345" in message
    assert "Nothing was pasted" in message
    assert "Type something" in message


def _worktree_publication_message(lode_id: str, path: Path, **overrides) -> dict:
    message = {
        "type": "lode_set_worktree_path",
        "lode_id": lode_id,
        "run_generation": TEST_RUN_GENERATION,
        "project": "project-one",
        "worktree_path": str(path),
        "ack_requested": True,
    }
    message.update(overrides)
    return message


@pytest.mark.parametrize(
    "reason",
    [
        "lode_not_found",
        "missing_run_generation",
        "stale_run_generation",
        "expected_teardown",
        "terminal_failure",
        "lifecycle_grace_pending",
    ],
)
def test_worktree_publication_reports_every_runner_gate(socket_path, make_lode, reason):
    srv = Server(socket_path)
    managed = config.worktree_root() / "testid11"
    managed.mkdir(parents=True)
    lode = make_lode(
        project="project-one",
        state="running",
        active=True,
        run_generation=TEST_RUN_GENERATION,
    )
    if reason != "lode_not_found":
        srv.lodes = [lode]
    if reason == "terminal_failure":
        lode["failure_kind"] = "oom"
    if reason == "lifecycle_grace_pending":
        lode.update(state="new", active=False)

    message = _worktree_publication_message("testid11", managed)
    if reason == "missing_run_generation":
        message.pop("run_generation")
    elif reason == "stale_run_generation":
        message["run_generation"] = "b" * 32
    conn = _mock_client(srv)
    before = copy.deepcopy(lode)

    with (
        patch.object(
            srv,
            "_generation_has_teardown_intent",
            return_value=reason == "expected_teardown",
        ),
        patch("hopper.lodes.save_lodes") as save,
        patch.object(srv, "broadcast") as broadcast,
    ):
        srv._handle_mutation(message, conn)

    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == reason
    assert lode == before
    save.assert_not_called()
    broadcast.assert_not_called()


@pytest.mark.parametrize(
    "reason",
    [
        "invalid_project",
        "project_mismatch",
        "invalid_worktree_path",
        "worktree_not_absolute",
        "worktree_missing",
        "worktree_outside_root",
        "worktree_identity_mismatch",
        "worktree_provenance_conflict",
    ],
)
def test_worktree_publication_reports_every_field_refusal(socket_path, tmp_path, make_lode, reason):
    srv = Server(socket_path)
    managed = config.worktree_root() / "testid11"
    managed.mkdir(parents=True)
    lode = make_lode(
        project="project-one",
        state="running",
        active=True,
        run_generation=TEST_RUN_GENERATION,
    )
    srv.lodes = [lode]
    message = _worktree_publication_message("testid11", managed)
    if reason == "invalid_project":
        message["project"] = None
    elif reason == "project_mismatch":
        message["project"] = "project-two"
    elif reason == "invalid_worktree_path":
        message["worktree_path"] = ""
    elif reason == "worktree_not_absolute":
        message["worktree_path"] = "relative/worktree"
    elif reason == "worktree_missing":
        message["worktree_path"] = str(config.worktree_root() / "missing11")
    elif reason == "worktree_outside_root":
        outside = tmp_path / "outside-root"
        outside.mkdir()
        message["worktree_path"] = str(outside)
    elif reason == "worktree_identity_mismatch":
        other = config.worktree_root() / "other-id"
        other.mkdir()
        message["worktree_path"] = str(other)
    elif reason == "worktree_provenance_conflict":
        lode["worktree_path"] = str(config.worktree_root() / "previous-id")
    conn = _mock_client(srv)
    before = copy.deepcopy(lode)

    with (
        patch("hopper.lodes.save_lodes") as save,
        patch.object(srv, "broadcast") as broadcast,
    ):
        srv._handle_mutation(message, conn)

    response = _decode_mock_response(conn)
    assert response["accepted"] is False
    assert response["reason"] == reason
    assert lode == before
    save.assert_not_called()
    broadcast.assert_not_called()


def test_worktree_publication_is_idempotent_and_broadcasts_post_save(socket_path, make_lode):
    srv = Server(socket_path)
    managed = config.worktree_root() / "testid11"
    managed.mkdir(parents=True)
    lode = make_lode(
        project="project-one",
        state="running",
        active=True,
        run_generation=TEST_RUN_GENERATION,
        worktree_path=str(managed),
    )
    srv.lodes = [lode]
    conn = _mock_client(srv)
    events = []

    def record_broadcast(message):
        events.append(("broadcast", message["lode"]["worktree_path"]))

    with (
        patch("hopper.lodes.save_lodes", side_effect=lambda _lodes: events.append(("save", None))),
        patch.object(srv, "broadcast", side_effect=record_broadcast),
    ):
        srv._handle_mutation(_worktree_publication_message("testid11", managed), conn)

    response = _decode_mock_response(conn)
    assert response["accepted"] is True
    assert response["reason"] == "accepted"
    assert lode["worktree_path"] == str(managed)
    assert events == [("save", None), ("broadcast", str(managed))]


def test_cleanup_uses_recorded_path_even_when_legacy_candidate_exists(socket_path, make_lode):
    managed = config.worktree_root() / "testid11"
    managed.mkdir(parents=True)
    legacy = config.hopper_dir() / "lodes" / "testid11" / "worktree"
    legacy.mkdir(parents=True)
    lode = make_lode(
        project="project-one",
        branch="hopper-testid11",
        worktree_path=str(managed),
    )
    srv = Server(socket_path)

    with (
        patch("hopper.server.find_project", return_value=MagicMock(path="/project")),
        patch("hopper.server.is_dirty", return_value=False),
        patch("hopper.server.remove_worktree", return_value=True) as remove,
        patch("hopper.server.branch_exists", return_value=True),
        patch("hopper.server.delete_branch", return_value=True) as delete,
    ):
        srv._cleanup_worktree(lode)

    remove.assert_called_once_with("/project", str(managed))
    delete.assert_called_once_with("/project", "hopper-testid11")


def test_cleanup_does_not_guess_between_unrecorded_candidates(socket_path, make_lode):
    (config.worktree_root() / "testid11").mkdir(parents=True)
    (config.hopper_dir() / "lodes" / "testid11" / "worktree").mkdir(parents=True)
    srv = Server(socket_path)

    with (
        patch("hopper.server.find_project") as find_project,
        patch("hopper.server.remove_worktree") as remove,
    ):
        srv._cleanup_worktree(make_lode(project="project-one"))

    find_project.assert_not_called()
    remove.assert_not_called()

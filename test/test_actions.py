# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for durable completion records and output staging."""

import copy
import hashlib
import json
import os

import pytest

from hopper import actions


def _process(pid: int, ppid: int, pgid: int) -> dict:
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


def _record(output: dict, *, phase: str = "accepted") -> dict:
    output = {
        **output,
        "canonical_name": "mill_out.md",
        "repair_token": "A" * 43,
        "published": False,
        "failure": None,
    }
    return {
        "schema_version": 2,
        "action_id": "1" * 32,
        "lode_id": "abcd2345",
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
        "stage": "mill",
        "expected_generation": "2" * 32,
        "accepted_at_ms": 1_000,
        "boot_id": "boot-one",
        "phase": phase,
        "next_action": {"kind": "advance", "target_stage": "refine"},
        "output": output,
        "ownership": {
            "source_record_relative_path": f"run-ownership-{'2' * 32}.json",
            "source_record_sha256": "3" * 64,
            "captured": True,
            "captured_at_ms": 1_001,
            "platform": "linux",
            "proof_mode": "linux-degraded",
            "pane": {
                "pane_id": "%1",
                "window_id": "@1",
                "root_process": _process(100, 10, 100),
            },
            "supervisor": _process(101, 100, 100),
            "worker": _process(102, 101, 102),
            "process_group": 100,
            "descendants": [],
            "unit": None,
            "cgroup": None,
        },
        "containment": {
            "state": "not_started",
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
        "durability": {
            "required": False,
            "preflight": {
                "outcome": "not_required",
                "count": 0,
                "basis": "completion",
                "error": None,
                "checked_at_ms": 1_000,
            },
            "final": {
                "outcome": "not_required",
                "count": 0,
                "basis": "completion",
                "error": None,
                "checked_at_ms": 1_000,
            },
        },
        "spawn": None,
        "ship": None,
        "markers": actions.new_markers(),
        "recovery": {"kind": None, "message": None, "command": None},
        "result": None,
    }


def _stage() -> tuple[dict, dict]:
    output = actions.stage_output("abcd2345", b"accepted bytes\n", blob_id="4" * 32)
    return output, _record(output)


def _blocked_facts_text() -> str:
    return (
        f"Action {'1' * 32} owns generation {'2' * 32} for advance refine; "
        "containment: not_started. Preserved: worktree, branch, stage session"
    )


def _run_ownership() -> dict:
    pending = _record(
        {
            "blob_id": "4" * 32,
            "staged_relative_path": f"completion-staging/{'4' * 32}.blob",
            "staged_identity": {"st_dev": 1, "st_ino": 2},
            "byte_length": 1,
            "digest_algorithm": "sha256",
            "digest_hex": "5" * 64,
        }
    )
    facts = pending["ownership"]
    return {
        "schema_version": 1,
        "lode_id": pending["lode_id"],
        "run_generation": pending["expected_generation"],
        "registered_at_ms": 1_000,
        "boot_id": pending["boot_id"],
        "platform": facts["platform"],
        "proof_mode": facts["proof_mode"],
        "degraded_reason": "systemd scope unavailable",
        "pane": facts["pane"],
        "supervisor": facts["supervisor"],
        "worker": facts["worker"],
        "process_group": facts["process_group"],
        "descendants": facts["descendants"],
        "unit": facts["unit"],
        "cgroup": facts["cgroup"],
        "unit_name": None,
    }


def test_stage_output_durably_owns_and_reverifies_exact_bytes(temp_config):
    output, _pending = _stage()
    path = temp_config / "lodes/abcd2345" / output["staged_relative_path"]

    assert path.read_bytes() == b"accepted bytes\n"
    assert output["byte_length"] == 15
    assert output["digest_algorithm"] == "sha256"
    assert output["digest_hex"] == hashlib.sha256(b"accepted bytes\n").hexdigest()
    assert output["staged_identity"] == {
        "st_dev": path.stat().st_dev,
        "st_ino": path.stat().st_ino,
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_run_ownership_round_trips_and_binds_pending_record(temp_config):
    ownership = _run_ownership()
    path = actions.write_run_ownership(ownership)
    source_digest = actions.durable_json_sha256(path)
    output = actions.stage_output("abcd2345", b"x", blob_id="6" * 32)

    pending = actions.new_pending_action(
        lode_id="abcd2345",
        stage="mill",
        expected_generation="2" * 32,
        action_type="completion",
        target_disposition="advance_refine",
        force_consent=False,
        output_facts=output,
        ownership_record=actions.load_run_ownership("abcd2345", "2" * 32, require_worker=True),
        source_record_sha256=source_digest,
        action_id="7" * 32,
        accepted_ms=1_001,
    )

    assert pending["ownership"]["source_record_sha256"] == source_digest
    assert pending["ownership"]["captured"] is False
    assert pending["output"]["repair_token"]
    assert actions.validate_pending_action(pending) is pending


def test_spawn_receipt_round_trips_exact_action_identity(temp_config):
    receipt = {
        "schema_version": 1,
        "action_id": "1" * 32,
        "source_lode_id": "abcd2345",
        "target_lode_id": "bcde2345",
        "target_generation": "2" * 32,
        "pane_id": "%7",
    }

    path = actions.write_spawn_receipt(receipt)

    assert path == temp_config / "lodes/abcd2345" / f"spawn-{'1' * 32}.json"
    assert actions.load_spawn_receipt("abcd2345", "1" * 32) == receipt


def test_pending_clear_removes_only_the_action_blob_and_fence(temp_config):
    _output, record = _stage()
    actions.write_pending_action(record)
    receipt = actions.spawn_receipt_path(record["lode_id"], record["action_id"])
    receipt.write_text("retained evidence\n")
    actions.transition_marker(record, "pending_clear", "intent")
    actions.transition_marker(
        record,
        "pending_clear",
        "done",
        attempt_id=record["markers"]["pending_clear"]["attempt_id"],
    )

    actions.clear_pending_action(record)

    assert not actions.pending_action_path(record["lode_id"]).exists()
    assert not (
        actions.lode_dir(record["lode_id"]) / record["output"]["staged_relative_path"]
    ).exists()
    assert receipt.exists()


def test_partial_run_ownership_requires_worker_when_accepting_completion(temp_config):
    ownership = _run_ownership()
    ownership["worker"] = None
    ownership["descendants"] = []
    actions.write_run_ownership(ownership)

    assert actions.load_run_ownership("abcd2345", "2" * 32) == ownership
    with pytest.raises(ValueError, match="worker is not registered"):
        actions.load_run_ownership("abcd2345", "2" * 32, require_worker=True)


@pytest.mark.parametrize(
    ("length", "digest", "message"),
    [
        (14, None, "length"),
        (None, "0" * 64, "SHA-256"),
    ],
)
def test_stage_output_rejects_transport_mismatch(length, digest, message, temp_config):
    with pytest.raises(ValueError, match=message):
        actions.stage_output(
            "abcd2345",
            b"accepted bytes\n",
            expected_length=length,
            expected_sha256=digest,
        )

    assert not (temp_config / "lodes/abcd2345/completion-staging").exists()


def test_repair_staged_output_replaces_only_exact_accepted_bytes(temp_config):
    output, pending = _stage()
    staged = actions.lode_dir(pending["lode_id"]) / output["staged_relative_path"]
    staged.write_bytes(b"damaged\n")

    identity = actions.repair_staged_output(pending, b"accepted bytes\n")

    assert staged.read_bytes() == b"accepted bytes\n"
    assert identity == {"st_dev": staged.stat().st_dev, "st_ino": staged.stat().st_ino}
    assert not (actions.lode_dir(pending["lode_id"]) / "mill_out.md").exists()


@pytest.mark.parametrize("data", [b"short", b"accepted bytez\n"])
def test_repair_staged_output_rejects_nonmatching_bytes_without_mutation(data, temp_config):
    output, pending = _stage()
    staged = actions.lode_dir(pending["lode_id"]) / output["staged_relative_path"]
    before = staged.read_bytes()

    with pytest.raises(ValueError, match="length|SHA-256"):
        actions.repair_staged_output(pending, data)

    assert staged.read_bytes() == before


def test_pending_output_recovery_exposes_capability_only_for_output_block(temp_config):
    _output, pending = _stage()
    actions.transition_marker(pending, "output_publish", "intent")
    actions.transition_marker(
        pending,
        "output_publish",
        "blocked",
        attempt_id=pending["markers"]["output_publish"]["attempt_id"],
    )
    pending["phase"] = "output_blocked"
    pending["output"]["failure"] = "staged blob missing"
    pending["recovery"] = {
        "kind": "output",
        "message": "staged blob missing",
        "command": actions.recovery_command(pending, "output"),
    }

    summary = actions.pending_output_recovery(pending)

    assert summary == {
        "stage": "mill",
        "action_id": pending["action_id"],
        "sha256": pending["output"]["digest_hex"],
        "byte_length": 15,
        "repair_token": "A" * 43,
        "failure": "staged blob missing",
        "command": f"hop lode repair-output abcd2345 - --token {'A' * 43}",
    }
    pending["phase"] = "containment_blocked"
    pending["recovery"]["kind"] = "containment"
    assert actions.pending_output_recovery(pending) is None


def test_pending_record_round_trips_only_after_file_and_directory_fsync(monkeypatch):
    _output, pending = _stage()
    real_fsync = os.fsync
    fsynced = []

    def recording_fsync(fd):
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(actions.os, "fsync", recording_fsync)
    path = actions.write_pending_action(pending)

    assert actions.load_pending_action("abcd2345") == pending
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(fsynced) == 2
    assert path.read_bytes().endswith(b"\n")


def test_invalid_or_cross_lode_pending_record_fails_closed():
    _output, pending = _stage()
    pending["unexpected"] = True
    with pytest.raises(ValueError, match="unknown keys"):
        actions.write_pending_action(pending)

    assert actions.load_pending_action("abcd2345") is None


def test_pending_record_rejects_inconsistent_boot_identity():
    _output, pending = _stage()
    pending["ownership"]["supervisor"]["birth"]["boot_id"] = "other-boot"

    with pytest.raises(ValueError, match="boot identity does not match"):
        actions.write_pending_action(pending)

    assert actions.load_pending_action("abcd2345") is None


def test_load_rejects_truncated_pending_record(temp_config):
    path = temp_config / "lodes/abcd2345/pending-completion.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":1')

    with pytest.raises(ValueError):
        actions.load_pending_action("abcd2345")


def test_load_v1_pending_record_fails_closed_with_upgrade_instruction(temp_config):
    path = temp_config / "lodes/abcd2345/pending-completion.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version": 1}\n')

    with pytest.raises(actions.LegacyPendingActionError, match="drained before.*upgraded"):
        actions.load_pending_action("abcd2345")


def test_manual_action_has_no_output_and_dispatches_next_action():
    record = actions.new_pending_action(
        lode_id="abcd2345",
        stage="refine",
        expected_generation=None,
        action_type="pause",
        target_disposition="paused",
        force_consent=False,
        action_id="7" * 32,
        accepted_ms=1_001,
    )

    assert record["output"] is None
    assert record["next_action"] == {"kind": "pause", "target_stage": None}
    assert actions.validate_pending_action(record) is record


def test_action_binding_requires_real_bool_after_json_round_trip():
    fields = {
        "lode_id": "abcd2345",
        "expected_generation": "2" * 32,
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
    }
    reversed_fields = {key: fields[key] for key in reversed(tuple(fields))}

    first = json.loads(json.dumps(fields, separators=(",", ":")))
    second = json.loads(json.dumps(reversed_fields, indent=2))

    assert actions.action_binding(first) == actions.action_binding(second)
    with pytest.raises(ValueError, match="must be a boolean"):
        actions.action_binding({**fields, "force_consent": 0})


def test_action_results_keep_eight_newest_in_publication_order():
    _output, pending = _stage()
    lode = {"id": pending["lode_id"], "action_results": []}

    for index in range(9):
        current = copy.deepcopy(pending)
        current["action_id"] = f"{index + 1:032x}"
        result = actions.new_action_result(current, completed_ms=2_000 + index)
        actions.append_action_result(lode, result)

    assert [item["action_id"] for item in lode["action_results"]] == [
        f"{index:032x}" for index in range(2, 10)
    ]
    assert actions.find_action_result(lode, f"{1:032x}") is None


def test_publish_output_verifies_before_replacing_canonical(temp_config):
    output, pending = _stage()
    canonical = temp_config / "lodes/abcd2345/mill_out.md"
    canonical.write_bytes(b"old canonical\n")

    assert actions.publish_output(pending) == canonical
    assert canonical.read_bytes() == b"accepted bytes\n"

    staged = temp_config / "lodes/abcd2345" / output["staged_relative_path"]
    staged.write_bytes(b"corrupt bytes\n")
    canonical.write_bytes(b"known good\n")
    with pytest.raises(ValueError, match="accepted digest"):
        actions.publish_output(pending)
    assert canonical.read_bytes() == b"known good\n"


def test_publish_output_rejects_replaced_staged_inode(temp_config):
    output, pending = _stage()
    staged = temp_config / "lodes/abcd2345" / output["staged_relative_path"]
    replacement = staged.with_suffix(".replacement")
    replacement.write_bytes(b"accepted bytes\n")
    os.replace(replacement, staged)

    with pytest.raises(OSError, match="identity changed"):
        actions.publish_output(pending)


def test_collect_orphans_keeps_only_recorded_blob(temp_config):
    output, pending = _stage()
    directory = temp_config / "lodes/abcd2345/completion-staging"
    orphan = directory / f"{'5' * 32}.blob"
    temporary = directory / f"{'6' * 32}.blob.{'7' * 32}.tmp"
    unrelated = directory / "operator-note"
    for path in (orphan, temporary, unrelated):
        path.write_bytes(b"x")

    removed = actions.collect_orphaned_staging("abcd2345", pending)

    assert set(removed) == {orphan, temporary}
    assert (directory / f"{output['blob_id']}.blob").exists()
    assert unrelated.exists()


def test_invalid_pending_record_prevents_orphan_collection(temp_config):
    _output, pending = _stage()
    orphan = temp_config / f"lodes/abcd2345/completion-staging/{'5' * 32}.blob"
    orphan.write_bytes(b"x")
    pending["output"]["digest_hex"] = "bad"

    with pytest.raises(ValueError, match="invalid format"):
        actions.collect_orphaned_staging("abcd2345", pending)
    assert orphan.exists()


def test_marker_transitions_require_intent_and_matching_attempt():
    _output, pending = _stage()
    actions.transition_marker(
        pending,
        "output_publish",
        "intent",
        attempt_id="8" * 32,
        detail="publish",
    )
    actions.transition_marker(
        pending,
        "output_publish",
        "done",
        attempt_id="8" * 32,
        detail="verified",
    )
    assert pending["markers"]["output_publish"] == {
        "state": "done",
        "attempt_id": "8" * 32,
        "detail": "verified",
    }
    with pytest.raises(ValueError, match="illegal marker transition"):
        actions.transition_marker(pending, "output_publish", "intent")


def test_marker_result_rejects_stale_attempt():
    _output, pending = _stage()
    actions.transition_marker(pending, "containment", "intent", attempt_id="8" * 32)
    with pytest.raises(ValueError, match="does not match"):
        actions.transition_marker(
            pending,
            "containment",
            "blocked",
            attempt_id="9" * 32,
        )


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("accepted", "Teardown: publishing accepted mill output"),
        ("capturing_ownership", "Teardown: capturing generation ownership"),
        ("closing_pane", "Teardown: closing pane %1"),
        (
            "observing_containment",
            "Teardown: waiting for bounded Linux ownership (degraded; leak-free cleanup unproven)",
        ),
        (
            "publishing_terminal",
            "Teardown: bounded Linux containment observed "
            "(degraded; leak-free cleanup unproven); awaiting next action",
        ),
        ("spawning", "Teardown: starting refine"),
    ],
)
def test_completion_status_projects_phase(phase, expected):
    _output, pending = _stage()
    pending["phase"] = phase
    assert actions.action_status(pending) == expected


def test_completion_status_projects_exact_output_recovery():
    output, pending = _stage()
    pending["phase"] = "output_blocked"
    pending["recovery"] = {
        "kind": "output",
        "message": "staged output unavailable",
        "command": actions.recovery_command(pending, "output"),
    }
    assert actions.action_status(pending) == (
        "Teardown blocked: accepted mill output is unavailable "
        f"(sha256 {output['digest_hex']}, 15 bytes). Repair with: "
        f"hop lode repair-output abcd2345 - --token {'A' * 43}. "
        f"{_blocked_facts_text()}"
    )


def test_completion_status_projects_retryable_publication_failure():
    _output, pending = _stage()
    pending["phase"] = "output_blocked"
    pending["recovery"] = {
        "kind": "publication",
        "message": "canonical directory is temporarily read-only",
        "command": actions.recovery_command(pending, "publication"),
    }

    assert actions.action_status(pending) == (
        "Teardown blocked: canonical directory is temporarily read-only. "
        f"Retry with: hop lode restart abcd2345. {_blocked_facts_text()}"
    )
    assert actions.pending_output_recovery(pending) is None


def test_linux_degraded_completion_status_preserves_birth_identity_proof():
    _output, pending = _stage()
    pending["stage"] = "ship"
    pending["next_action"] = {"kind": "ship_archive", "target_stage": None}
    pending["ship"] = {
        "provenance": {
            name: {"realpath": f"/tmp/{name}", "identity": {"st_dev": 1, "st_ino": index}}
            for index, name in enumerate(
                ("project", "git_common_dir", "worktree", "worktree_git_dir"), start=1
            )
        }
        | {
            "branch_ref": "refs/heads/hopper-abcd2345",
            "branch_oid": "a" * 40,
            "head_oid": "a" * 40,
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
            "original_path": "/tmp/worktree",
            "quarantine_path": "/tmp/quarantine",
            "expected_identity": {"st_dev": 1, "st_ino": 4},
            "registration_repaired": False,
            "removal_outcome": "pending",
            "branch_outcome": "pending",
        },
        "cleanup_failure": None,
    }
    pending["phase"] = "complete"

    status = actions.action_status(pending)

    assert status == (
        "Shipped (bounded Linux teardown; systemd proof and leak-free cleanup unproven)"
    )
    assert "birth identity" not in status


def test_completion_status_projects_generic_block():
    _output, pending = _stage()
    pending["phase"] = "containment_blocked"
    pending["recovery"] = {
        "kind": "containment",
        "message": "cgroup identity changed",
        "command": "hop lode restart abcd2345",
    }
    assert actions.action_status(pending) == (
        "Teardown blocked: cgroup identity changed. Retry with: hop lode restart abcd2345. "
        f"{_blocked_facts_text()}"
    )


def test_blocked_archive_recovery_names_the_cli_action_and_complete_projection():
    pending = actions.new_pending_action(
        lode_id="abcd2345",
        stage="mill",
        expected_generation=None,
        action_type="archive",
        target_disposition="archived",
        force_consent=False,
        action_id="7" * 32,
        already_empty=True,
    )
    pending["phase"] = "durability_blocked"
    pending["recovery"] = {
        "kind": "durability",
        "message": "worktree durability could not be proven",
        "command": actions.recovery_command(pending, "durability"),
    }

    projection = actions.pending_action_projection(pending)

    assert projection["recovery"]["command"] == f"hop lode archive {pending['lode_id']}"
    assert projection["containment"]["state"] == "proven"
    assert projection["preserved"] == {
        "worktree": True,
        "branch": True,
        "stage_session": True,
    }
    assert "Inspect with" not in projection["status"]
    assert f"Retry: hop lode archive {pending['lode_id']}" in projection["status"]


def test_validate_pending_completion_does_not_mutate_input():
    _output, pending = _stage()
    before = copy.deepcopy(pending)
    assert actions.validate_pending_action(pending) is pending
    assert pending == before

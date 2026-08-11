# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Durable storage primitives for accepted lode completions."""

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path

from hopper import config

PENDING_COMPLETION_FILENAME = "pending-completion.json"
COMPLETION_STAGING_DIRNAME = "completion-staging"
RUN_OWNERSHIP_PREFIX = "run-ownership-"
SCHEMA_VERSION = 1
DIGEST_ALGORITHM = "sha256"
POLL_INTERVAL_MS = 50

STAGES = {"mill", "refine", "ship"}
PHASES = {
    "accepted",
    "output_blocked",
    "publishing_output",
    "capturing_ownership",
    "closing_pane",
    "observing_containment",
    "force_killing",
    "containment_blocked",
    "proving_ship_landing",
    "ship_blocked",
    "quarantining",
    "publishing_next_action",
    "spawning",
    "cleanup_blocked",
    "complete",
}
MARKER_NAMES = (
    "output_publish",
    "ownership_capture",
    "pane_close",
    "containment",
    "scope_kill",
    "supervisor_kill",
    "ship_landing",
    "quarantine_rename",
    "worktree_repair",
    "cleanup_authorization",
    "stage_mutation",
    "archive",
    "backlog",
    "spawn",
    "worktree_remove",
    "branch_delete",
    "pending_clear",
)
MARKER_STATES = {"not_started", "intent", "done", "blocked"}
MARKER_TRANSITIONS = {
    "not_started": {"intent"},
    "intent": {"done", "blocked"},
    "blocked": {"intent"},
    "done": set(),
}

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HEX_OID = re.compile(r"[0-9a-f]{40,64}\Z")
_LODE_ID = re.compile(r"[a-z2-7]{8}\Z")
_REPAIR_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_STAGED_PATH = re.compile(r"completion-staging/([0-9a-f]{32})\.blob\Z")
_OWNERSHIP_PATH = re.compile(r"run-ownership-([0-9a-f]{32})\.json\Z")
_SPAWN_PATH = re.compile(r"spawn-([0-9a-f]{32})\.json\Z")
_STAGING_FILE = re.compile(r"[0-9a-f]{32}\.blob(?:\.[0-9a-f]{32}\.tmp)?\Z")


def lode_dir(lode_id: str) -> Path:
    """Return the persistent directory for one lode."""
    if not isinstance(lode_id, str) or _LODE_ID.fullmatch(lode_id) is None:
        raise ValueError("lode ID must be eight lowercase base32 characters")
    return config.hopper_dir() / "lodes" / lode_id


def pending_completion_path(lode_id: str) -> Path:
    """Return the canonical pending-completion path for a lode."""
    return lode_dir(lode_id) / PENDING_COMPLETION_FILENAME


def staging_dir(lode_id: str) -> Path:
    """Return the server-owned completion staging directory for a lode."""
    return lode_dir(lode_id) / COMPLETION_STAGING_DIRNAME


def run_ownership_path(lode_id: str, run_generation: str) -> Path:
    """Return the durable launch-ownership path for one generation."""
    if _HEX32.fullmatch(run_generation) is None:
        raise ValueError("run generation must be 32 lowercase hexadecimal characters")
    return lode_dir(lode_id) / f"{RUN_OWNERSHIP_PREFIX}{run_generation}.json"


def spawn_receipt_path(lode_id: str, action_id: str) -> Path:
    """Return the action-scoped pane bootstrap receipt path."""
    if _HEX32.fullmatch(action_id) is None:
        raise ValueError("completion action ID must be 32 lowercase hexadecimal characters")
    return lode_dir(lode_id) / f"spawn-{action_id}.json"


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_replace(path: Path, writer) -> None:
    """Populate and durably replace a file using a writer-unique sibling."""
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _fsync_directory(path.parent.parent)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            fd = None
            writer(output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _canonical_json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def write_durable_json(path: Path, value: dict) -> None:
    """Write canonical JSON with file and containing-directory durability."""
    payload = _canonical_json_bytes(value)
    _durable_replace(path, lambda output: output.write(payload))


def durable_json_sha256(path: Path) -> str:
    """Return the SHA-256 of exact durable JSON bytes."""
    return staged_output_sha256(path)


def _object(value, name: str, keys: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unknown = sorted(actual - keys)
        raise ValueError(f"{name} has missing keys {missing} and unknown keys {unknown}")
    return value


def _string(value, name: str, *, choices=None, pattern=None, nullable=False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if choices is not None and value not in choices:
        raise ValueError(f"{name} has an unsupported value")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid format")
    return value


def _integer(value, name: str, *, minimum=0, nullable=False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _boolean(value, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")


def _filesystem_identity(value, name: str) -> None:
    value = _object(value, name, {"st_dev", "st_ino"})
    _integer(value["st_dev"], f"{name}.st_dev")
    _integer(value["st_ino"], f"{name}.st_ino", minimum=1)


def _path_identity(value, name: str) -> None:
    value = _object(value, name, {"realpath", "identity"})
    realpath = _string(value["realpath"], f"{name}.realpath")
    if not realpath.startswith("/"):
        raise ValueError(f"{name}.realpath must be absolute")
    _filesystem_identity(value["identity"], f"{name}.identity")


def _birth_identity(value, name: str) -> None:
    value = _object(value, name, {"kind", "boot_id", "value"})
    kind = _string(
        value["kind"],
        f"{name}.kind",
        choices={"linux-proc-starttime", "ps-lstart", "unavailable"},
    )
    _string(value["boot_id"], f"{name}.boot_id", nullable=True)
    _string(value["value"], f"{name}.value", nullable=True)
    if kind == "linux-proc-starttime":
        if not value["boot_id"] or not value["value"]:
            raise ValueError(f"{name} Linux birth identity is incomplete")
    elif kind == "ps-lstart":
        if value["boot_id"] is not None or not value["value"]:
            raise ValueError(f"{name} ps birth identity is incomplete")
    elif value["boot_id"] is not None or value["value"] is not None:
        raise ValueError(f"{name} unavailable birth identity must contain nulls")


def _process_identity(value, name: str) -> None:
    value = _object(value, name, {"pid", "ppid", "pgid", "birth"})
    _integer(value["pid"], f"{name}.pid", minimum=1)
    _integer(value["ppid"], f"{name}.ppid")
    _integer(value["pgid"], f"{name}.pgid", minimum=1)
    _birth_identity(value["birth"], f"{name}.birth")


def _marker(value, name: str) -> None:
    value = _object(value, name, {"state", "attempt_id", "detail"})
    _string(value["state"], f"{name}.state", choices=MARKER_STATES)
    attempt = _string(value["attempt_id"], f"{name}.attempt_id", nullable=True)
    if attempt is not None and _HEX32.fullmatch(attempt) is None:
        raise ValueError(f"{name}.attempt_id has an invalid format")
    _string(value["detail"], f"{name}.detail", nullable=True)


def _validate_output(value: dict, stage: str) -> None:
    keys = {
        "blob_id",
        "staged_relative_path",
        "staged_identity",
        "canonical_name",
        "byte_length",
        "digest_algorithm",
        "digest_hex",
        "repair_token",
        "published",
        "failure",
    }
    value = _object(value, "output", keys)
    blob_id = _string(value["blob_id"], "output.blob_id", pattern=_HEX32)
    staged_path = _string(
        value["staged_relative_path"], "output.staged_relative_path", pattern=_STAGED_PATH
    )
    if staged_path != f"{COMPLETION_STAGING_DIRNAME}/{blob_id}.blob":
        raise ValueError("output staged path does not match blob ID")
    _filesystem_identity(value["staged_identity"], "output.staged_identity")
    if value["canonical_name"] != f"{stage}_out.md":
        raise ValueError("output canonical name does not match stage")
    _integer(value["byte_length"], "output.byte_length", minimum=1)
    if value["digest_algorithm"] != DIGEST_ALGORITHM:
        raise ValueError("output digest algorithm must be sha256")
    _string(value["digest_hex"], "output.digest_hex", pattern=_HEX_DIGEST)
    _string(value["repair_token"], "output.repair_token", pattern=_REPAIR_TOKEN)
    _boolean(value["published"], "output.published")
    _string(value["failure"], "output.failure", nullable=True)


def _validate_ownership(value: dict, generation: str, record_boot_id: str) -> None:
    keys = {
        "source_record_relative_path",
        "source_record_sha256",
        "captured",
        "captured_at_ms",
        "platform",
        "proof_mode",
        "pane",
        "supervisor",
        "worker",
        "process_group",
        "descendants",
        "unit",
        "cgroup",
    }
    value = _object(value, "ownership", keys)
    source = _string(
        value["source_record_relative_path"],
        "ownership.source_record_relative_path",
        pattern=_OWNERSHIP_PATH,
    )
    if source != f"run-ownership-{generation}.json":
        raise ValueError("ownership source path does not match generation")
    _string(value["source_record_sha256"], "ownership.source_record_sha256", pattern=_HEX_DIGEST)
    _boolean(value["captured"], "ownership.captured")
    _integer(value["captured_at_ms"], "ownership.captured_at_ms", nullable=True)
    if value["captured"] != (value["captured_at_ms"] is not None):
        raise ValueError("ownership capture flag and timestamp disagree")
    platform = _string(
        value["platform"], "ownership.platform", choices={"linux", "darwin", "other"}
    )
    proof_mode = _string(
        value["proof_mode"],
        "ownership.proof_mode",
        choices={
            "linux-strict",
            "linux-degraded",
            "darwin-bounded",
            "other-bounded-no-birth",
        },
    )
    pane = _object(value["pane"], "ownership.pane", {"pane_id", "window_id", "root_process"})
    _string(pane["pane_id"], "ownership.pane.pane_id")
    _string(pane["window_id"], "ownership.pane.window_id")
    _process_identity(pane["root_process"], "ownership.pane.root_process")
    _process_identity(value["supervisor"], "ownership.supervisor")
    _process_identity(value["worker"], "ownership.worker")
    _integer(value["process_group"], "ownership.process_group", minimum=1)
    if not isinstance(value["descendants"], list):
        raise ValueError("ownership.descendants must be an array")
    for index, process in enumerate(value["descendants"]):
        _process_identity(process, f"ownership.descendants[{index}]")
    descendant_pids = [process["pid"] for process in value["descendants"]]
    if descendant_pids != sorted(set(descendant_pids)):
        raise ValueError("ownership descendants must have unique sorted PIDs")
    if value["supervisor"]["pgid"] != value["process_group"]:
        raise ValueError("ownership process group does not match supervisor")
    unit = value["unit"]
    if unit is not None:
        unit = _object(unit, "ownership.unit", {"name", "load_state", "control_group"})
        _string(unit["name"], "ownership.unit.name")
        _string(unit["load_state"], "ownership.unit.load_state")
        control_group = _string(unit["control_group"], "ownership.unit.control_group")
        if not control_group.startswith("/"):
            raise ValueError("ownership.unit.control_group must be absolute")
    cgroup = value["cgroup"]
    if cgroup is not None:
        cgroup = _object(
            cgroup, "ownership.cgroup", {"relative_path", "absolute_path", "identity", "boot_id"}
        )
        for key in ("relative_path", "absolute_path"):
            path = _string(cgroup[key], f"ownership.cgroup.{key}")
            if not path.startswith("/"):
                raise ValueError(f"ownership.cgroup.{key} must be absolute")
        _filesystem_identity(cgroup["identity"], "ownership.cgroup.identity")
        _string(cgroup["boot_id"], "ownership.cgroup.boot_id")
        if cgroup["boot_id"] != record_boot_id:
            raise ValueError("ownership cgroup boot identity does not match record")

    births = [pane["root_process"]["birth"], value["supervisor"]["birth"], value["worker"]["birth"]]
    births.extend(process["birth"] for process in value["descendants"])
    if proof_mode == "linux-strict":
        if platform != "linux" or unit is None or cgroup is None:
            raise ValueError("strict Linux ownership requires unit and cgroup")
        if any(birth["kind"] != "linux-proc-starttime" for birth in births):
            raise ValueError("strict Linux ownership requires proc start times")
        if any(birth["boot_id"] != record_boot_id for birth in births):
            raise ValueError("strict Linux process boot identity does not match record")
    if proof_mode == "linux-degraded" and (
        platform != "linux"
        or unit is not None
        or cgroup is not None
        or any(birth["kind"] != "linux-proc-starttime" for birth in births)
    ):
        raise ValueError("degraded Linux ownership requires proc start times and no cgroup")
    if proof_mode == "linux-degraded" and any(
        birth["boot_id"] != record_boot_id for birth in births
    ):
        raise ValueError("degraded Linux process boot identity does not match record")
    if proof_mode == "darwin-bounded" and (
        platform != "darwin"
        or unit is not None
        or cgroup is not None
        or any(birth["kind"] != "ps-lstart" for birth in births)
    ):
        raise ValueError("Darwin ownership requires ps start-time identities")
    if proof_mode == "other-bounded-no-birth" and (
        platform != "other"
        or unit is not None
        or cgroup is not None
        or any(birth["kind"] != "ps-lstart" for birth in births)
    ):
        raise ValueError("other bounded ownership requires opaque ps observation tokens")


def _validate_containment(value: dict) -> None:
    keys = {
        "state",
        "started_monotonic_ns",
        "deadline_monotonic_ns",
        "poll_interval_ms",
        "last_cgroup_observation",
        "last_supervisor_observation",
        "last_owned_process_count",
        "result",
        "proof_label",
        "last_error",
    }
    value = _object(value, "containment", keys)
    _string(
        value["state"],
        "containment.state",
        choices={
            "not_started",
            "pane_close_pending",
            "grace",
            "kill_pending",
            "verify_after_kill",
            "proven",
            "blocked",
        },
    )
    _integer(value["started_monotonic_ns"], "containment.started_monotonic_ns", nullable=True)
    _integer(value["deadline_monotonic_ns"], "containment.deadline_monotonic_ns", nullable=True)
    if value["poll_interval_ms"] != POLL_INTERVAL_MS:
        raise ValueError("containment.poll_interval_ms must be 50")
    _string(
        value["last_cgroup_observation"],
        "containment.last_cgroup_observation",
        choices={"empty", "populated", "cannot-tell"},
        nullable=True,
    )
    _string(
        value["last_supervisor_observation"],
        "containment.last_supervisor_observation",
        choices={"gone", "alive", "cannot-tell"},
        nullable=True,
    )
    _integer(
        value["last_owned_process_count"], "containment.last_owned_process_count", nullable=True
    )
    _string(
        value["result"],
        "containment.result",
        choices={
            "linux-strict-empty",
            "linux-strict-killed-empty",
            "linux-degraded-bounded-empty",
            "darwin-bounded-empty",
            "other-bounded-empty-no-birth",
        },
        nullable=True,
    )
    _string(value["proof_label"], "containment.proof_label", nullable=True)
    _string(value["last_error"], "containment.last_error", nullable=True)


def _validate_spawn(value) -> None:
    if value is None:
        return
    value = _object(
        value,
        "spawn",
        {
            "target_generation",
            "receipt_relative_path",
            "pane_id",
            "supervisor_adopted",
            "worker_adopted",
        },
    )
    _string(value["target_generation"], "spawn.target_generation", pattern=_HEX32)
    _string(value["receipt_relative_path"], "spawn.receipt_relative_path", pattern=_SPAWN_PATH)
    _string(value["pane_id"], "spawn.pane_id", nullable=True)
    _boolean(value["supervisor_adopted"], "spawn.supervisor_adopted")
    _boolean(value["worker_adopted"], "spawn.worker_adopted")


def _validate_ship(value) -> None:
    value = _object(
        value,
        "ship",
        {"provenance", "landing", "backlog", "archive_published", "quarantine", "cleanup_failure"},
    )
    provenance = _object(
        value["provenance"],
        "ship.provenance",
        {
            "project",
            "git_common_dir",
            "worktree",
            "worktree_git_dir",
            "branch_ref",
            "branch_oid",
            "head_oid",
        },
    )
    for key in ("project", "git_common_dir", "worktree", "worktree_git_dir"):
        _path_identity(provenance[key], f"ship.provenance.{key}")
    branch_ref = _string(provenance["branch_ref"], "ship.provenance.branch_ref")
    if not branch_ref.startswith("refs/heads/"):
        raise ValueError("ship.provenance.branch_ref must be a full branch ref")
    _string(provenance["branch_oid"], "ship.provenance.branch_oid", pattern=_HEX_OID)
    _string(provenance["head_oid"], "ship.provenance.head_oid", pattern=_HEX_OID)
    landing = _object(value["landing"], "ship.landing", {"cause", "base_ref", "detail", "accepted"})
    for key in ("cause", "base_ref", "detail"):
        _string(landing[key], f"ship.landing.{key}", nullable=True)
    _boolean(landing["accepted"], "ship.landing.accepted")
    backlog = _object(
        value["backlog"],
        "ship.backlog",
        {"planned", "selected_item_id", "promoted_lode_id", "remaining_item_ids", "applied"},
    )
    _boolean(backlog["planned"], "ship.backlog.planned")
    _string(backlog["selected_item_id"], "ship.backlog.selected_item_id", nullable=True)
    _string(backlog["promoted_lode_id"], "ship.backlog.promoted_lode_id", nullable=True)
    if not isinstance(backlog["remaining_item_ids"], list) or not all(
        isinstance(item, str) for item in backlog["remaining_item_ids"]
    ):
        raise ValueError("ship.backlog.remaining_item_ids must be an array of strings")
    _boolean(backlog["applied"], "ship.backlog.applied")
    _boolean(value["archive_published"], "ship.archive_published")
    quarantine = _object(
        value["quarantine"],
        "ship.quarantine",
        {
            "original_path",
            "quarantine_path",
            "expected_identity",
            "registration_repaired",
            "removal_outcome",
            "branch_outcome",
        },
    )
    for key in ("original_path", "quarantine_path"):
        path = _string(quarantine[key], f"ship.quarantine.{key}")
        if not path.startswith("/"):
            raise ValueError(f"ship.quarantine.{key} must be absolute")
    _filesystem_identity(quarantine["expected_identity"], "ship.quarantine.expected_identity")
    _boolean(quarantine["registration_repaired"], "ship.quarantine.registration_repaired")
    _string(
        quarantine["removal_outcome"],
        "ship.quarantine.removal_outcome",
        choices={"pending", "removed", "retained"},
    )
    _string(
        quarantine["branch_outcome"],
        "ship.quarantine.branch_outcome",
        choices={"pending", "deleted", "already_absent", "retained"},
    )
    _string(value["cleanup_failure"], "ship.cleanup_failure", nullable=True)


def validate_pending_completion(record: dict) -> dict:
    """Validate the complete v1 pending record and return it unchanged."""
    keys = {
        "schema_version",
        "action_id",
        "lode_id",
        "stage",
        "run_generation",
        "accepted_at_ms",
        "boot_id",
        "phase",
        "next_action",
        "output",
        "ownership",
        "containment",
        "spawn",
        "ship",
        "markers",
        "recovery",
    }
    record = _object(record, "pending completion", keys)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported pending completion schema")
    _string(record["action_id"], "action_id", pattern=_HEX32)
    _string(record["lode_id"], "lode_id", pattern=_LODE_ID)
    stage = _string(record["stage"], "stage", choices=STAGES)
    generation = _string(record["run_generation"], "run_generation", pattern=_HEX32)
    _integer(record["accepted_at_ms"], "accepted_at_ms")
    boot_id = _string(record["boot_id"], "boot_id")
    if not boot_id:
        raise ValueError("boot_id must not be empty")
    _string(record["phase"], "phase", choices=PHASES)
    next_action = _object(record["next_action"], "next_action", {"kind", "target_stage"})
    expected_action = {
        "mill": ("advance", "refine"),
        "refine": ("advance", "ship"),
        "ship": ("ship_archive", None),
    }[stage]
    if (next_action["kind"], next_action["target_stage"]) != expected_action:
        raise ValueError("next action does not match stage")
    _validate_output(record["output"], stage)
    _validate_ownership(record["ownership"], generation, boot_id)
    _validate_containment(record["containment"])
    _validate_spawn(record["spawn"])
    if stage == "ship":
        if record["ship"] is None:
            raise ValueError("ship completion requires ship facts")
        _validate_ship(record["ship"])
    elif record["ship"] is not None:
        raise ValueError("non-ship completion cannot contain ship facts")
    markers = _object(record["markers"], "markers", set(MARKER_NAMES))
    for name in MARKER_NAMES:
        _marker(markers[name], f"markers.{name}")
    if stage != "ship":
        for name in (
            "ship_landing",
            "quarantine_rename",
            "worktree_repair",
            "cleanup_authorization",
            "archive",
            "backlog",
            "worktree_remove",
            "branch_delete",
        ):
            if markers[name] != new_marker():
                raise ValueError(f"non-ship marker {name} must not be started")
    recovery = _object(record["recovery"], "recovery", {"kind", "message", "command"})
    _string(
        recovery["kind"],
        "recovery.kind",
        choices={
            "output",
            "publication",
            "ownership",
            "containment",
            "landing",
            "spawn",
            "cleanup",
        },
        nullable=True,
    )
    _string(recovery["message"], "recovery.message", nullable=True)
    _string(recovery["command"], "recovery.command", nullable=True)
    return record


def validate_run_ownership(record: dict, *, require_worker: bool = False) -> dict:
    """Validate the durable supervisor/worker launch identity record."""
    keys = {
        "schema_version",
        "lode_id",
        "run_generation",
        "registered_at_ms",
        "boot_id",
        "platform",
        "proof_mode",
        "degraded_reason",
        "pane",
        "supervisor",
        "worker",
        "process_group",
        "descendants",
        "unit",
        "cgroup",
        "unit_name",
    }
    record = _object(record, "run ownership", keys)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported run ownership schema")
    _string(record["lode_id"], "run ownership lode_id", pattern=_LODE_ID)
    _string(record["run_generation"], "run ownership generation", pattern=_HEX32)
    _integer(record["registered_at_ms"], "run ownership registered_at_ms")
    boot_id = _string(record["boot_id"], "run ownership boot_id")
    if not boot_id:
        raise ValueError("run ownership boot identity is empty")
    platform = _string(
        record["platform"], "run ownership platform", choices={"linux", "darwin", "other"}
    )
    proof_mode = _string(
        record["proof_mode"],
        "run ownership proof_mode",
        choices={
            "linux-strict",
            "linux-degraded",
            "darwin-bounded",
            "other-bounded-no-birth",
        },
    )
    _string(record["degraded_reason"], "run ownership degraded_reason", nullable=True)
    pane = _object(record["pane"], "run ownership pane", {"pane_id", "window_id", "root_process"})
    _string(pane["pane_id"], "run ownership pane_id")
    _string(pane["window_id"], "run ownership window_id")
    _process_identity(pane["root_process"], "run ownership pane root")
    _process_identity(record["supervisor"], "run ownership supervisor")
    if record["worker"] is not None:
        _process_identity(record["worker"], "run ownership worker")
    elif require_worker:
        raise ValueError("run ownership worker is not registered")
    _integer(record["process_group"], "run ownership process_group", minimum=1)
    if record["supervisor"]["pgid"] != record["process_group"]:
        raise ValueError("run ownership process group does not match supervisor")
    _string(record["unit_name"], "run ownership unit_name", nullable=True)
    if not isinstance(record["descendants"], list):
        raise ValueError("run ownership descendants must be an array")
    for index, process in enumerate(record["descendants"]):
        _process_identity(process, f"run ownership descendants[{index}]")
    if record["worker"] is None:
        if record["descendants"] or record["unit"] is not None or record["cgroup"] is not None:
            raise ValueError("partial run ownership contains final capture facts")
    else:
        pending_shape = {
            "source_record_relative_path": f"run-ownership-{record['run_generation']}.json",
            "source_record_sha256": "0" * 64,
            "captured": True,
            "captured_at_ms": record["registered_at_ms"],
            "platform": platform,
            "proof_mode": proof_mode,
            "pane": record["pane"],
            "supervisor": record["supervisor"],
            "worker": record["worker"],
            "process_group": record["process_group"],
            "descendants": record["descendants"],
            "unit": record["unit"],
            "cgroup": record["cgroup"],
        }
        _validate_ownership(pending_shape, record["run_generation"], boot_id)
    if proof_mode == "linux-strict":
        if platform != "linux" or not record["unit_name"] or record["degraded_reason"] is not None:
            raise ValueError("strict Linux run ownership requires a unit and no degradation")
        if require_worker and (record["unit"] is None or record["cgroup"] is None):
            raise ValueError("strict Linux run ownership requires captured systemd facts")
    else:
        if record["unit_name"] is not None:
            raise ValueError("degraded run ownership cannot claim a systemd unit")
        if not record["degraded_reason"]:
            raise ValueError("degraded run ownership requires a reason")
    launch_births = [pane["root_process"]["birth"], record["supervisor"]["birth"]]
    if platform == "linux" and (
        any(birth["kind"] != "linux-proc-starttime" for birth in launch_births)
        or any(birth["boot_id"] != boot_id for birth in launch_births)
    ):
        raise ValueError("Linux run ownership requires matching proc birth identities")
    if platform != "linux" and any(birth["kind"] != "ps-lstart" for birth in launch_births):
        raise ValueError("bounded run ownership requires opaque ps birth identities")
    return record


def write_run_ownership(record: dict) -> Path:
    """Durably write one launch ownership record."""
    validate_run_ownership(record)
    path = run_ownership_path(record["lode_id"], record["run_generation"])
    write_durable_json(path, record)
    return path


def load_run_ownership(lode_id: str, run_generation: str, *, require_worker=False) -> dict | None:
    """Load one launch ownership record, preserving absence as distinct."""
    path = run_ownership_path(lode_id, run_generation)
    try:
        with open(path, encoding="utf-8") as source:
            record = json.load(source)
    except FileNotFoundError:
        return None
    validate_run_ownership(record, require_worker=require_worker)
    if record["lode_id"] != lode_id or record["run_generation"] != run_generation:
        raise ValueError("run ownership identity does not match its path")
    return record


def new_pending_completion(
    *,
    lode_id: str,
    stage: str,
    run_generation: str,
    output_facts: dict,
    ownership_record: dict,
    source_record_sha256: str,
    ship: dict | None = None,
    action_id: str | None = None,
    accepted_ms: int | None = None,
) -> dict:
    """Build a complete accepted record from verified staged and launch facts."""
    validate_run_ownership(ownership_record, require_worker=True)
    action_id = action_id or uuid.uuid4().hex
    next_action = {
        "mill": {"kind": "advance", "target_stage": "refine"},
        "refine": {"kind": "advance", "target_stage": "ship"},
        "ship": {"kind": "ship_archive", "target_stage": None},
    }.get(stage)
    if next_action is None:
        raise ValueError("completion stage is invalid")
    facts = ownership_record
    record = {
        "schema_version": SCHEMA_VERSION,
        "action_id": action_id,
        "lode_id": lode_id,
        "stage": stage,
        "run_generation": run_generation,
        "accepted_at_ms": accepted_ms if accepted_ms is not None else int(time.time() * 1000),
        "boot_id": facts["boot_id"],
        "phase": "accepted",
        "next_action": next_action,
        "output": {
            **output_facts,
            "canonical_name": f"{stage}_out.md",
            "repair_token": secrets.token_urlsafe(32),
            "published": False,
            "failure": None,
        },
        "ownership": {
            "source_record_relative_path": run_ownership_path(lode_id, run_generation).name,
            "source_record_sha256": source_record_sha256,
            "captured": False,
            "captured_at_ms": None,
            "platform": facts["platform"],
            "proof_mode": facts["proof_mode"],
            "pane": facts["pane"],
            "supervisor": facts["supervisor"],
            "worker": facts["worker"],
            "process_group": facts["process_group"],
            "descendants": facts["descendants"],
            "unit": facts["unit"],
            "cgroup": facts["cgroup"],
        },
        "containment": {
            "state": "not_started",
            "started_monotonic_ns": None,
            "deadline_monotonic_ns": None,
            "poll_interval_ms": POLL_INTERVAL_MS,
            "last_cgroup_observation": None,
            "last_supervisor_observation": None,
            "last_owned_process_count": None,
            "result": None,
            "proof_label": None,
            "last_error": None,
        },
        "spawn": None,
        "ship": ship,
        "markers": new_markers(),
        "recovery": {"kind": None, "message": None, "command": None},
    }
    validate_pending_completion(record)
    return record


def write_pending_completion(record: dict) -> Path:
    """Validate and durably publish the sole pending record for its lode."""
    validate_pending_completion(record)
    path = pending_completion_path(record["lode_id"])
    write_durable_json(path, record)
    return path


def load_pending_completion(lode_id: str) -> dict | None:
    """Load and validate a lode's pending record, or return None when absent."""
    path = pending_completion_path(lode_id)
    try:
        with open(path, encoding="utf-8") as source:
            record = json.load(source)
    except FileNotFoundError:
        return None
    validate_pending_completion(record)
    if record["lode_id"] != lode_id:
        raise ValueError("pending completion belongs to a different lode")
    return record


def _validate_spawn_receipt(receipt: dict) -> dict:
    keys = {
        "schema_version",
        "action_id",
        "source_lode_id",
        "target_lode_id",
        "target_generation",
        "pane_id",
    }
    receipt = _object(receipt, "spawn receipt", keys)
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported spawn receipt schema")
    _string(receipt["action_id"], "spawn receipt action_id", pattern=_HEX32)
    _string(receipt["source_lode_id"], "spawn receipt source_lode_id", pattern=_LODE_ID)
    _string(receipt["target_lode_id"], "spawn receipt target_lode_id", pattern=_LODE_ID)
    _string(receipt["target_generation"], "spawn receipt generation", pattern=_HEX32)
    _string(receipt["pane_id"], "spawn receipt pane_id")
    return receipt


def write_spawn_receipt(receipt: dict) -> Path:
    """Durably publish the pane identity before its supervisor starts."""
    receipt = _validate_spawn_receipt(receipt)
    path = spawn_receipt_path(receipt["source_lode_id"], receipt["action_id"])
    write_durable_json(path, receipt)
    return path


def load_spawn_receipt(lode_id: str, action_id: str) -> dict | None:
    """Load an action-scoped pane bootstrap receipt, preserving absence."""
    path = spawn_receipt_path(lode_id, action_id)
    try:
        with open(path, encoding="utf-8") as source:
            receipt = json.load(source)
    except FileNotFoundError:
        return None
    receipt = _validate_spawn_receipt(receipt)
    if receipt["source_lode_id"] != lode_id or receipt["action_id"] != action_id:
        raise ValueError("spawn receipt identity does not match its path")
    return receipt


def clear_pending_completion(record: dict) -> None:
    """Durably remove a completed action's staged blob and pending fence."""
    validate_pending_completion(record)
    if record["markers"]["pending_clear"]["state"] != "done":
        raise ValueError("pending completion cannot clear before pending_clear is done")
    directory = lode_dir(record["lode_id"])
    staged = directory / record["output"]["staged_relative_path"]
    try:
        staged.unlink()
    except FileNotFoundError:
        pass
    else:
        _fsync_directory(staged.parent)
    pending = pending_completion_path(record["lode_id"])
    try:
        pending.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(directory)


def stage_output(
    lode_id: str,
    data: bytes,
    *,
    blob_id: str | None = None,
    expected_length: int | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """Durably stage and reverify exact completion bytes."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("completion output must be nonempty bytes")
    digest = hashlib.sha256(data).hexdigest()
    if expected_length is not None and expected_length != len(data):
        raise ValueError("completion output length does not match message")
    if expected_sha256 is not None and not secrets.compare_digest(expected_sha256, digest):
        raise ValueError("completion output SHA-256 does not match message")
    blob_id = blob_id or uuid.uuid4().hex
    if _HEX32.fullmatch(blob_id) is None:
        raise ValueError("blob ID must be 32 lowercase hexadecimal characters")
    directory = staging_dir(lode_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{blob_id}.blob"
    if path.exists():
        raise FileExistsError(path)
    _durable_replace(path, lambda output: output.write(data))
    identity = verify_output_file(path, len(data), digest)
    return {
        "blob_id": blob_id,
        "staged_relative_path": f"{COMPLETION_STAGING_DIRNAME}/{blob_id}.blob",
        "staged_identity": identity,
        "byte_length": len(data),
        "digest_algorithm": DIGEST_ALGORITHM,
        "digest_hex": digest,
    }


def repair_staged_output(record: dict, data: bytes) -> dict:
    """Durably replace only the staged blob with the immutable accepted bytes."""
    validate_pending_completion(record)
    if not isinstance(data, bytes):
        raise ValueError("repair output must be bytes")
    output = record["output"]
    if len(data) != output["byte_length"]:
        raise ValueError("repair output length does not match accepted output")
    digest = hashlib.sha256(data).hexdigest()
    if not secrets.compare_digest(digest, output["digest_hex"]):
        raise ValueError("repair output SHA-256 does not match accepted output")
    path = lode_dir(record["lode_id"]) / output["staged_relative_path"]
    _durable_replace(path, lambda destination: destination.write(data))
    return verify_output_file(path, output["byte_length"], output["digest_hex"])


def pending_output_recovery(record: dict) -> dict | None:
    """Return the capability-bearing status projection only for blocked output."""
    validate_pending_completion(record)
    output = record["output"]
    if (
        record["phase"] != "output_blocked"
        or output["published"]
        or record["recovery"]["kind"] != "output"
    ):
        return None
    return {
        "stage": record["stage"],
        "action_id": record["action_id"],
        "sha256": output["digest_hex"],
        "byte_length": output["byte_length"],
        "repair_token": output["repair_token"],
        "failure": output["failure"] or record["recovery"]["message"],
        "command": record["recovery"]["command"],
    }


def verify_output_file(path: Path, length: int, digest_hex: str) -> dict:
    """Verify exact bytes and return their filesystem identity."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as source:
        before = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(source.fileno())
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise OSError("completion output identity changed while reading")
    if size != length or not secrets.compare_digest(digest.hexdigest(), digest_hex):
        raise ValueError("completion output bytes do not match accepted digest and length")
    return {"st_dev": before.st_dev, "st_ino": before.st_ino}


def verify_staged_output(record: dict) -> Path:
    """Verify that the server-owned staged blob still matches its accepted identity."""
    validate_pending_completion(record)
    output = record["output"]
    staged = lode_dir(record["lode_id"]) / output["staged_relative_path"]
    identity = verify_output_file(staged, output["byte_length"], output["digest_hex"])
    if identity != output["staged_identity"]:
        raise OSError("staged completion output identity changed")
    return staged


def publish_output(record: dict) -> Path:
    """Publish a verified staged blob to its canonical stage output path."""
    output = record["output"]
    directory = lode_dir(record["lode_id"])
    staged = verify_staged_output(record)

    canonical = directory / output["canonical_name"]

    def copy_and_verify(destination) -> None:
        digest = hashlib.sha256()
        length = 0
        with open(staged, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)
                digest.update(chunk)
                length += len(chunk)
        if length != output["byte_length"] or not secrets.compare_digest(
            digest.hexdigest(), output["digest_hex"]
        ):
            raise ValueError("staged completion output changed while publishing")

    _durable_replace(canonical, copy_and_verify)
    verify_output_file(canonical, output["byte_length"], output["digest_hex"])
    return canonical


def collect_orphaned_staging(lode_id: str, record: dict | None = None) -> list[Path]:
    """Delete writer temporaries and blobs not referenced by a valid record."""
    if record is None:
        record = load_pending_completion(lode_id)
    elif record["lode_id"] != lode_id:
        raise ValueError("pending completion belongs to a different lode")
    if record is not None:
        validate_pending_completion(record)
    keep = record["output"]["staged_relative_path"].split("/", 1)[1] if record else None
    directory = staging_dir(lode_id)
    if not directory.is_dir():
        return []
    removed = []
    for path in directory.iterdir():
        if path.is_file() and _STAGING_FILE.fullmatch(path.name) and path.name != keep:
            path.unlink()
            removed.append(path)
    if removed:
        _fsync_directory(directory)
    return removed


def new_marker() -> dict:
    """Return an untouched durable phase marker."""
    return {"state": "not_started", "attempt_id": None, "detail": None}


def new_markers() -> dict:
    """Return the complete marker map for a new completion."""
    return {name: new_marker() for name in MARKER_NAMES}


def transition_marker(
    record: dict,
    name: str,
    state: str,
    *,
    attempt_id: str | None = None,
    detail: str | None = None,
) -> dict:
    """Apply one legal marker transition in memory and return the record."""
    if name not in MARKER_NAMES or state not in MARKER_STATES:
        raise ValueError("unknown completion marker or state")
    marker = record["markers"][name]
    if state not in MARKER_TRANSITIONS[marker["state"]]:
        raise ValueError(f"illegal marker transition {marker['state']} -> {state}")
    if state == "intent":
        attempt_id = attempt_id or uuid.uuid4().hex
        if _HEX32.fullmatch(attempt_id) is None:
            raise ValueError("marker attempt ID must be 32 lowercase hexadecimal characters")
    elif attempt_id is None:
        attempt_id = marker["attempt_id"]
    if state in {"done", "blocked"} and attempt_id != marker["attempt_id"]:
        raise ValueError("marker result does not match its durable attempt")
    marker.update(state=state, attempt_id=attempt_id, detail=detail)
    return record


def recovery_command(record: dict, kind: str) -> str:
    """Return the single operator command for a blocked completion phase."""
    if kind == "output":
        return (
            f"hop lode repair-output {record['lode_id']} - "
            f"--token {record['output']['repair_token']}"
        )
    return f"hop lode restart {record['lode_id']}"


def completion_status(record: dict) -> str:
    """Project one pending record into the exact operator-facing status text."""
    phase = record["phase"]
    stage = record["stage"]
    recovery = record["recovery"]
    if phase == "output_blocked":
        detail = recovery["message"] or "completion output publication failed"
        command = recovery["command"]
        if recovery["kind"] == "publication":
            return f"Teardown blocked: {detail}. Retry with: {command}"
        output = record["output"]
        return (
            f"Teardown blocked: accepted {stage} output is unavailable "
            f"(sha256 {output['digest_hex']}, {output['byte_length']} bytes). Repair with: "
            f"{command}"
        )
    if phase in {"containment_blocked", "ship_blocked", "cleanup_blocked"}:
        detail = recovery["message"] or "unknown teardown failure"
        ship = record.get("ship")
        if phase == "cleanup_blocked" and ship is not None and ship["archive_published"]:
            return f"Shipped; cleanup retained: {detail}. Retry with: {recovery['command']}"
        return f"Teardown blocked: {detail}. Retry with: {recovery['command']}"
    if phase in {"accepted", "publishing_output"}:
        return f"Teardown: publishing accepted {stage} output"
    if phase == "capturing_ownership":
        return "Teardown: capturing generation ownership"
    if phase == "closing_pane":
        return f"Teardown: closing pane {record['ownership']['pane']['pane_id']}"
    if phase in {"observing_containment", "force_killing"}:
        mode = record["ownership"]["proof_mode"]
        if mode == "linux-strict":
            return "Teardown: waiting for Linux containment"
        if mode == "darwin-bounded":
            return (
                "Teardown: waiting for bounded Darwin ownership "
                "(degraded; leak-free cleanup unproven)"
            )
        if mode == "linux-degraded":
            return (
                "Teardown: waiting for bounded Linux ownership "
                "(degraded; leak-free cleanup unproven)"
            )
        return "Teardown: waiting for bounded ownership (degraded; birth identity unavailable)"
    if phase == "proving_ship_landing":
        return "Teardown: proving ship landing"
    if phase == "quarantining":
        ship = record["ship"]
        if ship["archive_published"]:
            return "Teardown: cleaning quarantined worktree"
        return "Teardown: quarantining shipped worktree"
    if phase == "publishing_next_action":
        if record["ownership"]["proof_mode"] == "linux-degraded":
            return (
                "Teardown: bounded Linux containment observed "
                "(degraded; leak-free cleanup unproven); awaiting next action"
            )
        target = record["next_action"]["target_stage"]
        return f"Teardown: advancing to {target}" if target else "Teardown: archiving shipped lode"
    if phase == "spawning":
        return f"Teardown: starting {record['next_action']['target_stage']}"
    if phase == "complete" and record["ownership"]["proof_mode"] == "darwin-bounded":
        return "Shipped (bounded Darwin teardown; leak-free cleanup unproven)"
    if phase == "complete" and record["ownership"]["proof_mode"] == "linux-degraded":
        return "Shipped (bounded Linux teardown; systemd proof and leak-free cleanup unproven)"
    if phase == "complete" and record["ownership"]["proof_mode"] != "linux-strict":
        return "Shipped (degraded teardown; birth identity and leak-free cleanup unproven)"
    if phase == "complete":
        return "Shipped"
    return "Teardown: archiving shipped lode"


def staged_output_sha256(path: Path) -> str:
    """Return SHA-256 for a file without loading it all into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def accepted_at_ms() -> int:
    """Return a wall-clock timestamp for a newly accepted record."""
    return int(time.time() * 1000)
